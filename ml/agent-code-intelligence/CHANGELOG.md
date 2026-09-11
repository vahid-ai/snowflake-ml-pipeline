# Changelog

## 1.1.0 — 2026-08-18

- Added a read-only Haiku codebase-intelligence subagent for automatic search and understanding
  delegation in Claude Code.
- Preloaded the codebase-intelligence-router skill into the subagent.

## 1.0.1 — 2026-08-18

- Replaced Serena's source-based `uvx` launch with the installed `serena-agent` CLI.
- Added host-specific Serena contexts for Claude Code and Codex profile rendering.
- Updated setup guidance to require Python 3.13 installation and `serena init`.

## 1.0.0 — 2026-08-11

- Initial dual-host Claude Code + Codex plugin manifests.
- Added token-efficient codebase intelligence routing hierarchy.
- Added skills for exact CLI search, ast-grep, LSP/JetBrains/Serena, Context7/DeepWiki, GrepAI, Zoekt, CodeGraph/code-review-graph, Repomix, and memory systems.
- Added native/local/Mem0/Graphiti/Cognee/Letta memory routing.
- Added verified learn-from-failure promotion policy.
- Added opt-in sanitized PostToolUseFailure capture hook.
- Added local SQLite lesson store and toolchain doctor.
- Added conservative MCP profile renderer instead of auto-starting every server.
