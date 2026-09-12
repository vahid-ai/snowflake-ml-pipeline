#!/usr/bin/env python3
"""Small deterministic routing helper for humans/agents."""
import argparse

p = argparse.ArgumentParser()
p.add_argument("need", choices=["file", "exact", "structured", "syntax", "symbol", "docs", "public-repo", "concept", "huge-corpus", "impact", "snapshot", "memory", "temporal-memory", "knowledge-memory", "stateful-agent"])
a = p.parse_args()
route = {
 "file": "fd -> rg --files",
 "exact": "rg -> Zoekt only for huge indexed corpora",
 "structured": "jq/yq -> targeted read",
 "syntax": "ast-grep -> targeted source read",
 "symbol": "native LSP -> JetBrains MCP if warm -> Serena -> compiler/type checker",
 "docs": "Context7 -> authoritative upstream docs if needed",
 "public-repo": "DeepWiki -> local rg/LSP for exact evidence",
 "concept": "likely rg terms -> GrepAI -> targeted symbol/range reads",
 "huge-corpus": "Zoekt -> GrepAI only if concept cannot be expressed textually",
 "impact": "LSP for one hop -> CodeGraph/code-review-graph for multi-hop blast radius",
 "snapshot": "Repomix with strict exclusions",
 "memory": "native memory -> local verified lesson -> Mem0",
 "temporal-memory": "Graphiti",
 "knowledge-memory": "Cognee",
 "stateful-agent": "Letta"
}
print(route[a.need])
