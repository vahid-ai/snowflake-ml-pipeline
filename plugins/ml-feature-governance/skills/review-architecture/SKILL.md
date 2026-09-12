---
name: review-architecture
description: Independently review canonical ML feature architecture, DAG design, versioning, fitted-state boundaries, feature-set contracts, and source-of-truth discipline.
---

# Canonical Feature Architecture Review

Review `feature-platform/` before implementation or generated code. Do not modify files during this pass.

Check for one canonical source of truth, immutable feature identities, explicit semantic/logical/model types, distinct raw
and transformed nodes, an acyclic dependency graph, separately versioned fitted artifacts, deterministic ordered feature
sets, reproducible run inputs, and protected generated outputs.

Return findings ordered by severity. Every blocking finding must identify the violated invariant, affected file or
feature, consequence, and smallest durable fix. Avoid choosing an execution engine unless the requirement demands it.

## Iceberg metadata

For Iceberg work, read `feature-platform/docs/iceberg-metadata.md` in the project (or
`templates/project/feature-platform/docs/iceberg-metadata.md` from the plugin root before initialization).
Apply the physical-schema, field-identity, documentation, and snapshot contract.
