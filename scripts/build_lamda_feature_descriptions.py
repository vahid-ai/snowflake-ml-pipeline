"""Generate researched descriptions for every feature in the IQSeC-Lab/LAMDA dataset.

LAMDA (arXiv:2505.18551) encodes each APK as a Drebin-style binary bag-of-words
vector: every column ``feat_i`` marks the presence of one static-analysis token
(a permission, API call, component name, intent action, hardware feature, or
embedded URL/domain). The Hub repo ships a ``feature_mapping.csv`` per config
that maps ``feat_i`` back to its original token.

This script downloads those mappings, classifies every token into its Drebin
feature set (S1-S8, Arp et al., NDSS 2014), and generates two texts per
feature — what the token is, and how it can be used as a signal for Android
malware detection — from a curated knowledge base of Android security research
(Drebin, PScout/Axplorer permission maps, AVClass family reports) plus
token-aware heuristics. The result is written to a JSON file that
``scripts/load_lamda_feature_descriptions.py`` loads into Iceberg tables on
Cloudflare R2.

Feature indices are config-specific: ``feat_0`` in the Baseline config is a
different token than ``feat_0`` in var_thresh_0.01. Descriptions are therefore
keyed by the token itself, with the per-config column ids attached.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from datetime import datetime, timezone
from pathlib import Path

DATASET_ID = "IQSeC-Lab/LAMDA"
CONFIGS = ("Baseline", "var_thresh_0.01")
DEFAULT_OUTPUT = Path(__file__).resolve().parent.parent / "data" / "lamda_feature_descriptions.json"

GENERATOR_VERSION = "1.0.0"

# --------------------------------------------------------------------------- #
# Category-level knowledge (Drebin feature sets S1-S8)
# --------------------------------------------------------------------------- #

CATEGORY_INFO = {
    "HardwareComponentsList": {
        "drebin_set": "S1 (hardware components)",
        "extraction_source": "AndroidManifest.xml <uses-feature> declarations",
        "description": (
            "Hardware and software capabilities the app declares it uses or requires via "
            "<uses-feature> entries in AndroidManifest.xml, e.g. camera, GPS, telephony, "
            "NFC, or specific sensors. Play-style stores use these to filter devices; the "
            "app itself is not granted anything by declaring them."
        ),
        "detection_signal": (
            "Individually weak but useful in combination: requesting access to hardware "
            "bundles that enable surveillance (camera + microphone + GPS + network) or "
            "SMS fraud (telephony) raises suspicion, while declarations typical of games "
            "or media apps lower it. Certain combinations are rare in benign apps and act "
            "as a prior over the app's intended capability surface; drift in hardware "
            "declarations also tracks ecosystem changes over LAMDA's 2013-2025 span."
        ),
    },
    "RequestedPermissionList": {
        "drebin_set": "S2 (requested permissions)",
        "extraction_source": "AndroidManifest.xml <uses-permission> declarations",
        "description": (
            "Permissions the app requests in its manifest. This is the classic Android "
            "malware feature set: permissions gate access to SMS, contacts, location, "
            "device identifiers, accounts, and system settings."
        ),
        "detection_signal": (
            "One of the most discriminative Drebin sets. Malware over-requests dangerous "
            "permissions relative to its advertised function (permission-function "
            "mismatch), and specific permissions (SEND_SMS, RECEIVE_SMS, "
            "SYSTEM_ALERT_WINDOW, BIND_ACCESSIBILITY_SERVICE, REQUEST_INSTALL_PACKAGES) "
            "are heavily enriched in toll-fraud, banker, and dropper families. "
            "Co-occurrence patterns (e.g. RECEIVE_SMS + INTERNET + RECEIVE_BOOT_COMPLETED) "
            "are stronger than any single permission."
        ),
    },
    "ActivityList": {
        "drebin_set": "S3 (app components)",
        "extraction_source": "AndroidManifest.xml <activity> declarations",
        "description": (
            "Fully-qualified (or package-relative, leading-dot) class names of activities "
            "— the app's UI screens — declared in the manifest. Names identify both "
            "app-specific screens and screens contributed by embedded SDKs (ad networks, "
            "payment, push, social login)."
        ),
        "detection_signal": (
            "Component names act as fingerprints: identical activity names across many "
            "APKs reveal repackaging campaigns and shared malware kits (Drebin found "
            "several DroidKungFu variants sharing component names), while ad/adware SDK "
            "activities flag aggressive monetization. Obfuscated single-letter or "
            "gibberish names, and lures like fake 'Update'/'Installer' screens, skew "
            "malicious; well-known benign SDK activities lower the malware prior."
        ),
    },
    "ServiceList": {
        "drebin_set": "S3 (app components)",
        "extraction_source": "AndroidManifest.xml <service> declarations",
        "description": (
            "Class names of services — long-running background components without UI — "
            "declared in the manifest. Services host background sync, push handling, "
            "media playback, and, in malware, C2 polling, premium-SMS senders, and "
            "spyware collectors."
        ),
        "detection_signal": (
            "Background services are where malware does its work: a service co-occurring "
            "with boot-completed receivers indicates persistence, and recognizable "
            "service names shared across APKs fingerprint families and botnets. Services "
            "from known push/ad SDKs (including grey-area SDKs like Igexin) identify "
            "embedded third-party code with its own risk profile."
        ),
    },
    "BroadcastReceiverList": {
        "drebin_set": "S3 (app components)",
        "extraction_source": "AndroidManifest.xml <receiver> declarations",
        "description": (
            "Class names of broadcast receivers — components that react to system or app "
            "events (boot completed, SMS received, connectivity changes) — declared in "
            "the manifest."
        ),
        "detection_signal": (
            "Receivers are the classic persistence and interception hook: names like "
            "BootReceiver paired with BOOT_COMPLETED intent filters give auto-start, and "
            "SMS-related receivers enable mTAN/OTP interception in banking trojans. "
            "Receiver names recurring across unrelated APKs are strong family "
            "fingerprints; device-admin receivers appear in ransomware and lockers."
        ),
    },
    "IntentFilterList": {
        "drebin_set": "S4 (filtered intents)",
        "extraction_source": "AndroidManifest.xml <intent-filter> actions/categories",
        "description": (
            "Intent actions and categories the app listens for via intent-filters — the "
            "events that can launch or wake its components, e.g. BOOT_COMPLETED, "
            "SMS_RECEIVED, USER_PRESENT, or push-SDK-specific actions."
        ),
        "detection_signal": (
            "Directly encodes triggering behavior: Drebin singled out BOOT_COMPLETED as a "
            "typical malware trigger (start after reboot without user action); "
            "SMS_RECEIVED (often with high priority) marks SMS interception; USER_PRESENT "
            "and connectivity actions time malicious activity to user/network state. "
            "Filters for package-install events support app-monitoring/overlay attacks. "
            "Rare custom actions shared across APKs fingerprint kits and their C2 wakeup "
            "paths."
        ),
    },
    "RestrictedApiList": {
        "drebin_set": "S5 (restricted API calls)",
        "extraction_source": "classes.dex static analysis (permission-protected framework calls)",
        "description": (
            "Calls found in the app's bytecode to Android framework APIs that are "
            "protected by a permission (per PScout/Axplorer-style permission maps), e.g. "
            "TelephonyManager, SmsManager, LocationManager, AccountManager, WifiManager "
            "methods."
        ),
        "detection_signal": (
            "Shows what permission-gated capabilities the code actually exercises, "
            "cutting through manifest noise. High-risk calls (device-ID reads, SMS "
            "sending, account enumeration, audio recording) are enriched in spyware and "
            "toll fraud; Drebin additionally flags restricted calls whose permission is "
            "NOT requested in the manifest — a hallmark of root exploits, loaded "
            "payloads, or dead kit code. Combinations (collect identifier + open "
            "network connection) outline exfiltration pipelines."
        ),
    },
    "UsedPermissionsList": {
        "drebin_set": "S6 (used permissions)",
        "extraction_source": "classes.dex static analysis (permissions implied by observed API calls)",
        "description": (
            "Permissions that are actually exercised by the API calls observed in the "
            "bytecode (derived by mapping restricted calls back to their guarding "
            "permission, in the style of Felt et al.'s Stowaway/PScout). A subset of the "
            "requested permissions that the code demonstrably uses."
        ),
        "detection_signal": (
            "Stronger evidence than a manifest request: a used SEND_SMS or "
            "READ_PHONE_STATE proves the code path exists. The gap between requested and "
            "used permissions is itself informative — over-requesting suggests "
            "boilerplate kits or future dynamic payloads, while used-but-not-requested "
            "gaps suggest privilege escalation or plugin loading."
        ),
    },
    "SuspiciousApiList": {
        "drebin_set": "S7 (suspicious API calls)",
        "extraction_source": "classes.dex static analysis (APIs disproportionately used by malware)",
        "description": (
            "A curated set of API calls and strings that grant access to sensitive data "
            "or resources and are disproportionately found in malware: device-identifier "
            "reads, SMS send/receive, Runtime.exec, reflection/dynamic loading, and "
            "root-shell artifacts like 'system/bin/su'."
        ),
        "detection_signal": (
            "Each token is a direct capability indicator: getDeviceId/getSubscriberId "
            "feed victim tracking and premium-service registration, sendTextMessage is "
            "the toll-fraud primitive, Runtime.exec and 'system/bin/su' indicate shell "
            "command execution and rooting attempts, HttpPost marks exfiltration "
            "plumbing. Benign apps use some of these too, so weight comes from "
            "combinations and from wrapper diversity (many distinct classes calling "
            "getSystemService is normal; TelephonyManager identifier reads plus HTTP "
            "POST is not)."
        ),
    },
    "URLDomainList": {
        "drebin_set": "S8 (network addresses)",
        "extraction_source": "classes.dex static analysis (URLs, hostnames and IPs in code/strings)",
        "description": (
            "Hostnames, URLs and IP addresses embedded in the app's code or resources — "
            "ad and analytics endpoints, CDNs, social/platform APIs, XML namespaces, and "
            "sometimes hard-coded C2 servers."
        ),
        "detection_signal": (
            "Hard-coded infrastructure is one of the highest-precision signals available "
            "statically: domains reused across unrelated APKs cluster campaigns and "
            "identify C2 or dropper hosts, raw IPs and dynamic-DNS hosts skew heavily "
            "malicious, and aggressive ad-network endpoints characterize ad-fraud and "
            "PUA families. Conversely, ubiquitous platform domains (googleapis.com, "
            "schema.org namespaces) mostly indicate mainstream SDK usage and act as "
            "benign-prior features. Domain features are also the most drift-prone set — "
            "infrastructure churns faster than code — making them central to LAMDA's "
            "concept-drift analyses."
        ),
    },
}

# --------------------------------------------------------------------------- #
# Permission knowledge base (used by Requested/Used permission categories)
# keyed by the segment after the last '.' of the permission string
# --------------------------------------------------------------------------- #

PERMISSION_KB: dict[str, tuple[str, str]] = {
    "INTERNET": (
        "allows the app to open network sockets and perform arbitrary network I/O",
        "Near-universal in both classes, so alone it carries little weight; its value is "
        "as an enabler in combinations (e.g. with READ_SMS or device-ID reads it "
        "completes an exfiltration path, and its absence makes many malicious behaviors "
        "impossible).",
    ),
    "SEND_SMS": (
        "allows the app to send SMS messages without user confirmation",
        "Classic toll-fraud primitive: premium-rate SMS trojans (FakeInst, OpFake, "
        "Boxer) depend on it. Heavily enriched in malware and one of the most "
        "discriminative single permissions in Drebin-style models, especially when the "
        "app's advertised purpose has no messaging function.",
    ),
    "RECEIVE_SMS": (
        "allows the app to receive and process incoming SMS messages",
        "Enables interception of mTAN/OTP codes (banking trojans like Zitmo/Marcher) and "
        "suppression of premium-SMS confirmations. Co-occurring with SEND_SMS or a "
        "high-priority SMS_RECEIVED intent-filter it is a strong fraud/interception "
        "indicator.",
    ),
    "READ_SMS": (
        "allows the app to read stored SMS messages",
        "Lets malware harvest OTPs, mTANs and private conversations from the inbox; a "
        "core spyware/banker capability rarely needed by benign non-messaging apps.",
    ),
    "WRITE_SMS": (
        "allows the app to modify or delete stored SMS messages",
        "Used by SMS trojans to delete carrier billing notifications and bank alerts, "
        "hiding fraud from the victim; very rare in legitimate apps.",
    ),
    "RECEIVE_MMS": (
        "allows the app to receive and process incoming MMS messages",
        "Same interception surface as RECEIVE_SMS, plus historical remote-exploit "
        "delivery (Stagefright-era); mostly seen in messaging apps or SMS malware.",
    ),
    "RECEIVE_WAP_PUSH": (
        "allows the app to receive WAP push messages",
        "WAP push can deliver silent configuration/URL payloads; requested almost "
        "exclusively by messaging apps and SMS-focused malware.",
    ),
    "CALL_PHONE": (
        "allows the app to initiate phone calls without going through the dialer UI",
        "Enables premium-number call fraud and USSD abuse; suspicious in apps without a "
        "calling function.",
    ),
    "READ_PHONE_STATE": (
        "allows reading phone status and identity, including IMEI/IMSI, phone number and call state",
        "Historically the top malware permission: IMEI/IMSI reads feed victim tracking, "
        "premium-service registration and bot enrollment. Very common in older benign "
        "apps too (ad SDKs), so its weight comes from co-occurrence with SMS/network "
        "exfiltration features and shifts over LAMDA's timespan as Google restricted "
        "identifier access.",
    ),
    "PROCESS_OUTGOING_CALLS": (
        "allows the app to observe, redirect or abort outgoing calls",
        "Call-interception surface used by spyware and banking trojans to reroute or "
        "monitor calls; rare in benign apps outside dialers.",
    ),
    "RECORD_AUDIO": (
        "allows the app to record audio via the microphone",
        "Core surveillance capability: stalkerware and RATs record calls and ambient "
        "audio. Benign in comms/voice apps, so mismatch with app purpose plus network "
        "exfiltration features is the signal.",
    ),
    "CAMERA": (
        "allows the app to access the camera and take pictures/video",
        "Surveillance-relevant (spyware capturing photos) but extremely common in benign "
        "apps; meaningful mainly in capability bundles (camera + audio + location + "
        "network) or when mismatched with app function.",
    ),
    "ACCESS_FINE_LOCATION": (
        "allows access to precise GPS-level location",
        "Victim-tracking capability central to stalkerware; also ubiquitous in benign "
        "apps, so signal comes from purpose mismatch and pairing with background "
        "persistence and exfiltration features.",
    ),
    "ACCESS_COARSE_LOCATION": (
        "allows access to approximate (network-based) location",
        "Same tracking surface as fine location at lower precision; widely used by ad "
        "SDKs, so weak alone but part of surveillance bundles.",
    ),
    "ACCESS_BACKGROUND_LOCATION": (
        "allows location access while the app is in the background (Android 10+)",
        "Continuous covert tracking capability; heavily restricted by Play policy, so "
        "its presence in sideloaded apps skews toward stalkerware.",
    ),
    "READ_CONTACTS": (
        "allows reading the user's contacts database",
        "Contact harvesting fuels SMS-worm propagation (sending malicious links to all "
        "contacts) and data theft; a key spyware feature when combined with network "
        "senders.",
    ),
    "WRITE_CONTACTS": (
        "allows modifying the user's contacts database",
        "Rarely needed outside contact managers; malware uses it to plant or alter "
        "entries (e.g. masking premium/bank numbers).",
    ),
    "READ_CALL_LOG": (
        "allows reading the call history",
        "Call-metadata harvesting is a standard stalkerware capability; restricted by "
        "Play policy since 2019, so requests outside dialer apps are suspicious.",
    ),
    "WRITE_CALL_LOG": (
        "allows modifying the call history",
        "Used by spyware to erase evidence of calls it placed or intercepted.",
    ),
    "GET_ACCOUNTS": (
        "allows listing accounts in the device's AccountManager (email, Google, etc.)",
        "Account enumeration identifies the victim and their services; feeds phishing "
        "and credential-theft flows in bankers and spyware.",
    ),
    "AUTHENTICATE_ACCOUNTS": (
        "allows the app to act as an account authenticator and manage account credentials (pre-M)",
        "Lets malware register fake authenticators or harvest tokens; low benign "
        "usage outside sync/identity apps.",
    ),
    "USE_CREDENTIALS": (
        "allows requesting auth tokens for accounts from AccountManager (pre-M)",
        "Token harvesting surface; combined with GET_ACCOUNTS it outlines credential "
        "theft.",
    ),
    "MANAGE_ACCOUNTS": (
        "allows adding/removing accounts and deleting their credentials (pre-M)",
        "Account manipulation capability rarely needed by ordinary apps; part of "
        "credential-theft bundles.",
    ),
    "RECEIVE_BOOT_COMPLETED": (
        "allows the app to receive the broadcast sent after the system finishes booting",
        "The persistence permission: Drebin explicitly cites boot-completed handling as "
        "typical malware behavior (auto-restart of background services without user "
        "action). Strong in combination with services and SMS/network features; also "
        "common in benign sync/alarm apps, so never decisive alone.",
    ),
    "SYSTEM_ALERT_WINDOW": (
        "allows drawing windows on top of all other apps (overlays)",
        "The overlay-attack permission: banking trojans (Marcher, Anubis, Cerberus "
        "lineage) draw fake login screens over real banking apps, and lockers/ransomware "
        "use it to pin the screen. Highly enriched in malware relative to its narrow "
        "benign uses (chat heads, floating widgets).",
    ),
    "WRITE_SETTINGS": (
        "allows modifying system settings",
        "Used by malware to change network/audio settings, set homepages, or aid "
        "persistence; modest benign usage in tool apps.",
    ),
    "WRITE_SECURE_SETTINGS": (
        "allows modifying secure system settings (normally signature/system-only)",
        "Should be unobtainable by third-party apps; requesting it signals rooted-device "
        "targeting or system-app repackaging.",
    ),
    "DISABLE_KEYGUARD": (
        "allows disabling the lock-screen keyguard",
        "Lets malware wake and unlock the screen to perform UI actions or show lures; "
        "combined with device-admin features it appears in lockers.",
    ),
    "WAKE_LOCK": (
        "allows keeping the CPU awake",
        "Ubiquitous benign permission (push, sync, media); in malware it keeps mining, "
        "C2 polling or spyware collection running — only meaningful in combination.",
    ),
    "VIBRATE": (
        "allows controlling the vibrator",
        "Benign-leaning utility permission; contributes to benign priors more than "
        "malware detection.",
    ),
    "GET_TASKS": (
        "allows retrieving the list of running/recent tasks (deprecated in L)",
        "Foreground-app detection is the trigger for overlay phishing (attack when the "
        "banking app is on screen) and for anti-analysis (detect AV/analysis tools); "
        "enriched in bankers of the 2013-2016 era covered by LAMDA.",
    ),
    "REORDER_TASKS": (
        "allows moving tasks to the foreground/background",
        "Used with GET_TASKS to force phishing screens on top or hide activity; low "
        "benign usage outside launchers.",
    ),
    "RESTART_PACKAGES": (
        "allows (historically) killing other apps' background processes",
        "Malware kills AV/cleaner processes or competing malware; deprecated and "
        "near-zero legitimate need in modern apps.",
    ),
    "KILL_BACKGROUND_PROCESSES": (
        "allows killing other apps' background processes",
        "Same AV-killing / competitor-killing surface as RESTART_PACKAGES; benign in "
        "task managers only.",
    ),
    "READ_EXTERNAL_STORAGE": (
        "allows reading shared/external storage",
        "Document and media harvesting surface for spyware; extremely common in benign "
        "apps, so contributes mainly through bundles.",
    ),
    "WRITE_EXTERNAL_STORAGE": (
        "allows writing to shared/external storage",
        "Very common benign permission; malware uses it to stage payloads or plant "
        "files, and historic ransomware encrypted external storage through it.",
    ),
    "MOUNT_UNMOUNT_FILESYSTEMS": (
        "allows mounting/unmounting removable storage (system-level)",
        "System permission that ordinary apps should not hold; requests indicate "
        "repackaged system tools or kits copying boilerplate manifests.",
    ),
    "INSTALL_PACKAGES": (
        "allows silently installing apps (system/signature-only)",
        "Unobtainable by normal apps: requesting it signals dropper intent targeting "
        "rooted devices or preloaded-system abuse; a strong malware indicator.",
    ),
    "REQUEST_INSTALL_PACKAGES": (
        "allows requesting installation of APKs from this app (sideloading, Android O+)",
        "The modern dropper permission: trojans use it to install second-stage payloads "
        "outside the store. Play policy tightly limits it, so it is enriched in droppers "
        "and off-store malware.",
    ),
    "DELETE_PACKAGES": (
        "allows silently uninstalling apps (system/signature-only)",
        "System-only; requested by malware to remove AV apps on rooted devices — a "
        "red-flag request for a third-party app.",
    ),
    "REQUEST_DELETE_PACKAGES": (
        "allows requesting uninstallation of packages",
        "Used to prompt removal of security apps or competing malware; rare benign use.",
    ),
    "BIND_DEVICE_ADMIN": (
        "binds a DeviceAdminReceiver, giving the app device-administrator powers once activated",
        "Device admin grants lock/wipe/password powers and uninstall resistance: "
        "ransomware and lockers coerce activation, then resist removal. Strong malware "
        "signal outside MDM/enterprise apps.",
    ),
    "BIND_ACCESSIBILITY_SERVICE": (
        "binds an AccessibilityService, allowing the app to read the screen and inject UI actions",
        "The most abused modern capability: bankers (Anubis, Cerberus, FluBot lineage) "
        "use accessibility to read screens, harvest credentials, auto-grant permissions "
        "and click through dialogs. Outside genuine accessibility tools this is a "
        "top-tier malware indicator in LAMDA's later years.",
    ),
    "BIND_NOTIFICATION_LISTENER_SERVICE": (
        "binds a NotificationListenerService that can read and dismiss all notifications",
        "Reads OTP codes and bank alerts from notifications (bypassing SMS "
        "restrictions) and can suppress security warnings; enriched in modern bankers "
        "and spyware.",
    ),
    "PACKAGE_USAGE_STATS": (
        "allows querying app-usage statistics (special access granted via Settings)",
        "Modern replacement for GET_TASKS in foreground-app detection, enabling timed "
        "overlay attacks; limited benign use (parental control, launchers).",
    ),
    "READ_LOGS": (
        "allows reading low-level system log files (system-only since 4.1)",
        "Historic data mine (pre-4.1 logs leaked other apps' data); modern requests are "
        "kit boilerplate or rooted-device targeting — skews malicious.",
    ),
    "ACCESS_NETWORK_STATE": (
        "allows reading network connectivity state",
        "Ubiquitous in both classes (ad SDKs check connectivity); malware gates C2 "
        "traffic on connectivity. Only meaningful in combinations.",
    ),
    "ACCESS_WIFI_STATE": (
        "allows reading Wi-Fi state, including SSID/BSSID and (historically) MAC address",
        "Wi-Fi identifiers served as tracking IDs and coarse geolocation; common in ad "
        "SDKs and spyware alike — bundle-level signal.",
    ),
    "CHANGE_WIFI_STATE": (
        "allows enabling/disabling Wi-Fi and modifying configured networks",
        "Malware toggles Wi-Fi to force cellular (billing fraud) or ensure "
        "connectivity for C2; modest benign use in utility apps.",
    ),
    "CHANGE_NETWORK_STATE": (
        "allows changing network connectivity (e.g. toggling mobile data, pre-M)",
        "Same fraud/connectivity-manipulation surface as CHANGE_WIFI_STATE.",
    ),
    "CHANGE_WIFI_MULTICAST_STATE": (
        "allows receiving Wi-Fi multicast packets",
        "Benign-leaning (device discovery, casting); little malware relevance beyond "
        "kit boilerplate.",
    ),
    "NFC": (
        "allows NFC I/O",
        "Benign-leaning (payments, tags); occasionally relevant to relay-attack "
        "research but rarely discriminative for commodity malware.",
    ),
    "BLUETOOTH": (
        "allows connecting to paired Bluetooth devices",
        "Benign-leaning; historical worms probed Bluetooth, but in LAMDA's span it "
        "mostly marks legitimate device-companion functionality.",
    ),
    "BLUETOOTH_ADMIN": (
        "allows discovering and pairing Bluetooth devices",
        "Benign-leaning like BLUETOOTH; contributes to capability bundles rather than "
        "standing alone.",
    ),
    "MODIFY_AUDIO_SETTINGS": (
        "allows modifying global audio settings",
        "Spyware mutes shutter/notification sounds during covert recording; also common "
        "in benign media apps — weak alone.",
    ),
    "SET_WALLPAPER": (
        "allows setting the system wallpaper",
        "Benign for personalization apps; wallpaper trojans were an early Android "
        "malware lure, so it co-occurs with adware/PUA families in older years.",
    ),
    "SET_WALLPAPER_HINTS": (
        "allows setting wallpaper size hints",
        "Companion to SET_WALLPAPER with the same personalization-app profile.",
    ),
    "EXPAND_STATUS_BAR": (
        "allows expanding/collapsing the status bar",
        "Minor UI manipulation used by some lockers/adware; largely benign utility "
        "boilerplate.",
    ),
    "FLASHLIGHT": (
        "allows controlling the camera flashlight",
        "Benign utility permission; flashlight apps were, however, a classic adware/PUA "
        "wrapper, so it co-occurs with aggressive ad SDK features.",
    ),
    "BROADCAST_STICKY": (
        "allows sending sticky broadcasts (deprecated)",
        "Legacy mechanism with negligible security value; mostly kit boilerplate in "
        "either class.",
    ),
    "READ_CALENDAR": (
        "allows reading calendar events",
        "Personal-data harvesting surface (schedules reveal victim patterns); benign in "
        "productivity apps, part of spyware bundles otherwise.",
    ),
    "WRITE_CALENDAR": (
        "allows adding/modifying calendar events",
        "Calendar-spam adware plants event reminders as an ad channel; low benign need "
        "outside calendar apps.",
    ),
    "READ_HISTORY_BOOKMARKS": (
        "allows reading the stock browser's history and bookmarks (com.android.browser)",
        "Browsing-history theft is straightforward surveillance; Drebin-era spyware "
        "commonly harvested it, and benign use is nearly nonexistent outside browsers.",
    ),
    "WRITE_HISTORY_BOOKMARKS": (
        "allows modifying the stock browser's history/bookmarks",
        "Used by adware to plant bookmarks/homepages; near-zero benign need outside "
        "browsers.",
    ),
    "ACCESS_LOCATION_EXTRA_COMMANDS": (
        "allows access to extra location provider commands (e.g. GPS aiding-data injection)",
        "Low-information companion to location permissions; mostly boilerplate in "
        "location-using apps of either class.",
    ),
    "ACCESS_ASSISTED_GPS": (
        "vendor/legacy permission for assisted-GPS access",
        "Legacy boilerplate accompanying location bundles; negligible standalone "
        "signal.",
    ),
    "GET_PACKAGE_SIZE": (
        "allows retrieving the storage size of any package",
        "Occasionally used in cleaner/task apps and by malware enumerating installed "
        "apps; weak alone.",
    ),
    "CLEAR_APP_CACHE": (
        "allows clearing the caches of installed apps",
        "Cleaner-app permission; malware masquerading as cleaners/boosters (a common "
        "PUA lure) requests it, so it co-occurs with that family cluster.",
    ),
    "SET_ALARM": (
        "allows setting alarms in the clock app",
        "Benign-leaning; occasionally used by malware for crude scheduling.",
    ),
    "USE_FINGERPRINT": (
        "allows using fingerprint hardware for authentication",
        "Benign-leaning (auth flows); in malware mostly boilerplate from embedded "
        "SDKs.",
    ),
    "USE_BIOMETRIC": (
        "allows using biometric authentication hardware",
        "Benign-leaning modern auth permission.",
    ),
    "FOREGROUND_SERVICE": (
        "required (Android 9+) to run foreground services",
        "Ubiquitous modern permission; malware needs it for persistent visible-ish "
        "services (often with misleading notifications), so it marks the modern "
        "persistence pattern without being discriminative alone.",
    ),
    "POST_NOTIFICATIONS": (
        "required (Android 13+) to post notifications",
        "Ubiquitous modern permission; in malware it supports phishing notifications, "
        "but it is a weak, era-marking feature that tracks LAMDA's later years.",
    ),
    "QUERY_ALL_PACKAGES": (
        "allows querying the full list of installed apps (Android 11+)",
        "Installed-app inventory drives targeted overlay attacks (which bank apps are "
        "present?) and AV detection; Play-restricted, so enriched in bankers and "
        "spyware in recent years.",
    ),
    "READ_PRIVILEGED_PHONE_STATE": (
        "system-only access to privileged phone state/identifiers",
        "Unobtainable by third-party apps; requesting it reveals system-app "
        "repackaging or kit boilerplate targeting rooted/OEM contexts.",
    ),
    "ACCESS_SUPERUSER": (
        "legacy marker permission for requesting root via Superuser apps",
        "Explicit rooted-device targeting: apps declaring it expect to run su. Strong "
        "signal for root-abusing tools and malware droppers.",
    ),
    "DEVICE_POWER": (
        "system-only permission to control device power state",
        "System-only request that appears in repackaged/boilerplate malicious "
        "manifests.",
    ),
    "REBOOT": (
        "system-only permission to reboot the device",
        "System-only; requested by lockers/kits as boilerplate — near-zero benign "
        "third-party use.",
    ),
    "BATTERY_STATS": (
        "allows collecting battery statistics",
        "Benign-leaning diagnostics permission; occasional anti-analysis use (emulator "
        "batteries behave oddly).",
    ),
    "SYSTEM_OVERLAY_WINDOW": (
        "system-only variant of overlay windows",
        "System-only overlay request signalling locker/banker boilerplate.",
    ),
    "STATUS_BAR": (
        "system-only permission to open/close/disable the status bar",
        "Lockers disable the status bar to trap victims; system-only, so any request "
        "is kit boilerplate worth flagging.",
    ),
    "PERSISTENT_ACTIVITY": (
        "legacy permission to make an activity persistent (deprecated)",
        "Deprecated persistence mechanism; appears in old malware kits.",
    ),
    "ACCESS_ADSERVICES_AD_ID": (
        "allows access to the Privacy-Sandbox advertising ID (Android 13+)",
        "Marks modern ad-SDK presence; era feature useful for drift analysis more than "
        "maliciousness.",
    ),
    "ACCESS_ADSERVICES_ATTRIBUTION": (
        "allows access to Privacy-Sandbox attribution APIs",
        "Modern ad-SDK marker like ACCESS_ADSERVICES_AD_ID.",
    ),
    "ACCESS_ADSERVICES_TOPICS": (
        "allows access to Privacy-Sandbox Topics API",
        "Modern ad-SDK marker like ACCESS_ADSERVICES_AD_ID.",
    ),
    "AD_ID": (
        "allows access to the Google advertising ID (Android 13+)",
        "Ad-SDK marker permission; tracks monetized apps in both classes.",
    ),
    "SCHEDULE_EXACT_ALARM": (
        "allows scheduling exact alarms (Android 12+)",
        "Modern scheduling permission; malware uses exact alarms for reliable C2 "
        "polling/trigger timing, but benign alarm/reminder apps dominate.",
    ),
    "USE_EXACT_ALARM": (
        "allows exact alarms for alarm/timer apps (Android 13+)",
        "Benign-leaning modern scheduling permission.",
    ),
    "HIDE_OVERLAY_WINDOWS": (
        "allows an app to hide other apps' overlays above it (Android 12+)",
        "Defensive permission used by banking apps against overlay malware; its "
        "presence marks security-conscious apps (benign prior).",
    ),
}

# keyword fallbacks for permissions not in the KB, checked in order
PERMISSION_KEYWORD_RULES: list[tuple[re.Pattern[str], str, str]] = [
    (re.compile(r"C2D_MESSAGE$|c2dm", re.I),
     "a per-app Google Cloud Messaging (C2DM/GCM) permission authorizing push messages for this package",
     "Push-registration boilerplate present in most GCM-era apps; near-zero standalone "
     "weight, but its package prefix fingerprints the app/kit identity, which helps "
     "cluster repackaged families."),
    (re.compile(r"MIPUSH|JPUSH|GETUI|IGEXIN|UPUSH|HONORPUSH|HMS|PUSH", re.I),
     "a vendor push-SDK permission (OEM or third-party push channel)",
     "Marks an embedded push SDK. Chinese third-party push SDKs (JPush, Getui/Igexin, "
     "UPush) are frequent in grey-market APKs and some (Igexin) have shipped "
     "malicious plugin loaders, so specific SDK identity shifts the prior."),
    (re.compile(r"BADGE|SHORTCUT", re.I),
     "a launcher badge/shortcut permission used to show unread counts or create shortcuts",
     "Launcher-integration boilerplate; INSTALL_SHORTCUT historically enabled "
     "ad/scareware shortcut spam, so shortcut permissions co-occur with adware."),
    (re.compile(r"BILLING|PAY|IAP|VENDING", re.I),
     "a billing/in-app-purchase related permission",
     "Marks monetized apps; fake-payment and purchase-hijacking malware also request "
     "billing permissions, so it contributes to fraud-oriented bundles."),
    (re.compile(r"ACCOUNT", re.I),
     "an account-related permission controlling access to device accounts",
     "Account access supports victim identification and credential flows; weight "
     "depends on co-occurring exfiltration features."),
    (re.compile(r"STORAGE|MEDIA", re.I),
     "a storage/media access permission",
     "Storage access is ubiquitous; in malware it supports payload staging and "
     "document/media harvesting as part of larger bundles."),
    (re.compile(r"LOCATION|GPS", re.I),
     "a location-related permission",
     "Location access is a tracking capability; evaluate against app purpose and "
     "co-occurring background/persistence features."),
    (re.compile(r"BOOT", re.I),
     "a boot-event permission enabling launch-at-startup behavior",
     "Persistence-oriented: auto-start after reboot is a classic malware trait, "
     "shared with benign sync/alarm apps."),
    (re.compile(r"SMS|MMS", re.I),
     "an SMS/MMS-related permission",
     "SMS capabilities are the historical core of Android malware monetization "
     "(premium SMS, OTP theft); strongly weighted especially off-store."),
    (re.compile(r"DOWNLOAD", re.I),
     "a download-manager related permission",
     "Supports payload retrieval in droppers as well as ordinary content downloads; "
     "bundle-level signal."),
    (re.compile(r"NOTIFICATION", re.I),
     "a notification-related permission",
     "Notification access/posting supports phishing notifications and OTP-alert "
     "reading in malware, against a large benign base."),
    (re.compile(r"WAKE|ALARM", re.I),
     "a scheduling/wakeup-related permission",
     "Keeps background work alive — C2 polling and collection in malware, sync in "
     "benign apps; combination feature."),
]


def describe_permission(perm: str, used: bool) -> tuple[str, str]:
    suffix = perm.rsplit(".", 1)[-1].strip()
    kb = PERMISSION_KB.get(suffix.upper())
    vendor = "" if perm.startswith(("android.", "com.android.")) else (
        " It is a non-AOSP (vendor/app-defined) permission, so its package prefix also "
        "fingerprints a specific SDK or app family."
    )
    if kb:
        what, signal = kb
    else:
        for pattern, what, signal in PERMISSION_KEYWORD_RULES:
            if pattern.search(perm):
                break
        else:
            what = "an Android permission gating access to a protected capability"
            signal = (
                "Not in the curated high-signal set; contributes through rarity and "
                "co-occurrence — uncommon permissions shared by many APKs often "
                "fingerprint a common kit or SDK."
            )
    if used:
        description = (
            f"Binary feature: 1 if static analysis of the DEX bytecode finds API calls that "
            f"actually exercise the permission {perm}, which {what}."
        )
        signal = (
            f"{signal} As a *used* (not merely requested) permission, this is code-level "
            "evidence the capability is exercised, which weighs more than a manifest "
            "request alone."
        )
    else:
        description = (
            f"Binary feature: 1 if the APK's manifest requests the permission {perm} "
            f"via <uses-permission>, which {what}.{vendor}"
        )
    return description, signal


# --------------------------------------------------------------------------- #
# Restricted / suspicious API knowledge
# --------------------------------------------------------------------------- #

API_CLASS_KB: dict[str, tuple[str, str]] = {
    "android.telephony.TelephonyManager": (
        "telephony state and identity (IMEI/MEID, IMSI, SIM details, phone number, network info)",
        "Identifier reads (getDeviceId, getSubscriberId, getSimSerialNumber, getLine1Number) "
        "are staple spyware/bot-enrollment calls used to tag victims and register premium "
        "services; also used by ad SDKs, so pair with exfiltration features.",
    ),
    "android.telephony.SmsManager": (
        "sending SMS/MMS messages programmatically",
        "sendTextMessage without UI is the premium-SMS-fraud primitive (FakeInst/OpFake); "
        "among the highest-precision restricted calls for toll fraud.",
    ),
    "android.telephony.gsm.SmsManager": (
        "the deprecated GSM-specific SMS sending API",
        "Same toll-fraud primitive as SmsManager; its deprecated form marks older kits in "
        "LAMDA's early years.",
    ),
    "android.accounts.AccountManager": (
        "the device account database: enumerating accounts and obtaining auth tokens",
        "Account enumeration and token retrieval (getAccounts*, getAuthToken, "
        "blockingGetAuthToken) support victim identification and credential/token theft; "
        "benign use is concentrated in sync and identity apps.",
    ),
    "android.location.LocationManager": (
        "GPS/network location fixes and provider control",
        "Continuous location polling in the background is a stalkerware signature; "
        "evaluate with persistence features and network senders.",
    ),
    "android.net.wifi.WifiManager": (
        "Wi-Fi state, scan results, configured networks and connectivity control",
        "SSID/BSSID reads provide tracking and coarse location; enabling/disabling Wi-Fi "
        "manipulates connectivity for fraud or C2. Mixed benign usage in utility apps.",
    ),
    "android.net.ConnectivityManager": (
        "network connectivity state and (historically) toggling mobile data",
        "Connectivity checks gate C2 traffic and time attacks to online periods; "
        "ubiquitous benign use makes this a combination feature.",
    ),
    "android.app.ActivityManager": (
        "running task/process introspection and memory info",
        "getRunningTasks/getRunningAppProcesses drive overlay-attack timing (detect "
        "foreground banking app) and anti-analysis (detect AV/emulator processes); "
        "killBackgroundProcesses removes defenses.",
    ),
    "android.media.AudioManager": (
        "audio routing, volume and ringer control",
        "Spyware silences ringers/shutter sounds during covert capture; benign media use "
        "is broad, so weight comes from co-occurrence with recording APIs.",
    ),
    "android.media.MediaRecorder": (
        "audio/video recording",
        "Programmatic recording is the core capture primitive of stalkerware and RATs; "
        "with call-state listeners it implies call recording.",
    ),
    "android.media.AudioRecord": (
        "low-level PCM audio capture",
        "Raw microphone capture favored by surveillance code (no UI, no shutter sound); "
        "stronger spyware association than MediaRecorder.",
    ),
    "android.hardware.Camera": (
        "the legacy camera API",
        "Programmatic capture (takePicture without preview tricks) appears in spyware; "
        "in benign apps the camera is usually user-driven.",
    ),
    "android.bluetooth.BluetoothAdapter": (
        "Bluetooth radio control, discovery and pairing",
        "Mostly benign device-companion functionality; historical worm propagation makes "
        "it a minor era feature.",
    ),
    "android.bluetooth.BluetoothDevice": (
        "operations on remote Bluetooth devices",
        "Benign-leaning; contributes to device-interaction bundles.",
    ),
    "android.bluetooth.BluetoothHeadset": (
        "Bluetooth headset profile control",
        "Benign-leaning audio-routing surface.",
    ),
    "android.bluetooth.BluetoothSocket": (
        "RFCOMM Bluetooth data connections",
        "Benign-leaning; rare data channel for proximity malware.",
    ),
    "android.app.NotificationManager": (
        "posting and cancelling notifications",
        "Phishing notifications and fake system alerts are common malware lures; benign "
        "base is enormous, so context decides.",
    ),
    "android.app.WallpaperManager": (
        "reading/setting the system wallpaper",
        "Personalization surface; wallpaper apps were an early adware lure so it "
        "co-occurs with PUA clusters in older years.",
    ),
    "android.app.DownloadManager": (
        "system download service for queued HTTP downloads",
        "Droppers use it to fetch second-stage APKs with OS-level reliability; also "
        "ubiquitous benign content downloading.",
    ),
    "android.app.KeyguardManager$KeyguardLock": (
        "programmatic lock-screen disabling (deprecated)",
        "Disabling the keyguard lets malware act on the screen or show lures while the "
        "device appears locked; low benign need.",
    ),
    "android.content.ContentResolver": (
        "querying/modifying content providers (contacts, SMS, calendar, browser data)",
        "The read path for bulk PII harvesting (contacts, SMS inbox, call log) in "
        "spyware; benign use is universal, so which providers co-occur matters.",
    ),
    "android.content.Context": (
        "context-level service access and file/directory operations",
        "Generic plumbing (getSystemService, openFileOutput); near-zero standalone "
        "signal beyond marking service usage.",
    ),
    "android.os.PowerManager$WakeLock": (
        "keeping the CPU/screen awake",
        "Sustains covert background work (mining, C2 loops, collection); benign push/"
        "media use dominates, so combination-level signal.",
    ),
    "android.os.Vibrator": (
        "vibration control",
        "Benign-leaning UI feedback.",
    ),
    "android.provider.Browser": (
        "the stock browser's history/bookmarks provider",
        "Browsing-history reads are near-pure surveillance outside browsers; Drebin-era "
        "spyware harvested it routinely.",
    ),
    "android.provider.ContactsContract$Contacts": (
        "the contacts provider",
        "Bulk contact reads feed SMS-worm propagation and data theft.",
    ),
    "android.provider.ContactsContract$RawContacts": (
        "raw contact records in the contacts provider",
        "Same harvesting surface as Contacts with write/merge access.",
    ),
    "android.provider.Contacts$People": (
        "the legacy (pre-2.0) contacts provider",
        "Legacy contact harvesting; marks old kits in LAMDA's early years.",
    ),
    "android.provider.CalendarContract$Reminders": (
        "calendar reminders provider",
        "Calendar-spam adware plants reminders as an ad channel; low benign need "
        "outside calendar apps.",
    ),
    "android.provider.Settings$System": (
        "reading/writing system settings",
        "Settings manipulation (ringtones, brightness, network) used by adware/lockers; "
        "modest benign tool-app use.",
    ),
    "android.provider.Settings$Secure": (
        "reading secure settings (e.g. ANDROID_ID, enabled accessibility services)",
        "ANDROID_ID reads provide a tracking identifier; checking enabled accessibility "
        "services appears in both bankers (self-check) and anti-malware.",
    ),
    "android.speech.SpeechRecognizer": (
        "speech-to-text recognition",
        "Benign-leaning assistant functionality; niche eavesdropping applications.",
    ),
    "android.nfc.NfcAdapter": (
        "NFC adapter state and dispatch",
        "Benign-leaning (payments/tags); relay-attack niche only.",
    ),
    "android.webkit.WebView": (
        "embedded web rendering",
        "WebView with JavaScript bridges is a common malware loader/phishing surface "
        "(addJavascriptInterface abuse), but benign hybrid apps dominate usage.",
    ),
    "android.widget.VideoView": (
        "video playback widget",
        "Benign media functionality.",
    ),
    "android.media.MediaPlayer": (
        "audio/video playback",
        "Benign media functionality; wake-lock variants keep devices awake.",
    ),
    "android.media.Ringtone": (
        "ringtone playback",
        "Benign personalization surface; ringtone apps were an early premium-SMS lure.",
    ),
    "android.media.RingtoneManager": (
        "ringtone enumeration and setting",
        "Benign personalization; co-occurs with early premium-SMS ringtone scams.",
    ),
    "android.inputmethodservice.KeyboardView": (
        "custom soft-keyboard rendering",
        "Custom keyboards can keylog; outside genuine IME apps this raises credential-"
        "theft concerns.",
    ),
    "android.view.View": (
        "base UI view operations (here: permission-gated ones like haptic feedback)",
        "Generic UI plumbing with negligible standalone signal.",
    ),
    "android.app.KeyguardManager": (
        "lock-screen state queries and control",
        "Keyguard checks time attacks to locked/unlocked state; low standalone weight.",
    ),
    "android.net.wifi.p2p.WifiP2pManager": (
        "Wi-Fi Direct peer-to-peer networking",
        "Benign-leaning sharing functionality.",
    ),
}

SUSPICIOUS_METHOD_KB: dict[str, tuple[str, str]] = {
    "getSystemService": (
        "obtains a system service handle (TelephonyManager, LocationManager, etc.) — the "
        "gateway call to most sensitive framework services",
        "Drebin counts getSystemService as suspicious because every sensitive-service "
        "interaction starts here; the calling class distinguishes app code from library "
        "wrappers. Weight is low alone and comes from which services and identifier "
        "reads co-occur.",
    ),
    "getDeviceId": (
        "reads a device identifier (on TelephonyManager, the IMEI/MEID)",
        "IMEI harvesting tags victims for tracking, bot enrollment and premium-service "
        "registration; a classic spyware call, especially followed by network sends. "
        "(On view/input classes the same method name is benign input-device plumbing — "
        "the class qualifier matters.)",
    ),
    "getSubscriberId": (
        "reads the subscriber identifier (IMSI) from TelephonyManager",
        "IMSI reads are more privacy-invasive than IMEI (identifies the SIM/subscriber); "
        "heavily used by Chinese SMS-fraud families and spyware for victim registration.",
    ),
    "getSimCountryIso": (
        "reads the SIM card's country code",
        "Used for premium-number selection per country in toll fraud, and for geo-"
        "fencing (avoid AV-heavy or researcher regions); also benign localization.",
    ),
    "sendTextMessage": (
        "sends an SMS programmatically without user interaction",
        "The single most direct toll-fraud call: silent premium-SMS sending built "
        "FakeInst/OpFake-era monetization, and worms propagate via SMS to contacts. "
        "Very high precision when the app has no messaging purpose.",
    ),
    "getMessageBody": (
        "extracts the body text from a received SmsMessage",
        "Parsing incoming SMS bodies is the OTP/mTAN interception step in banking "
        "trojans and the C2 channel of SMS-controlled bots; benign only in real "
        "messaging apps.",
    ),
    "exec": (
        "executes a shell command via Runtime.exec",
        "Shell execution runs su, mounts partitions, drops payloads and probes the "
        "system; a top Drebin suspicious call. Benign uses exist (logcat, ping) but "
        "combined with 'system/bin/su' or chmod strings it indicates rooting/dropper "
        "behavior.",
    ),
    "printStackTrace": (
        "prints an exception stack trace (java.io.IOException here)",
        "Not sensitive itself — it fingerprints unpolished/kit code paths around "
        "network I/O; contributes stylometric rather than capability signal.",
    ),
    "getWifiState": (
        "reads the Wi-Fi radio state",
        "Connectivity probing that gates C2/fraud logic; ubiquitous benign use, "
        "combination-level weight.",
    ),
    "setWifiEnabled": (
        "programmatically enables/disables Wi-Fi",
        "Forcing Wi-Fi off pushes traffic to billable cellular (fraud) or ensures a "
        "clean channel for C2; modest benign utility use.",
    ),
    "getExternalStorageDirectory": (
        "resolves the shared external-storage root path",
        "Staging ground access: malware drops payload APKs and harvests documents from "
        "external storage; enormous benign base, so pairs with dropper/exfil features.",
    ),
    "getPackageInfo": (
        "queries PackageManager for an installed package's details",
        "Installed-app reconnaissance: detect AV products, target banking apps for "
        "overlays, or check for competitors; also common benign self-checks.",
    ),
}

# --------------------------------------------------------------------------- #
# Intent action knowledge
# --------------------------------------------------------------------------- #

INTENT_KB: dict[str, tuple[str, str]] = {
    "android.intent.action.BOOT_COMPLETED": (
        "fired once after the system finishes booting",
        "The canonical persistence trigger: Drebin explicitly cites listening for "
        "BOOT_COMPLETED as typical malware behavior (auto-restart background services "
        "after reboot). Strong when paired with services and SMS/network capabilities; "
        "also used by benign sync/alarm apps.",
    ),
    "android.provider.Telephony.SMS_RECEIVED": (
        "fired when an SMS arrives",
        "SMS interception hook: bankers grab OTPs/mTANs and SMS trojans read C2 "
        "commands; kits register it with high priority to beat the default app. One of "
        "the highest-precision intent features outside real messaging apps.",
    ),
    "android.provider.Telephony.SMS_DELIVER": (
        "delivered only to the default SMS app when an SMS arrives (KitKat+)",
        "Malware posing as the default SMS app gains exclusive SMS access — a "
        "post-KitKat interception pattern seen in bankers/spyware.",
    ),
    "android.intent.action.NEW_OUTGOING_CALL": (
        "fired when an outgoing call is placed",
        "Call interception/redirect hook used by spyware and fraud (rewriting dialed "
        "numbers); rare benign use outside dialers.",
    ),
    "android.intent.action.PHONE_STATE": (
        "fired on call-state changes (ringing/offhook/idle)",
        "Call monitoring trigger for spyware call-recording and for suppressing "
        "evidence during fraud calls.",
    ),
    "android.intent.action.USER_PRESENT": (
        "fired when the user unlocks the device",
        "Times attacks to active use: adware pops ads and bankers launch overlays when "
        "the user is present; a favored malware trigger with modest benign use.",
    ),
    "android.intent.action.SCREEN_ON": (
        "fired when the screen turns on",
        "User-activity trigger like USER_PRESENT (must be registered in code; manifest "
        "presence marks kit patterns).",
    ),
    "android.intent.action.SCREEN_OFF": (
        "fired when the screen turns off",
        "Covert-window trigger: spyware starts recording or C2 sync when the screen is "
        "off to avoid observation.",
    ),
    "android.intent.action.PACKAGE_ADDED": (
        "fired when a new app is installed",
        "App-monitoring hook: adware pushes 'you installed X' ads, bankers refresh "
        "overlay targets, and guard code detects AV installs.",
    ),
    "android.intent.action.PACKAGE_REMOVED": (
        "fired when an app is uninstalled",
        "Monitoring/self-protection hook (react to AV or companion removal).",
    ),
    "android.intent.action.PACKAGE_REPLACED": (
        "fired when a package is updated",
        "Persistence helper: restart services after self-update; also benign SDK use.",
    ),
    "android.intent.action.MY_PACKAGE_REPLACED": (
        "fired to an app after it is updated",
        "Standard persistence-across-update hook in both classes; mild signal.",
    ),
    "android.net.conn.CONNECTIVITY_CHANGE": (
        "fired on network connectivity changes",
        "Gates C2 polling and bulk exfiltration to online windows; very common benign "
        "use, combination-level feature.",
    ),
    "android.intent.action.BATTERY_LOW": (
        "fired when the battery is low",
        "Occasionally used to throttle malicious background work; mostly benign "
        "power-management.",
    ),
    "android.intent.action.ACTION_POWER_CONNECTED": (
        "fired when power is connected",
        "Charging windows favor heavy covert work (mining, bulk upload); mostly benign.",
    ),
    "android.intent.action.ACTION_POWER_DISCONNECTED": (
        "fired when power is disconnected",
        "Companion to POWER_CONNECTED with the same mild profile.",
    ),
    "android.app.action.DEVICE_ADMIN_ENABLED": (
        "delivered to a DeviceAdminReceiver when device-admin is activated",
        "Marks a device-admin component: ransomware/lockers coerce activation for lock/"
        "wipe powers and uninstall resistance. Strong signal outside MDM apps.",
    ),
    "android.app.action.ACTION_PASSWORD_CHANGED": (
        "device-admin callback for password changes",
        "Device-admin surveillance of credential changes; MDM-or-malware profile.",
    ),
    "android.intent.action.MAIN": (
        "the main entry-point action of an app",
        "Universal launcher boilerplate with negligible standalone signal; its rare "
        "absence (no launchable UI) is itself a hiding indicator.",
    ),
    "android.intent.action.VIEW": (
        "generic display-data action (deep links, URL handling)",
        "Universal benign pattern; malware uses VIEW filters for phishing deep links, "
        "so specific schemes/hosts matter more than the action.",
    ),
    "android.intent.action.SEND": (
        "share-sheet data-send action",
        "Benign sharing boilerplate.",
    ),
    "android.accessibilityservice.AccessibilityService": (
        "binds an accessibility service",
        "Accessibility is the most abused modern capability: screen reading, credential "
        "harvesting, auto-clicking through permission dialogs (Anubis/Cerberus/FluBot "
        "lineages). Outside genuine accessibility tools this is a top-tier signal in "
        "LAMDA's later years.",
    ),
    "android.service.notification.NotificationListenerService": (
        "binds a notification-listener service",
        "Reads all notifications (OTP codes, bank alerts) and can dismiss security "
        "warnings; enriched in modern bankers/spyware.",
    ),
    "android.intent.action.QUICKBOOT_POWERON": (
        "HTC/vendor variant of the boot-completed event",
        "Vendor boot trigger requested alongside BOOT_COMPLETED for broader persistence "
        "coverage — its presence usually means deliberate cross-device auto-start.",
    ),
    "com.htc.intent.action.QUICKBOOT_POWERON": (
        "HTC-specific quick-boot power-on event",
        "Vendor persistence trigger like QUICKBOOT_POWERON.",
    ),
    "android.intent.action.TIME_SET": (
        "fired when the system time is changed",
        "Anti-analysis/scheduling niche (detect time manipulation); mostly benign "
        "alarm-app use.",
    ),
    "android.intent.action.TIMEZONE_CHANGED": (
        "fired when the timezone changes",
        "Benign scheduling boilerplate.",
    ),
    "android.intent.action.DATE_CHANGED": (
        "fired when the date changes",
        "Benign scheduling boilerplate; occasional time-bomb trigger.",
    ),
    "android.intent.action.AIRPLANE_MODE": (
        "fired when airplane mode toggles",
        "Connectivity monitoring; minor signal.",
    ),
    "android.intent.action.HEADSET_PLUG": (
        "fired when a headset is (un)plugged",
        "Benign audio boilerplate.",
    ),
    "android.media.RINGER_MODE_CHANGED": (
        "fired when the ringer mode changes",
        "Benign audio boilerplate; spyware occasionally tracks silent mode.",
    ),
    "android.intent.action.CREATE_SHORTCUT": (
        "launcher request to create a shortcut",
        "Shortcut spam was a classic adware channel; co-occurs with PUA clusters.",
    ),
    "com.android.vending.INSTALL_REFERRER": (
        "Play-Store install-referrer broadcast delivering campaign attribution",
        "Ad-attribution boilerplate present in most monetized apps; benign-leaning, "
        "useful mainly as an SDK fingerprint.",
    ),
    "com.google.android.c2dm.intent.RECEIVE": (
        "receives GCM/FCM push messages",
        "Push boilerplate in most GCM-era apps; malware also drives C2 over push, so "
        "value is as plumbing context, not a standalone flag.",
    ),
    "com.google.android.c2dm.intent.REGISTRATION": (
        "receives GCM registration results",
        "Push-registration boilerplate like c2dm RECEIVE.",
    ),
    "android.appwidget.action.APPWIDGET_UPDATE": (
        "home-screen widget update callback",
        "Benign widget boilerplate; rarely, widgets provide persistence for adware.",
    ),
    "android.intent.action.DOWNLOAD_COMPLETE": (
        "fired when a DownloadManager download finishes",
        "Dropper hook (install fetched payload on completion) as well as ordinary "
        "content-download handling.",
    ),
}

INTENT_KEYWORD_RULES: list[tuple[re.Pattern[str], str, str]] = [
    (re.compile(r"BOOT|POWERON", re.I),
     "a boot/startup-related event",
     "Persistence trigger family: auto-start at boot is the classic malware persistence "
     "pattern, shared with benign sync apps."),
    (re.compile(r"SMS|MMS", re.I),
     "an SMS/MMS-related event or binding",
     "SMS event handling outside messaging apps marks interception/fraud capability."),
    (re.compile(r"PUSH|C2DM|GCM|FCM|MIPUSH|JPUSH|GETUI|IGEXIN|XMPUSH|HMS", re.I),
     "a push-SDK message/registration action",
     "Embedded push SDK plumbing; SDK identity fingerprints app lineage, and some "
     "grey push SDKs (Igexin) have shipped malicious loaders."),
    (re.compile(r"ADMIN", re.I),
     "a device-administration event",
     "Device-admin components appear in ransomware/lockers for lock/wipe powers and "
     "uninstall resistance; strong outside MDM."),
    (re.compile(r"PACKAGE", re.I),
     "a package (install/remove/update) lifecycle event",
     "App-inventory monitoring supports overlay targeting, ad spam and AV detection."),
    (re.compile(r"ALARM|TIMER|SCHEDULE", re.I),
     "an alarm/scheduling event",
     "Scheduling plumbing that in malware drives periodic C2 polling; mostly benign."),
    (re.compile(r"CONNECTIVITY|WIFI|NETWORK", re.I),
     "a connectivity-related event",
     "Gates network-dependent behavior (C2, exfiltration) to online windows; very "
     "common benign use."),
    (re.compile(r"NOTIFICATION", re.I),
     "a notification-related action",
     "Notification plumbing; listener bindings can read OTPs and suppress warnings."),
]

# --------------------------------------------------------------------------- #
# URL/domain classification
# --------------------------------------------------------------------------- #

AD_NETWORKS = (
    "admob", "doubleclick", "applovin", "applvn", "inmobi", "jumptap", "adwo",
    "mopub", "millennialmedia", "flurry", "chartboost", "vungle", "unityads",
    "adcolony", "startapp", "airpush", "leadbolt", "tapjoy", "ironsrc",
    "ironsource", "smaato", "adfonic", "madvertise", "mobclix", "mdotm",
    "adsmogo", "domob", "youmi", "waps", "wooboo", "casee", "vpon", "adchina",
    "smartadserver", "wapx", "appflood", "duapps", "batmobi", "appnext",
    "pubmatic", "criteo", "adjust", "appsflyer", "branch.io", "kochava",
    "singular", "tenjin", "supersonicads", "facebook.com/ads", "moatads",
    "amazon-adsystem", "yandex.ru/ads", "mobfox", "nexage", "rubiconproject",
    "openx", "greystripe", "adwhirl", "mobvista", "mintegral", "bytedance",
    "pangle", "adtilt", "everbadge", "sponsorpay", "fyber",
)
ANALYTICS = (
    "google-analytics", "crashlytics", "firebase", "fabric.io", "umeng",
    "mixpanel", "amplitude", "segment", "newrelic", "bugsnag", "sentry",
    "localytics", "appcenter", "hockeyapp", "countly", "talkingdata", "growingio",
)
PLATFORM = (
    "google.com", "googleapis.com", "gstatic.com", "googleusercontent",
    "android.com", "goo.gl", "play.google", "youtube.com", "ytimg",
    "facebook.com", "fbcdn", "graph.facebook", "twitter.com", "twimg",
    "instagram.com", "linkedin.com", "pinterest.com", "whatsapp.com",
    "apple.com", "microsoft.com", "windows.net", "amazonaws.com",
    "cloudfront.net", "akamai", "cloudflare", "fastly", "jsdelivr", "unpkg",
    "github.com", "githubusercontent", "gitlab.com", "bitbucket.org",
    "sourceforge.net", "paypal.com", "stripe.com", "baidu.com", "qq.com",
    "weibo.com", "wechat", "tencent", "alibaba", "alipay", "taobao", "aliyun",
    "163.com", "sina.com", "sohu.com", "yandex", "vk.com", "mail.ru",
)
SCHEMA_NS = (
    "w3.org", "schemas.android.com", "schema.org", "xmlpull.org", "apache.org",
    "xml.org", "purl.org", "openmobilealliance.org", "whatwg.org", "json.org",
    "ns.adobe.com", "specs.xmlsoap.org", "schemas.xmlsoap.org", "wapforum.org",
)
SHORTENERS = ("bit.ly", "goo.gl", "tinyurl", "t.co", "ow.ly", "is.gd", "j.mp", "adf.ly")
DYNDNS = ("no-ip", "dyndns", "duckdns", "3322.org", "8866.org", "changeip", "vicp.net", "xicp.net")

IP_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}(:\d+)?$")


def describe_url(item: str) -> tuple[str, str]:
    lower = item.lower()
    base = (
        f"Binary feature: 1 if the hostname/URL '{item}' appears in the APK's DEX code "
        "or string constants (Drebin S8 network-address extraction)."
    )
    if IP_RE.match(lower.split("/")[0]):
        return base, (
            "A hard-coded raw IP address: benign apps almost always use hostnames, so "
            "embedded IPs skew strongly toward C2 servers, static payload hosts and "
            "kit-configured infrastructure; IP reuse across APKs clusters campaigns."
        )
    if any(d in lower for d in DYNDNS):
        return base, (
            "A dynamic-DNS host: rented, rotating infrastructure favored for C2 because "
            "it survives IP churn and costs nothing — heavily malware-skewed, rare in "
            "legitimate apps."
        )
    if any(d in lower for d in SHORTENERS):
        return base, (
            "A URL-shortener domain: hides the true destination of payload/phishing "
            "links; enriched in adware and social-engineering flows."
        )
    if any(d in lower for d in SCHEMA_NS):
        return base, (
            "An XML/schema namespace URI (build tooling and parser boilerplate), not "
            "live network traffic; near-universal in APKs and acts as a benign-leaning "
            "background feature — its absence in a stripped/obfuscated binary can be "
            "the anomaly."
        )
    if any(d in lower for d in AD_NETWORKS):
        return base, (
            "An advertising-network endpoint: marks an embedded ad SDK. Ad-SDK "
            "constellations separate aggressive adware/PUA and ad-fraud clickers from "
            "clean apps, fingerprint repackaging campaigns (pirated apps rebuilt with "
            "the repackager's ad IDs), and date the APK's era for drift analysis."
        )
    if any(d in lower for d in ANALYTICS):
        return base, (
            "An analytics/crash-reporting endpoint: marks a mainstream telemetry SDK. "
            "Mildly benign-leaning (professional app hygiene), while specific SDK mixes "
            "fingerprint app lineage; some regional analytics SDKs collect aggressively "
            "and co-occur with PUA."
        )
    if any(d in lower for d in PLATFORM):
        return base, (
            "A major platform/CDN domain: mostly marks mainstream SDK and content "
            "usage, giving a benign-leaning prior. Malware also abuses big platforms "
            "for hosting (e.g. payload on cloud storage) so path-level context and "
            "co-occurring features decide."
        )
    return base, (
        "An app- or vendor-specific endpoint. Embedded infrastructure is high-precision "
        "evidence: the same uncommon domain across many unrelated APKs marks a shared "
        "kit, C2 or dropper host, and domain features drive family clustering. Being "
        "the most drift-prone feature set (infrastructure churns yearly), such tokens "
        "also power LAMDA's concept-drift experiments."
    )


# --------------------------------------------------------------------------- #
# Component (activity/service/receiver) classification
# --------------------------------------------------------------------------- #

SDK_PREFIXES: list[tuple[str, str, str]] = [
    ("androidx.", "an AndroidX/Jetpack library component",
     "Standard modern library scaffolding: benign-leaning, and its presence/version mix "
     "dates the APK's build era for drift analysis."),
    ("android.support.", "an Android Support Library component",
     "Legacy support-library scaffolding; benign-leaning and marks pre-AndroidX builds "
     "(useful era/drift feature)."),
    ("com.google.android.gms", "a Google Play services component",
     "Play-services scaffolding: benign-leaning, and rarely spoofed by malware "
     "imitating Google package names — exact-name matching matters."),
    ("com.google.firebase", "a Firebase SDK component",
     "Mainstream backend SDK; benign-leaning build fingerprint."),
    ("com.facebook.", "a Facebook SDK component",
     "Mainstream social/ads SDK; benign-leaning, contributes to SDK-mix fingerprints."),
    ("com.unity3d", "a Unity engine/ads component",
     "Game-engine scaffolding; benign-leaning, marks games and Unity-ads monetization."),
]

SDK_KEYWORDS: list[tuple[re.Pattern[str], str, str]] = [
    (re.compile(r"admob|applovin|inmobi|chartboost|vungle|adcolony|mopub|startapp|"
                r"airpush|leadbolt|tapjoy|ironsource|adwo|domob|youmi|waps|appnext|"
                r"mobvista|mintegral|adactivity|adservice|advert", re.I),
     "an advertising-SDK component",
     "Ad-SDK components (interstitial activities, ad services) mark monetization "
     "aggressiveness: constellations of many ad SDKs typify adware/PUA and ad-fraud "
     "families, and repackaged apps carry the repackager's ad components."),
    (re.compile(r"gcm|fcm|c2dm|jpush|getui|igexin|mipush|umeng|xg\b|xinge|push", re.I),
     "a push-messaging SDK component",
     "Push plumbing doubles as a C2 channel; specific SDKs fingerprint lineage and "
     "grey SDKs like Igexin have shipped malicious plugin loaders."),
    (re.compile(r"alipay|wxpay|wechatpay|unionpay|paypal|billing|purchase|pay\b", re.I),
     "a payment/billing SDK component",
     "Payment components mark monetized apps and are impersonated by purchase-"
     "hijacking and fake-payment malware — exact class names separate real SDKs from "
     "look-alikes."),
    (re.compile(r"boot", re.I),
     "a boot-event handling component",
     "Boot receivers implement auto-start persistence — the classic Drebin malware "
     "trait, shared with benign sync apps; pairing with SMS/network features decides."),
    (re.compile(r"sms|mms", re.I),
     "an SMS/MMS-handling component",
     "SMS components outside messaging apps mark interception (OTP theft) or "
     "premium-SMS fraud capability — historically among the strongest component "
     "signals."),
    (re.compile(r"admin|devicepolicy", re.I),
     "a device-administration component",
     "Device-admin receivers grant lock/wipe powers and uninstall resistance; "
     "ransomware/locker-associated outside MDM apps."),
    (re.compile(r"accessib", re.I),
     "an accessibility-service component",
     "Accessibility services are the most abused modern capability (screen reading, "
     "auto-granting, overlay support in bankers); strong signal outside genuine "
     "accessibility tools."),
    (re.compile(r"ringtone|wallpaper", re.I),
     "a ringtone/wallpaper personalization component",
     "Personalization apps were a classic premium-SMS and adware lure in LAMDA's early "
     "years; these components co-occur with those family clusters."),
    (re.compile(r"update|install|download", re.I),
     "an update/installer/download component",
     "Fake 'update' and 'installer' components are standard dropper and FakeInst-style "
     "lures; benign updaters exist, so kit-name reuse across APKs is the tell."),
    (re.compile(r"lock|screen", re.I),
     "a lock/screen-related component",
     "Screen-locking components appear in lockers/ransomware and in benign locker "
     "apps; co-occurrence with device-admin decides."),
    (re.compile(r"notif", re.I),
     "a notification-handling component",
     "Notification plumbing; listener services can read OTP notifications."),
    (re.compile(r"audio|music|media|video|player", re.I),
     "a media playback/recording component",
     "Media components are benign-leaning; recording-oriented names paired with "
     "microphone permissions suggest surveillance."),
    (re.compile(r"wizard|splash|main|home|launcher|login|settings|about|help|web|"
                r"browser|detail|list|search|share|feedback", re.I),
     "a common app UI screen/component",
     "Generic UI naming; individually weak, but exact-name reuse across unrelated "
     "APKs fingerprints shared kits and repackaging campaigns."),
]

COMPONENT_KIND = {
    "ActivityList": ("an activity (UI screen)", "<activity>"),
    "ServiceList": ("a service (background component)", "<service>"),
    "BroadcastReceiverList": ("a broadcast receiver (event-handling component)", "<receiver>"),
}


def describe_component(category: str, item: str) -> tuple[str, str]:
    kind, tag = COMPONENT_KIND[category]
    relative = item.startswith(".")
    base = (
        f"Binary feature: 1 if the APK's manifest declares {kind} named '{item}' "
        f"via {tag}."
    )
    if relative:
        base += (
            " The leading dot marks a package-relative (app-local) class, i.e. code "
            "belonging to the app itself rather than a bundled library."
        )
    for prefix, what, signal in SDK_PREFIXES:
        if item.startswith(prefix):
            return base + f" This is {what}.", signal
    for pattern, what, signal in SDK_KEYWORDS:
        if pattern.search(item):
            return base + f" The name indicates {what}.", signal
    extra = (
        "App-local component names are the raw material for repackaging and family "
        "fingerprints: the same class name recurring across unrelated APKs (as Drebin "
        "observed for DroidKungFu variants) indicates a shared kit; single-letter or "
        "gibberish names indicate obfuscation, which skews malicious."
        if relative
        else "Fully-qualified third-party names identify embedded libraries; the SDK "
        "mix fingerprints app lineage, and uncommon packages shared across APKs mark "
        "common kits."
    )
    return base, extra


# --------------------------------------------------------------------------- #
# Per-category dispatch
# --------------------------------------------------------------------------- #

HW_KEYWORDS: list[tuple[re.Pattern[str], str, str]] = [
    (re.compile(r"camera", re.I), "camera hardware",
     "Camera declarations are near-universal; in capability bundles (camera + mic + "
     "location + network) they support surveillance profiles."),
    (re.compile(r"telephony", re.I), "telephony (cellular/SIM) hardware",
     "Requiring telephony ties the app to SIM-capable devices — consistent with SMS "
     "fraud and call abuse; Drebin highlights requesting telephony together with GPS "
     "as a privacy-invasive combination."),
    (re.compile(r"location|gps", re.I), "location hardware (GPS/network positioning)",
     "Location capability supports tracking; combined with telephony or network "
     "features it outlines victim-monitoring profiles."),
    (re.compile(r"microphone|audio", re.I), "microphone/audio hardware",
     "Microphone declarations in non-comms apps pair with RECORD_AUDIO in "
     "surveillance bundles."),
    (re.compile(r"bluetooth", re.I), "Bluetooth hardware",
     "Benign-leaning connectivity declaration."),
    (re.compile(r"wifi", re.I), "Wi-Fi hardware",
     "Benign-leaning connectivity declaration; part of connectivity-manipulation "
     "bundles at most."),
    (re.compile(r"nfc", re.I), "NFC hardware",
     "Benign-leaning (payments/tags)."),
    (re.compile(r"fingerprint", re.I), "fingerprint sensor hardware",
     "Benign-leaning auth capability; also marks the modern era for drift."),
    (re.compile(r"touchscreen|faketouch", re.I), "touchscreen input capability",
     "Ubiquitous boilerplate with negligible discriminative weight."),
    (re.compile(r"sensor", re.I), "a device sensor (accelerometer, gyroscope, etc.)",
     "Mostly benign (games/fitness); sensor checks also appear in emulator-detection "
     "(anti-analysis) code because emulators lack real sensors."),
    (re.compile(r"screen\.|landscape|portrait", re.I), "a screen-orientation capability",
     "Ubiquitous boilerplate; negligible weight."),
    (re.compile(r"vr\.|vulkan|gamepad|leanback|type\.pc|usb", re.I),
     "a specialized platform capability (VR/graphics/TV/PC/USB)",
     "Marks niche device targets; benign-leaning and era-dating."),
    (re.compile(r"wallpaper", re.I), "live-wallpaper software capability",
     "Personalization capability; wallpaper apps were an early adware/premium-SMS "
     "lure, so it co-occurs with those clusters in early years."),
]


def describe_hardware(item: str) -> tuple[str, str]:
    for pattern, what, signal in HW_KEYWORDS:
        if pattern.search(item):
            return (
                f"Binary feature: 1 if the APK's manifest declares the platform feature "
                f"'{item}' ({what}) via <uses-feature>.",
                signal,
            )
    return (
        f"Binary feature: 1 if the APK's manifest declares the platform feature "
        f"'{item}' via <uses-feature>.",
        "Declares a device capability the app targets; weak alone, contributes to "
        "capability-bundle and era/drift signals.",
    )


def describe_intent(item: str) -> tuple[str, str]:
    if item == "":
        return (
            "Binary feature: 1 if the manifest contains an intent-filter entry that "
            "parsed to an empty token (malformed or empty action string).",
            "A parsing artifact; empty/malformed manifest entries are slightly more "
            "common in carelessly built or machine-generated (kit) APKs.",
        )
    kb = INTENT_KB.get(item)
    base = (
        f"Binary feature: 1 if the APK's manifest declares an intent-filter for "
        f"'{item}'"
    )
    if kb:
        what, signal = kb
        return f"{base}, {what}.", signal
    for pattern, what, signal in INTENT_KEYWORD_RULES:
        if pattern.search(item):
            return f"{base}, {what}.", signal
    if item.startswith(("android.intent.", "android.")):
        return (
            f"{base}, a platform intent action/category.",
            "Standard platform event handling; weight comes from co-occurrence with "
            "capability features rather than the action alone.",
        )
    return (
        f"{base}, a custom (app- or SDK-defined) action.",
        "Custom actions are strong fingerprints: an uncommon action string shared "
        "across unrelated APKs marks a common SDK or malware kit and its internal "
        "wakeup/C2 message bus; Drebin-style models exploit exactly this reuse.",
    )


def describe_restricted_api(item: str) -> tuple[str, str]:
    cls, _, method = item.rpartition(".")
    kb = API_CLASS_KB.get(cls)
    method_kb = SUSPICIOUS_METHOD_KB.get(method)
    base = (
        f"Binary feature: 1 if the DEX bytecode contains a call to {method}() on "
        f"{cls}, a permission-protected framework API"
    )
    if kb:
        what, signal = kb
        base += f" for {what}."
    else:
        base += "."
        signal = (
            "Shows the code exercises a permission-gated capability; weight follows "
            "the sensitivity of the underlying data/resource and co-occurring "
            "exfiltration features."
        )
    if method_kb:
        m_what, m_signal = method_kb
        base += f" The specific call {m_what}."
        signal = m_signal
    signal += (
        " Drebin additionally treats a restricted call whose guarding permission is "
        "absent from the manifest as its own red flag (root exploit or loaded payload)."
    )
    return base, signal


def describe_suspicious_api(item: str) -> tuple[str, str]:
    if item == "system/bin/su":
        return (
            "Binary feature: 1 if the string 'system/bin/su' (the superuser binary "
            "path) appears in the APK's code/strings.",
            "Direct rooting indicator: the app checks for or invokes the su binary — "
            "root detection in some benign apps, but privilege escalation and root "
            "abuse in droppers and exploit kits; strongly weighted with Runtime.exec.",
        )
    if item.startswith("Lorg/apache/http"):
        return (
            f"Binary feature: 1 if the DEX bytecode references {item} (Apache "
            "HttpClient POST request).",
            "HTTP POST is the workhorse of data exfiltration and C2 check-ins in "
            "Drebin-era malware; legacy Apache HttpClient usage also dates the build. "
            "Weight comes from co-occurring identifier reads and collection features.",
        )
    # smali-ish token: L<path>/Class.method or L<path>/Class;->method
    token = item.lstrip("L").replace(";->", ".")
    cls_path, _, method = token.rpartition(".")
    cls = cls_path.replace("/", ".")
    method_kb = SUSPICIOUS_METHOD_KB.get(method)
    base = (
        f"Binary feature: 1 if the DEX bytecode calls {method}() via {cls} — one of "
        "Drebin's 'suspicious API' set (calls seen disproportionately in malware)."
    )
    if method_kb:
        m_what, m_signal = method_kb
        base = (
            f"Binary feature: 1 if the DEX bytecode calls {method}() via {cls}, which "
            f"{m_what}. Part of Drebin's 'suspicious API' set."
        )
        signal = m_signal
    else:
        signal = (
            "Flagged in the Drebin suspicious-API list for granting access to "
            "sensitive data or resources; evaluate through co-occurrence with "
            "identifier reads and network senders."
        )
    if "support/v4" in item or "androidx" in item.lower():
        signal += (
            " The calling class is library/compat code, so this variant often marks "
            "SDK wrappers rather than app logic — the class qualifier differentiates "
            "who makes the call."
        )
    return base, signal


def describe_token(category: str, item: str) -> tuple[str, str]:
    if category in ("RequestedPermissionList", "UsedPermissionsList"):
        # Requested list also contains <uses-feature>-style strings some manifests
        # put in <uses-permission>; describe those as hardware declarations.
        if item.startswith(("android.hardware.", "android.software.")):
            desc, signal = describe_hardware(item)
            return (
                desc.replace("<uses-feature>", "<uses-permission> (a hardware feature "
                             "string placed in a permission tag — common manifest "
                             "sloppiness)"),
                signal + " Appearing as a mis-placed permission entry, it also "
                "fingerprints manifest-generation tooling.",
            )
        return describe_permission(item, used=category == "UsedPermissionsList")
    if category == "HardwareComponentsList":
        return describe_hardware(item)
    if category == "RestrictedApiList":
        return describe_restricted_api(item)
    if category == "SuspiciousApiList":
        return describe_suspicious_api(item)
    if category == "IntentFilterList":
        return describe_intent(item)
    if category == "URLDomainList":
        return describe_url(item)
    if category in COMPONENT_KIND:
        return describe_component(category, item)
    raise ValueError(f"unknown category {category}")


# --------------------------------------------------------------------------- #
# Metadata / provenance columns present in the warehouse tables
# --------------------------------------------------------------------------- #

METADATA_COLUMNS = [
    {
        "column_name": "hash",
        "description": "SHA-256 digest of the APK, the sample's unique identifier (joins to AndroZoo).",
        "detection_signal": "Identifier only — used for deduplication, AndroZoo/VirusTotal joins and leakage-free splits, never as a model feature.",
    },
    {
        "column_name": "label",
        "description": "Ground-truth class: 0 = benign, 1 = malware (malware iff >= 4 VirusTotal engines flagged the sample).",
        "detection_signal": "The supervised target. The 4+ AV threshold reduces label noise but biases toward well-detected malware; low-detection greyware sits in the benign class.",
    },
    {
        "column_name": "family",
        "description": "Malware family assigned by AVClass2 from VirusTotal vendor labels (benign samples carry a placeholder).",
        "detection_signal": "Target for family classification and class-incremental learning; also groups samples for campaign-level analysis of shared features.",
    },
    {
        "column_name": "vt_count",
        "description": "Number of VirusTotal engines that flagged the sample at collection time.",
        "detection_signal": "Label-confidence measure: high counts mark consensus malware, counts 1-3 mark greyware excluded from the malware class. Not a deployable feature (it IS the AV verdict) but useful for noise-aware training.",
    },
    {
        "column_name": "year_month",
        "description": "Sample timestamp (YYYY-MM) derived from its AndroZoo submission date.",
        "detection_signal": "The axis of LAMDA's concept-drift benchmark: enables time-aware splits, temporal generalization tests and drift measurement; not an input feature.",
    },
    {
        "column_name": "dataset_id",
        "description": "Provenance column added by the ingestion pipeline: the Hugging Face dataset id (IQSeC-Lab/LAMDA).",
        "detection_signal": "Lineage only.",
    },
    {
        "column_name": "config_name",
        "description": "Provenance column added by the ingestion pipeline: which LAMDA config (Baseline or var_thresh_0.01) the row came from.",
        "detection_signal": "Determines which feature mapping applies to the row's feat_* columns — feature ids are config-specific.",
    },
    {
        "column_name": "split_name",
        "description": "Provenance column added by the ingestion pipeline: LAMDA's published train/test split membership (80/20, stratified by label within each year).",
        "detection_signal": "Use the published split for comparable benchmarks; time-aware evaluations should split on year_month instead.",
    },
    {
        "column_name": "row_number",
        "description": "Provenance column added by the ingestion pipeline: row index within the source Parquet shard.",
        "detection_signal": "Lineage only.",
    },
    {
        "column_name": "source_file",
        "description": "Provenance column added by the ingestion pipeline: the Hub Parquet shard the row was read from.",
        "detection_signal": "Lineage only.",
    },
]


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def load_mapping(config: str, data_dir: Path | None) -> dict[str, str]:
    """Return {token -> feat_id} for a config, downloading the CSV if needed."""
    name = f"{config}/feature_mapping.csv"
    if data_dir and (data_dir / name).exists():
        path = data_dir / name
    else:
        from huggingface_hub import hf_hub_download

        path = Path(
            hf_hub_download(DATASET_ID, name, repo_type="dataset")
        )
    with open(path, newline="", encoding="utf-8") as fh:
        return {row["feature_name"]: row["mapped_name"] for row in csv.DictReader(fh)}


def build(data_dir: Path | None = None) -> dict:
    mappings = {config: load_mapping(config, data_dir) for config in CONFIGS}
    baseline = mappings["Baseline"]

    features = []
    for token, feat_id in sorted(baseline.items(), key=lambda kv: int(kv[1].split("_")[1])):
        category, _, item = token.partition("_")
        description, signal = describe_token(category, item)
        features.append(
            {
                "token": token,
                "category": category,
                "drebin_set": CATEGORY_INFO[category]["drebin_set"],
                "item": item,
                "feature_id_baseline": feat_id,
                "feature_id_var_thresh_0_01": mappings["var_thresh_0.01"].get(token),
                "description": description,
                "detection_signal": signal,
            }
        )

    # Sanity: every var_thresh token must exist in Baseline (verified upstream).
    missing = set(mappings["var_thresh_0.01"]) - set(baseline)
    if missing:
        raise RuntimeError(f"var_thresh_0.01 tokens absent from Baseline: {sorted(missing)[:5]}")

    categories = [
        {
            "category": name,
            "feature_count_baseline": sum(1 for f in features if f["category"] == name),
            "feature_count_var_thresh_0_01": sum(
                1 for f in features
                if f["category"] == name and f["feature_id_var_thresh_0_01"]
            ),
            **info,
        }
        for name, info in CATEGORY_INFO.items()
    ]

    return {
        "dataset_id": DATASET_ID,
        "dataset_paper": "https://arxiv.org/abs/2505.18551",
        "feature_methodology": (
            "Drebin-style static analysis (Arp et al., NDSS 2014): binary bag-of-words "
            "over tokens extracted from AndroidManifest.xml and classes.dex, "
            "vectorized then reduced with VarianceThreshold (0.001 for Baseline -> "
            "4,561 features; 0.01 -> 925 features). feat_i column ids are "
            "config-specific and map to tokens via feature_mapping.csv."
        ),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "generator_version": GENERATOR_VERSION,
        "configs": list(CONFIGS),
        "categories": categories,
        "metadata_columns": METADATA_COLUMNS,
        "features": features,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--data-dir", type=Path, default=None,
        help="Directory holding pre-downloaded <config>/feature_mapping.csv files.",
    )
    args = parser.parse_args()

    payload = build(args.data_dir)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=1, ensure_ascii=False)
        fh.write("\n")
    print(
        f"Wrote {len(payload['features'])} feature descriptions, "
        f"{len(payload['categories'])} categories and "
        f"{len(payload['metadata_columns'])} metadata columns to {args.output}"
    )


if __name__ == "__main__":
    main()
