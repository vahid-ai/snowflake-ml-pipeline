# ML Feature Contract — Standalone Project Policy

This policy is mandatory for work involving ML features, training/validation/test datasets, feature engineering,
feature stores, model-input construction, or cross-engine data pipelines. The plugin's skills guide judgment; its hooks
and validator enforce deterministic portions of the contract.

## Rule MLFEAT-01: Canonical semantic source of truth

### Rule

Feature meaning MUST be owned by engine-neutral canonical definitions under `feature-platform/`. Backend-specific
representations are derived implementations.

### Verification

- Identify the canonical feature definition for every changed model input.
- Verify dbt/Spark/Snowflake/DuckDB/Polars/Ray/Dask/cuDF/NVTabular definitions do not independently redefine semantics.
- Block changes that patch generated output without updating the canonical source or generator.

## Rule MLFEAT-02: Immutable versioned feature identity

### Rule

Every raw or transformed model feature MUST have a stable ID and integer version. Downstream references MUST pin
`id@N`. Changing semantics requires a new version.

### Verification

- Reject bare feature names in feature sets and experiment manifests.
- Verify transform/window/hash/tokenizer/model changes do not silently retain an incompatible feature identity.

## Rule MLFEAT-03: Explicit three-layer typing

### Rule

Feature semantic type, logical/storage type, and model representation MUST be modeled separately when applicable.
Canonical primitive types MUST use explicit widths and encodings.

### Verification

- Reject ambiguous canonical types such as `int`, `long`, `double`, or unspecified `string`.
- Verify vector shape/dtype and categorical index/cardinality are explicit where applicable.

## Rule MLFEAT-04: Typed acyclic transformation graph

### Rule

Raw and transformed values MUST be distinct nodes in an acyclic dependency graph.

### Verification

- Resolve every dependency to an exact version.
- Detect missing dependencies and cycles.
- Reject pipeline flags that cause one feature identity to alternate between raw and transformed semantics.

## Rule MLFEAT-05: Portable semantics before backend code

### Rule

Canonical transforms MUST use typed portable operations or an engine-neutral IR when possible. Backend-specific code
MUST be isolated behind named adapter/plugin implementations with declared equivalence.

### Verification

- Reject embedded backend code in canonical feature transforms.
- For plugin transforms, verify an implementation map and equivalence policy exist.

## Rule MLFEAT-06: Fitted state is an immutable artifact

### Rule

Learned transform state MUST be fitted on an explicitly declared split and stored/versioned separately from the feature
definition.

### Verification

- Reject mutable means, scales, medians, vocabularies, categories, or quantiles embedded in canonical definitions.
- Verify state artifacts pin source snapshot, split, code/environment version, and output schema.

## Rule MLFEAT-07: Point-in-time correctness

### Rule

Time-dependent features MUST be computable using only information available at prediction time.

### Verification

- Verify entity keys, event time, availability time, window boundary, lag, and late-data policy.
- Verify rolling windows explicitly state current-event inclusion or exclusion.
- Block future-data joins and fitting on validation/test data unless explicitly part of a non-evaluation experiment.

## Rule MLFEAT-08: Deterministic hashing and text encoding

### Rule

Portable categorical hashing MUST specify algorithm, seed, byte encoding, bucket count, null behavior, and modulo or
signed semantics.

### Verification

- Reject engine/runtime default hash functions as canonical semantics.
- Verify non-ASCII and UTF-8 golden cases exist for string-derived features.

## Rule MLFEAT-09: Feature sets are independent versioned contracts

### Rule

Models MUST consume versioned feature sets that pin exact feature versions and deterministic order.

### Verification

- Verify model-input dtype, shape, and order can be generated from the feature set.
- Reject feature selection implemented only as notebook-local column lists or untracked conditionals.

## Rule MLFEAT-10: Backend capability failures are explicit

### Rule

Each backend MUST declare physical type and operation support. Unsupported or lossy mappings MUST fail before execution
unless a reviewed emulation is explicitly declared.

### Verification

- Check signed/unsigned, float precision, timezone, null, regex, hash, window, and nested/vector mappings.
- Reject silent coercion or semantic fallback.

## Rule MLFEAT-11: Differential semantic tests

### Rule

A semantic change or new backend MUST be covered by normalized golden-data differential tests.

### Verification

- Test nulls, non-ASCII strings, unknown categories, numeric extrema, NaN/infinity, duplicate timestamps, late arrivals,
  empty strings/vectors, and boundary timestamps as applicable.
- Compare exact values or declared tolerances after normalization to canonical Arrow-like semantics.

## Rule MLFEAT-12: Reproducible execution manifest

### Rule

Training and evaluation runs MUST be reproducible from immutable references.

### Verification

Pin, as applicable: source snapshot, split version, feature-set version, fitted artifact versions, backend profile, code
commit, environment lock, and deterministic seeds.

## Rule MLFEAT-13: Generated outputs are derived

### Rule

Generated dbt models, SQL, backend schemas, Feast definitions, MLflow signatures, and similar outputs MUST NOT be
hand-authored as competing feature semantics.

### Verification

- Trace generated artifacts back to canonical sources or generators.
- Ensure validation detects drift and direct edits to generated directories.

## Rule MLFEAT-14: Human approval for semantic compatibility changes

### Rule

Breaking changes to an existing feature contract, type mapping, point-in-time behavior, or model-input layout MUST be
recorded in a change contract and explicitly approved by the user before implementation or rollout.

### Verification

- List affected feature IDs, versions, feature sets, models, materializations, and serving consumers.
- Provide a migration, backfill, dual-read, or deprecation strategy.
- Do not infer approval from a general request to inspect, plan, diagnose, or review the change.

## Rule MLFEAT-15: Iceberg physical metadata and independent data versions

Canonical types/nullability remain the required contract; Iceberg describes the actual
persisted schema. Adapters must validate their compatibility through explicit physical
mappings. Keep concise prose in field doc, and ML semantics/governance in the registry.
Bind materialized features by table identity plus field ID; verify name-based consumers
on rename. Pin every resolved Iceberg training input by table plus snapshot independently
from feature-set versions, and preserve the snapshots needed for replay. Keep normalized
features in separate fitted DAG nodes. See feature-platform/docs/iceberg-metadata.md for
examples and the boundary between static checks and catalog integration checks.
