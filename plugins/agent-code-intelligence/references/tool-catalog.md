# Tool catalog and role

Verified against upstream documentation on 2026-08-11.

## Local CLI primitives

- ripgrep (`rg`) — exact text/regex search.
- fd — file discovery.
- ast-grep (`sg`) — AST pattern search and rewrite.
- jq — JSON filtering/transformation.
- yq — YAML filtering/transformation.
- fzf — interactive fuzzy selection.

## Symbol / IDE intelligence

- Host-native LSP plugins — definitions, references, types, diagnostics.
- JetBrains MCP — use an already-indexed IntelliJ/Android Studio/PyCharm project.
- Serena — portable MCP semantic symbol retrieval/editing.

## Documentation / repo knowledge

- Context7 — current/versioned library docs.
- DeepWiki — public repository wiki/Q&A.
- Repomix — AI-friendly repository packing and MCP.

## Semantic/indexed retrieval

- GrepAI — local semantic code search + call tracing; MCP command: `grepai mcp-serve`.
- Zoekt — trigram-indexed source search for large repo sets.

## Graph tools

- CodeGraph — semantic/structural graph MCP, optional graph-only/core profiles.
- code-review-graph — local-first structural review/blast-radius graph.

## Memory

- Native Claude/Codex memory — first choice for host-local lightweight learning.
- Local plugin memory — simple SQLite verified-lesson fallback.
- Mem0 — cross-agent factual/semantic memory.
- Graphiti — temporal knowledge graph memory.
- Cognee — broader AI memory/knowledge graph across documents and entities.
- Letta — stateful long-lived agent harness; not merely a memory database.
