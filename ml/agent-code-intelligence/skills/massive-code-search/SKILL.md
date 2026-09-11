---
name: massive-code-search
description: Use Zoekt indexed source search for very large monorepos or many repositories when recursive ripgrep would repeatedly scan too much data.
---

# Massive code search with Zoekt

Use Zoekt only when a Zoekt index exists or the corpus is large enough that building one is justified.

Typical local usage:

```bash
zoekt-git-index -index ~/.zoekt /path/to/repo
zoekt 'hello'
zoekt 'hello file:README'
```

Use `rg` for small/medium repositories or one-off searches. Zoekt is deterministic indexed text/symbol search, not semantic search. If the concept cannot be expressed as text/query syntax, use GrepAI after exhausting likely exact terms.
