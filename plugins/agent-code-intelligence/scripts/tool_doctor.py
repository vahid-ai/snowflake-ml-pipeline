#!/usr/bin/env python3
import shutil, json, os, platform

checks = [
    ("rg", "exact search"),
    ("fd", "file discovery"),
    ("sg", "ast-grep structural search"),
    ("ast-grep", "ast-grep alternate binary"),
    ("jq", "JSON filtering"),
    ("yq", "YAML filtering"),
    ("fzf", "interactive fuzzy selection"),
    ("serena", "Serena MCP (install with `uv tool install -p 3.13 serena-agent`)"),
    ("npx", "Context7 / Repomix launcher"),
    ("grepai", "semantic code search"),
    ("zoekt", "large-corpus indexed search"),
    ("zoekt-git-index", "Zoekt indexing"),
    ("codegraph-server", "CodeGraph MCP"),
    ("code-review-graph", "review graph"),
    ("mem0", "Mem0 CLI"),
    ("cognee-cli", "Cognee CLI"),
    ("docker", "Graphiti/Cognee container runtime")
]
rows = []
for cmd, role in checks:
    path = shutil.which(cmd)
    rows.append({"command": cmd, "available": bool(path), "path": path, "role": role})

# Treat sg/ast-grep as one capability.
print(f"Agent Code Intelligence doctor | {platform.system()} {platform.machine()}")
print("-" * 78)
for r in rows:
    mark = "OK" if r["available"] else "--"
    print(f"{mark:>2}  {r['command']:<20} {r['role']:<34} {r['path'] or ''}")
print("\nNotes:")
print("- JetBrains MCP is IDE-managed and cannot be proven available from PATH alone.")
print("- DeepWiki is remote and requires no local binary.")
print("- Graphiti/Cognee/Letta may require separate service configuration beyond a binary check.")
print("- Missing optional tools are not errors; the router should fall back to cheaper available layers.")
