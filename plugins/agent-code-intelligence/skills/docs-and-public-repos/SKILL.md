---
name: docs-and-public-repos
description: Retrieve current library/API documentation with Context7 and architecture/Q&A for unfamiliar public repositories with DeepWiki, then return to local code tools for precise implementation work.
---

# Documentation and public repository knowledge

## Context7

Use for version-sensitive framework/library/API questions. It is preferable to guessing from model memory or reading an entire installed dependency tree.

Good triggers:

- "What is the current API for ...?"
- "How does version X configure ...?"
- "Show an official usage example for this library."

Use the smallest relevant documentation request. Do not fetch broad docs if one API/topic is enough.

## DeepWiki

Use for public repository architecture, subsystem discovery, and repository-grounded questions when the local codebase is unfamiliar.

Typical flow:

1. Ask DeepWiki for architecture/subsystem guidance.
2. Identify likely files/symbols.
3. Switch back to `rg`, LSP/Serena, or AST search for exact local evidence.

Do not treat generated wiki summaries as authoritative over the source code for a precise edit.
