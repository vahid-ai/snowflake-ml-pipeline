---
name: repository-packing
description: Use Repomix to create a bounded AI-friendly repository snapshot for one-shot review, model handoff, remote-repo analysis, or archival context, not as the default iterative coding retrieval mechanism.
---

# Repository packing with Repomix

Use Repomix when the deliverable itself benefits from a self-contained repository snapshot.

Examples:

```bash
repomix
repomix --mcp
```

Good uses:

- initial one-shot architecture review;
- sending a selected repo/subtree to another model;
- creating a reproducible context artifact;
- remote repository packing through the MCP server.

Bad use: repacking and resending an entire repository on every edit loop. For iterative coding, use exact/LSP/semantic retrieval.

Always review exclusions and avoid packaging secrets, credential directories, generated outputs, or datasets unless explicitly needed.
