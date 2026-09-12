---
name: graph-impact-analysis
description: Use CodeGraph or code-review-graph for blast radius, dependency/call paths, related tests, PR impact, and multi-hop structural reasoning after exact/LSP retrieval becomes insufficient.
---

# Graph impact analysis

Graph tools are an escalation layer, not the default search path.

## Use CodeGraph when

- you need structured callers/callees/dependency traversal;
- you need related tests or implementation discovery across modules;
- a compact AI context or PR context can replace many file reads;
- graph-only/core profiles can answer without embeddings.

Prefer a narrow tool profile such as `core` or `graph` when supported; large MCP tool surfaces themselves consume context.

## Use code-review-graph when

- the task is specifically review/blast-radius oriented;
- you need minimal changed-file context, affected functions, tests, or incremental graph updates;
- the repo is large enough that graph maintenance pays for itself.

Do not use graph tooling for a tiny repository or a one-hop reference already available through LSP. Measure token savings rather than assuming a graph is always cheaper.

After graph analysis identifies affected files/symbols, return to precise local reads and focused tests.
