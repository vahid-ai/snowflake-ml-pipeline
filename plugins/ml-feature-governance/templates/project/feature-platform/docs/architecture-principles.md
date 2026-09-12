# ML Feature Platform Architecture Principles

These rules define the repository-level engineering contract for ML features and training/inference data. They apply
during design, implementation, validation, review, materialization, training, serving, and migration work.

## 1. One semantic source of truth

Feature meaning belongs in engine-neutral definitions under `feature-platform/`. Spark, Snowflake, dbt, DuckDB, Polars,
Ray, Dask, cuDF, NVTabular, Feast, and MLflow representations are implementations or generated artifacts.

## 2. Immutable versioned features

Every model-consumable raw or transformed feature has an immutable ID and integer version. Downstream consumers pin
`id@version`. A semantic change creates a new version.

## 3. Separate semantic, logical, and model types

Do not conflate feature meaning with its storage or tensor representation. Use explicit-width, encoding-aware canonical
types and explicit model dtypes/shapes/cardinalities.

## 4. Feature engineering is a typed DAG

Raw and transformed values are separate nodes with explicit dependencies. The graph must be acyclic and completely
resolvable before execution.

## 5. Portable semantics first

Prefer typed built-in operations or an engine-neutral IR. Backend-specific code belongs behind adapters/plugins with a
declared semantic-equivalence policy.

## 6. Fitted state is an artifact

Scalers, vocabularies, quantiles, imputers, PCA state, learned buckets, target encoders, and embedding models are fitted
on explicit data/splits and persisted separately from the immutable feature definition.

## 7. Point-in-time correctness is part of the contract

Entity keys, event time, availability time, windows, lag, late-data policy, and current-event inclusion/exclusion are
explicit whenever time affects feature values.

## 8. Cross-engine determinism is intentional

Hashing, encoding, null behavior, precision, overflow, timezone, regex, and window semantics must not depend on an
engine's undocumented/default behavior.

## 9. Models consume versioned feature sets

Feature sets pin exact feature versions and deterministic ordering. Experiment variants resolve to explicit immutable
manifests before training/evaluation.

## 10. Backends declare capability

Unsupported or lossy physical mappings fail before execution unless an explicit reviewed emulation exists.

## 11. Portability is tested, not assumed

Run golden-data differential tests across supported engines and normalize to the canonical Arrow-like schema before
comparison.

## 12. Reproducibility pins all mutable inputs

Training/evaluation manifests should pin source snapshots, split versions, feature-set versions, fitted artifacts,
backend profiles, code/environment versions, and deterministic seeds.

## 13. Generated outputs are derived

Generated SQL/dbt/backend schemas/Feast definitions/MLflow signatures do not become a second semantic source of truth.

## 14. Breaking semantics require explicit human approval

Compatibility-impacting changes must surface affected feature versions/consumers plus migration, backfill, or dual-read
strategy. Record the decision in a change contract and obtain explicit user approval before implementation or rollout.

## Rule MLFEAT-15: Iceberg physical metadata and independent data versions

Canonical types/nullability remain the required contract; Iceberg describes the actual
persisted schema. Adapters must validate their compatibility through explicit physical
mappings. Keep concise prose in field doc, and ML semantics/governance in the registry.
Bind materialized features by table identity plus field ID; verify name-based consumers
on rename. Pin every resolved Iceberg training input by table plus snapshot independently
from feature-set versions, and preserve the snapshots needed for replay. Keep normalized
features in separate fitted DAG nodes. See feature-platform/docs/iceberg-metadata.md for
examples and the boundary between static checks and catalog integration checks.
