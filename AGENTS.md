# Repository agent guidance

These instructions apply to the entire `snowflake-ml-pipeline` repository. The packages in `plugins/` provide
the shared skills and validators; their location does not limit their scope to that directory.

## Agent Code Intelligence

For code search, debugging, refactoring, or architecture work, read
`plugins/agent-code-intelligence/skills/codebase-intelligence-router/SKILL.md` and follow its retrieval hierarchy.
Start with exact local searches and targeted file reads; use structural, symbol, semantic, or graph tools when needed.
For memory work, use `plugins/agent-code-intelligence/skills/memory-router/SKILL.md` and retain only verified lessons.

## ML workflow

For ML feature work, use the relevant skill in `plugins/ml-feature-governance/skills/` and the policy in
`plugins/ml-feature-governance/policies/feature-contract.md`. Run validation from the repository root.
The initial `feature-platform/` definitions are the plugin's starter examples, not a verified mapping of the existing
LAMDA ingestion tables. Register actual source schemas and feature contracts before relying on them for pipeline work.

See `docs/plugins.md` for client setup and validation commands.

<!-- ml-feature-governance:start -->
## ML Feature Governance

When `feature-platform/` exists, it is the canonical semantic source of truth for ML features and model-input contracts.

- Use immutable `feature_id@version` references.
- Keep semantic types, logical/storage types, and model representations separate.
- Model raw and transformed values as distinct nodes in an acyclic dependency graph.
- Keep fitted state outside definitions and fit it only on an explicit training split.
- Require explicit point-in-time, availability-time, null, hashing, and window-boundary semantics.
- Keep engine-specific behavior in adapters and generated outputs; never directly edit `feature-platform/generated/`.
- Run `python .feature-platform/tools/featurectl.py validate --project-dir .` after canonical edits.
- Use versioned change contracts for semantic, backend, and migration changes.
- Require explicit user approval before implementing a compatibility-breaking migration.

The complete policy is in `feature-platform/docs/architecture-principles.md`.

For Iceberg work, read `feature-platform/docs/iceberg-metadata.md`: canonical types/nullability
are required contracts, actual schemas live in Iceberg, field IDs are table-scoped, and
resolved datasets pin snapshots independently of feature versions. Keep docs as prose
and normalization in separate fitted-transform nodes.
<!-- ml-feature-governance:end -->
