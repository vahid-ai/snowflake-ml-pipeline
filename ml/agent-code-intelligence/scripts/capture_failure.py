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

root = os.getenv("PLUGIN_DATA") or os.getenv("CLAUDE_PLUGIN_DATA")
if not root:
    root = str(Path.home() / ".agent-code-intelligence")
out = Path(root)
out.mkdir(parents=True, exist_ok=True)

secret_patterns = [
    (re.compile(r"(?i)(authorization:\\s*bearer\\s+)[^\\s]+"), r"\\1<REDACTED>"),
    (re.compile(r"(?i)(api[_-]?key|token|password|secret)(\\s*[=:]\\s*)[^\\s,;]+"), r"\\1\\2<REDACTED>"),
    (re.compile(r"sk-[A-Za-z0-9_-]{12,}"), "<REDACTED_KEY>"),
]

def redact(s):
    s = str(s or "")[:6000]
    for pat, repl in secret_patterns:
        s = pat.sub(repl, s)
    return s

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
