# Routing matrix

| Need | First choice | Escalate to | Avoid by default |
|---|---|---|---|
| Find filename | fd | rg --files | semantic search |
| Exact string/error/config | rg | Zoekt for huge corpus | embeddings |
| JSON/YAML field | jq/yq | small targeted read | full payload dump |
| Syntax shape/codemod | ast-grep | compiler-assisted refactor | regex-only rewrite |
| Definition/reference/type | native LSP | JetBrains / Serena | whole-file scans |
| Current library docs | Context7 | official web/docs | model-memory guessing |
| Public repo architecture | DeepWiki | local source/LSP | treating wiki as source of truth |
| Unknown identifier, known concept | GrepAI | CodeGraph hybrid search | blind file traversal |
| Hundreds of repos | Zoekt | semantic layer | recursive grep on every query |
| Blast radius/dependency path/tests | CodeGraph / code-review-graph | focused source reads | graph for trivial one-hop query |
| One-shot repo snapshot | Repomix | selected subtree packing | repeated whole-repo packing |
| Small durable lesson | native/local memory | Mem0 | graph memory |
| Temporal relationships | Graphiti | Cognee | flat note dump |
| Enterprise/project knowledge corpus | Cognee | custom KG/RAG | raw chat memory |
| Persistent evolving agent | Letta | custom harness | using it for a few facts |
