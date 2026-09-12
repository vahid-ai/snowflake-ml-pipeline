---
name: toolchain-doctor
description: Check which code-intelligence CLIs and optional integration prerequisites are installed before choosing a retrieval backend or diagnosing missing MCP/LSP tools.
---

# Toolchain doctor

Run the bundled doctor before configuring or debugging the toolkit:

```bash
python3 "${PLUGIN_ROOT:-$CLAUDE_PLUGIN_ROOT}/scripts/tool_doctor.py"
```

If neither variable is available, locate the installed plugin directory and run `scripts/tool_doctor.py` directly.

Interpretation:

- Missing `rg`/`fd`/`sg`/`jq`/`yq` affects the cheap local tiers.
- Missing `serena` affects Serena MCP. Install it with `uv tool install -p 3.13 serena-agent`,
  then run `serena init` before configuring a host profile.
- Missing `npx` affects Context7/Repomix launch profiles.
- Missing `grepai`, `zoekt`, `codegraph-server`, or `code-review-graph` means those optional escalation layers are unavailable.
- JetBrains MCP availability is controlled by the IDE, not just a shell binary.
- Graphiti/Cognee may require Docker and databases/services.

Do not install software automatically without user approval. Use the doctor to explain the smallest missing dependency for the requested capability.
