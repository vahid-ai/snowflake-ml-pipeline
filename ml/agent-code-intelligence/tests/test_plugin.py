#!/usr/bin/env python3
from pathlib import Path
import json, re, subprocess, sys, tempfile, os

ROOT = Path(__file__).resolve().parents[1]

for rel in [".claude-plugin/plugin.json", ".codex-plugin/plugin.json", "hooks/hooks.json", "config/integrations.json", "config/profiles.json"]:
    json.loads((ROOT/rel).read_text())

for manifest in [".claude-plugin/plugin.json", ".codex-plugin/plugin.json"]:
    data = json.loads((ROOT/manifest).read_text())
    assert data["name"] == "agent-code-intelligence"
    assert data["version"] == "1.1.0"
    assert data["skills"] == "./skills/"

agent = (ROOT / "agents" / "codebase-intelligence.md").read_text()
assert agent.startswith("---\n")
assert "\nname: codebase-intelligence\n" in agent
assert "\nmodel: haiku\n" in agent
assert "\n  - agent-code-intelligence:codebase-intelligence-router\n" in agent
assert "\n  - Write\n" in agent and "\n  - Edit\n" in agent

skills = list((ROOT/"skills").glob("*/SKILL.md"))
assert len(skills) >= 10
for p in skills:
    text = p.read_text()
    assert text.startswith("---\n")
    assert re.search(r"\nname: [a-z0-9-]+\n", text)
    assert "\ndescription: " in text

# Ensure profiles only reference known integrations.
ints = json.loads((ROOT/"config/integrations.json").read_text())
profiles = json.loads((ROOT/"config/profiles.json").read_text())
for names in profiles.values():
    assert all(n in ints for n in names)

# Smoke-test host-specific rendering and route helpers.
for host in ["claude-code", "codex"]:
    rendered = subprocess.check_output(
        [sys.executable, str(ROOT/"scripts/render_mcp_config.py"), "minimal", host],
        text=True,
    )
    serena = json.loads(rendered)["mcpServers"]["serena"]
    assert serena["command"] == "serena"
    assert serena["args"] == ["start-mcp-server", "--project-from-cwd", f"--context={host}"]
subprocess.run([sys.executable, str(ROOT/"scripts/route.py"), "symbol"], check=True, stdout=subprocess.DEVNULL)
print(f"ok: {len(skills)} skills, manifests/config/scripts validated")
