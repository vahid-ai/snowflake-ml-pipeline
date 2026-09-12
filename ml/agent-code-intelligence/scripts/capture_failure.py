#!/usr/bin/env python3
"""Optionally capture sanitized failed tool events for later verified-learning review.

Disabled unless AGENT_TOOLKIT_CAPTURE_FAILURES=1. This script never promotes a failure
into durable memory; it only stores a bounded, redacted lead in plugin-local data.
"""
import json, os, re, sys
from pathlib import Path
from datetime import datetime, timezone

if os.getenv("AGENT_TOOLKIT_CAPTURE_FAILURES") != "1":
    sys.exit(0)

try:
    event = json.load(sys.stdin)
except Exception:
    sys.exit(0)
if not isinstance(event, dict):
    sys.exit(0)

root = os.getenv("PLUGIN_DATA") or os.getenv("CLAUDE_PLUGIN_DATA")
if not root:
    root = str(Path.home() / ".agent-code-intelligence")
out = Path(root)
out.mkdir(parents=True, exist_ok=True)

SECRET_NAME = r"[\w.-]*(?:api[_-]?key|access[_-]?key|private[_-]?key|token|password|passwd|secret|credential|authorization)[\w.-]*"
SECRET_KEY = re.compile(SECRET_NAME, re.IGNORECASE)
ASSIGNMENT = re.compile(
    r"(?P<prefix>(?:(?<![\w.])[\"']?" + SECRET_NAME + r"[\"']?\s*[=:]\s*|--" + SECRET_NAME + r"\s+))"
    r'''(?:\\+["'][\s\S]*|[\[{][\s\S]*|"(?:\\.|[^"\\])*(?:"|$)|'(?:\\.|[^'\\])*(?:'|$)|[^\s,;}\]&]+)''',
    re.IGNORECASE,
)
# A quoted credential embedded in an escaped command fragment may not be a
# standalone JSON document. Omit the remaining fragment when its end is ambiguous.
ESCAPED_ASSIGNMENT = re.compile(
    r'''(?P<prefix>\\+["']''' + SECRET_NAME + r'''\\+["']\s*[=:]\s*)[\s\S]*''',
    re.IGNORECASE,
)
secret_patterns = [
    (ESCAPED_ASSIGNMENT, r"\g<prefix><REDACTED>"),
    (re.compile(r"(?i)(\b(?:bearer|basic)\s+)[^\s\"',;}]+"), r"\1<REDACTED>"),
    (ASSIGNMENT, r"\g<prefix><REDACTED>"),
    (re.compile(r"\b(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|(?:AKIA|ASIA)[A-Z0-9]{16})\b"), "<REDACTED_KEY>"),
]


def sanitize(value):
    if isinstance(value, dict):
        return {
            sanitize(str(key)): "<REDACTED>" if SECRET_KEY.fullmatch(str(key)) else sanitize(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [sanitize(item) for item in value]
    if not isinstance(value, str):
        return value
    # Decode JSON containers/strings before matching, including JSON escaped once
    # inside command output. This exposes quoted keys and Unicode-escaped names.
    try:
        decoded = json.loads(value)
    except (ValueError, TypeError):
        decoded = None
    if isinstance(decoded, (dict, list, str)) and decoded != value:
        return json.dumps(sanitize(decoded), ensure_ascii=False)
    if '\\"' in value:
        try:
            decoded = json.loads('"' + value + '"')
        except ValueError:
            pass
        else:
            if decoded != value:
                return sanitize(decoded)
    for pattern, replacement in secret_patterns:
        value = pattern.sub(replacement, value)
    return value

def redact(s):
    value = sanitize(s)
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False)
    # Truncate only after removing complete values, including long quoted secrets.
    return str(value or "")[:6000]

inp = event.get("tool_input") or {}
record = {
    "ts": datetime.now(timezone.utc).isoformat(),
    "session_id": redact(event.get("session_id", ""))[:200],
    "cwd": redact(event.get("cwd", ""))[:1000],
    "tool_name": redact(event.get("tool_name", ""))[:300],
    "command": redact(inp.get("command", ""))[:3000] if isinstance(inp, dict) else "",
    "error": redact(event.get("error", ""))[:4000],
    "verified": False
}
with (out / "failures.jsonl").open("a", encoding="utf-8") as f:
    f.write(json.dumps(record, ensure_ascii=False) + "\n")
