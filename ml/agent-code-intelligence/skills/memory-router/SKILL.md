---
name: memory-router
description: Decide where durable coding-agent knowledge should live: native Claude/Codex memory, local verified lessons, Mem0, Graphiti, Cognee, or Letta, while preventing redundant or unverified memory writes.
---

# Memory router

Memory is separate from source-code retrieval. Never use memory as a substitute for checking current source when the code may have changed.

## Hierarchy

### 1. Host-native memory first

Use Claude Code or Codex native memory for lightweight project/user learnings that the host can carry across sessions. Prefer this when only one host needs the fact and no external memory service is required.

### 2. Plugin local verified lessons

Use `agent-memory` for small, local, repo-scoped lessons when no external memory backend is configured. Store only verified lessons, not raw conversation history.

### 3. Mem0

Use for simple cross-agent/cross-host factual memory and semantic recall: build quirks, conventions, recurring fixes, user/team preferences, or reusable engineering facts.

### 4. Graphiti

Use when relationships and time matter: "service A depended on B after migration C", superseded decisions, incident→cause→fix relationships, or evolving architecture.

### 5. Cognee

Use when building a broader engineering knowledge system across documents, code-related notes, tickets, incidents, design docs, and extracted entities/relationships.

### 6. Letta

Use only when the architecture calls for a persistent stateful agent/harness whose identity, skills, and memory evolve over long horizons. Do not add Letta merely to remember a few project facts.

## What belongs in durable memory

Good:

- a verified non-obvious build/test command;
- a confirmed project constraint not obvious from code;
- a root cause + verified fix likely to recur;
- an architectural decision and why it exists;
- a stable workflow preference.

Bad:

- speculative diagnoses;
- temporary branch state;
- secrets/tokens/passwords;
- copied source code that can be retrieved from the repo;
- facts that are likely to become stale without a timestamp/source.

Use the `learn-from-failure` skill before promoting failure-derived knowledge.
