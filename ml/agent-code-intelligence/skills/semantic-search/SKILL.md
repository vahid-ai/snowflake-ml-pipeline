---
name: semantic-search
description: Use GrepAI semantic search and call tracing only when the code concept is known but exact identifiers are not, or when semantic retrieval can replace broad exploratory file reading.
---

# Semantic code search with GrepAI

Before using semantic search, try cheap exact terms if the likely vocabulary is obvious.

Use GrepAI for conceptual queries such as:

```bash
grepai search "user authentication flow"
grepai search "where expired sessions are renewed"
grepai trace callers "Login"
grepai trace callees "refreshSession"
```

MCP mode is `grepai mcp-serve` when configured.

After search returns candidates, inspect only the best-ranked ranges/symbols. Do not use semantic search as permission to read every result.

Use LSP/Serena instead if the exact symbol is known. Use CodeGraph/code-review-graph when transitive impact or multi-hop dependency reasoning is the core question.

Check `grepai status` when results look stale or empty; do not repeatedly query a broken index.
