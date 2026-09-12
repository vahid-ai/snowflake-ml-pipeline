---
name: codebase-intelligence-router
description: Route any codebase exploration, debugging, refactoring, architecture, dependency, documentation, or impact-analysis task to the cheapest and most precise available search/code-intelligence tool before reading large files or using expensive semantic/graph retrieval.
---

# Codebase intelligence router

Use this skill whenever the task requires finding, understanding, navigating, explaining, changing, debugging, or reviewing code.

## Primary objective

Minimize total context, tool calls, latency, and duplicated retrieval while preserving correctness. Do not use a more expensive retrieval layer merely because it is available.

## Mandatory retrieval hierarchy

Use the first layer that can answer the question. Escalate only when the current layer is insufficient.

### Layer 0 — direct knowledge and file targeting

If the exact file or symbol is already known, inspect only the smallest relevant range or symbol body. Do not rediscover known locations.

### Layer 1 — cheap deterministic CLI search

Use these first for literal facts:

- `fd`: find candidate files/directories by name or extension.
- `rg`: exact text, regex, filenames containing a known token, import/use sites, config keys, error strings.
- `jq`: slice/filter JSON before showing it to the model.
- `yq`: slice/filter YAML before showing it to the model.
- `fzf`: optional interactive narrowing when a human is selecting from many candidates; do not require it for autonomous execution.

Prefer several narrow `rg` queries over reading entire directories.

### Layer 2 — structural syntax search

Use `ast-grep` / `sg` when the target is a code shape rather than a literal string: call expressions, declarations, argument patterns, unsafe API forms, migration candidates, or syntax-aware rewrites.

Do not use regex for a structural refactor when an AST pattern can express it safely.

### Layer 3 — symbol/type intelligence

Use an LSP or IDE-backed symbol tool when the question is about definitions, references, implementations, types, diagnostics, inheritance, or call hierarchy.

Preference order:

1. Existing host-native LSP/code-intelligence tool already active for the workspace.
2. JetBrains MCP when IntelliJ IDEA / Android Studio / PyCharm / another JetBrains IDE already has the project indexed and its MCP server is available.
3. Serena MCP for portable symbol-oriented navigation/editing across Claude Code and Codex.
4. Compiler/type-checker CLI if the question is specifically whether the code type-checks/builds.

Do not launch a second indexer if an already-warm index can answer the same question.

### Layer 4 — authoritative external documentation or public-repo knowledge

Use `Context7` for current, version-specific library/framework/API documentation.

Use `DeepWiki` for high-level architecture and Q&A about unfamiliar public repositories. After DeepWiki identifies likely files/subsystems, return to local exact/LSP tools for precise code changes.

### Layer 5 — semantic code search

Use `GrepAI` when you know the concept but not the identifier, for example "where is session renewal handled?" and literal/symbol search is not enough.

Before semantic search, try one or two likely exact searches if terminology is predictable. After semantic search finds candidates, inspect only the returned symbols/ranges.

### Layer 6 — large-corpus indexed text search

Use `Zoekt` instead of recursive grep when searching a very large monorepo or many repositories where a prebuilt Zoekt index exists. It is still deterministic text/symbol search; do not treat it as semantic retrieval.

For a small or medium single repo, prefer `rg` because maintaining a Zoekt index may cost more than it saves.

### Layer 7 — graph / blast-radius analysis

Use CodeGraph or code-review-graph when the question fundamentally requires relationships across many files: callers/callees, dependency paths, impact/blast radius, related tests, PR context, cross-module traversal, or architectural graph queries.

Prefer graph-only/core profiles when semantic embeddings are unnecessary. For small repos or one-hop relationships already answerable by LSP, do not pay graph indexing/tool-surface overhead.

### Layer 8 — repository packing

Use Repomix when a bounded snapshot is the goal: one-shot architecture analysis, model handoff, remote repo packaging, or a portable review artifact.

Do not repeatedly pack and resend the whole repo in an iterative editing loop. Surgical retrieval is cheaper.

## Cross-layer stop rules

- Do not call two overlapping tools just to confirm each other unless the result is ambiguous or high stakes.
- Do not read a whole file when a symbol/range is sufficient.
- Do not read generated files, vendored dependencies, lockfiles, build outputs, or large structured files without first narrowing them.
- Do not start embeddings or graph indexing for a task that `rg`, AST search, or LSP can answer.
- Do not use DeepWiki or external services for private repository contents unless the user has explicitly configured/authorized that service for the repository.
- Prefer local tools for source code when privacy matters.

## Tool selection shortcuts

- "Where is exact string/error/config X?" → `rg`.
- "Which files are Kotlin/Python/tests/auth-related?" → `fd` + `rg`.
- "Find calls shaped like X" → `ast-grep`.
- "Who references/implements this symbol?" → LSP / JetBrains / Serena.
- "How does library version X use API Y?" → Context7.
- "Explain architecture of public repo X" → DeepWiki, then local tools.
- "I know the concept, not the name" → GrepAI.
- "Search hundreds of repos" → Zoekt.
- "What breaks if I change this?" → CodeGraph / code-review-graph.
- "Give another model a repo snapshot" → Repomix.
- "Remember a verified project lesson" → memory-router + learn-from-failure.

## Retrieval budget discipline

Start narrow. A good sequence is often:

1. One `fd` or `rg` query.
2. One structural or symbol query if needed.
3. Read 1–3 small relevant regions.
4. Edit.
5. Verify with focused tests/type checks.

If more than roughly 5–8 exploratory reads are happening without convergence, stop and escalate one level instead of continuing blind file traversal.
