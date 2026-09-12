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
