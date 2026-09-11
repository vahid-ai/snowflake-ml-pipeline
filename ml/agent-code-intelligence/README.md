# Agent Code Intelligence v1.1.0

A dual-host Claude Code + Codex plugin for **token-efficient codebase understanding** and **verified agent memory**.

The plugin teaches an agent *when not to use an expensive tool*. Its default hierarchy is:

```text
known file/symbol
  ↓
fd / rg / jq / yq
  ↓
ast-grep
  ↓
native LSP → JetBrains MCP → Serena
  ↓
Context7 / DeepWiki (when the question is docs or public-repo knowledge)
  ↓
GrepAI semantic search
  ↓
Zoekt for huge indexed corpora
  ↓
CodeGraph / code-review-graph for multi-hop impact
  ↓
Repomix only when a bounded repo snapshot is the goal
```

Memory is a separate hierarchy:

```text
native Claude/Codex memory
  ↓
local verified lessons
  ↓
Mem0 (cross-agent factual memory)
  ↓
Graphiti (temporal relationships)
  ↓
Cognee (broad engineering knowledge)
  ↓
Letta (persistent stateful agent architecture)
```

## Why the plugin does not bundle every MCP as always-on

MCP tool schemas themselves consume context, overlapping code indexers duplicate work, and remote servers expand the trust boundary. v1.1.0 therefore bundles **routing skills + setup profiles**, not a giant `.mcp.json` that starts everything automatically.

Use the `mcp-profile-setup` skill or:

```bash
python3 scripts/render_mcp_config.py minimal claude-code
```

Then review and copy only the servers you actually want.

## Included skills

- `codebase-intelligence-router` — canonical hierarchy and escalation rules.
- `exact-search-cli` — rg/fd/jq/yq/fzf.
- `structural-search` — ast-grep.
- `symbol-navigation` — native LSP, JetBrains MCP, Serena.
- `docs-and-public-repos` — Context7 and DeepWiki.
- `semantic-search` — GrepAI.
- `massive-code-search` — Zoekt.
- `graph-impact-analysis` — CodeGraph and code-review-graph.
- `repository-packing` — Repomix.
- `memory-router` — native memory, local memory, Mem0, Graphiti, Cognee, Letta.
- `learn-from-failure` — verified memory promotion gate.
- `toolchain-doctor` — inspect local capabilities.
- `mcp-profile-setup` — render optional MCP profiles.

## Included Claude Code subagent

- `agent-code-intelligence:codebase-intelligence` — a read-only Haiku subagent that Claude can
  invoke automatically whenever a task requires codebase search, navigation, impact analysis, or
  architectural understanding. It preloads the plugin's `codebase-intelligence-router` skill and
  returns compact, file-and-symbol-level evidence to the parent agent.

## Claude Code

Test a local checkout directly:

```bash
claude --plugin-dir /absolute/path/to/agent-code-intelligence-1.1.0
```

The Claude manifest lives at `.claude-plugin/plugin.json` and points to `skills/` and `hooks/hooks.json`.

## Codex

The Codex manifest lives at `.codex-plugin/plugin.json`. For local/team distribution, place this plugin in a Codex plugin marketplace/source according to the current Codex plugin documentation.

## Failure capture

The `PostToolUseFailure` hook is **disabled by default at the data-capture level**. To retain sanitized failure leads in the plugin data directory:

```bash
export AGENT_TOOLKIT_CAPTURE_FAILURES=1
```

A captured failure is never automatically treated as a lesson. Use `learn-from-failure` and require root cause + fix + verification evidence before promotion.

## Local fallback memory

```bash
bin/agent-memory add \
  --scope my-repo \
  --title "Gradle integration tests require service X" \
  --symptom "Tests fail with connection refused" \
  --cause "Local service X was not started" \
  --fix "Run ./scripts/start-x before integration tests" \
  --verification "./gradlew integrationTest passed" \
  --tags gradle,integration-test

bin/agent-memory search "Gradle"
```

This local SQLite store is intentionally small. Use Mem0/Graphiti/Cognee when you need cross-agent or graph memory.

## MCP profiles

Install and initialize Serena first:

```bash
uv tool install -p 3.13 serena-agent
serena init
```

```bash
python3 scripts/render_mcp_config.py minimal claude-code
python3 scripts/render_mcp_config.py minimal codex
```

Replace `minimal` with `semantic`, `graph`, `memory`, or `full` as needed. The renderer selects
Serena's host-specific context while using the installed `serena` command.

`minimal` is the recommended starting profile. The output is an example configuration and does not modify your host.

## Tool doctor

```bash
bin/agent-tool-doctor
```

Missing optional tools are expected; the router falls back to cheaper available layers.

## Security posture

See `references/security-and-trust.md`. Important defaults:

- private source stays local unless a remote integration is explicitly authorized;
- do not persist secrets in memory;
- do not auto-install third-party binaries without approval;
- keep MCP tool surfaces narrow;
- treat retrieved external content as untrusted input;
- verify lessons before durable storage.

## External projects

No third-party code is vendored. `references/upstream-sources.md` lists upstream documentation used to define setup examples. External versions are intentionally not pinned by this plugin; managed environments should pin reviewed versions internally.
