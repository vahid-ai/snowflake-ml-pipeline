---
name: architecture
description: Enforce the ML feature-platform architecture whenever designing, editing, reviewing, or implementing feature engineering, training datasets, model inputs, feature stores, schemas, backend adapters, data transformations, or ML data pipelines.
---

# ML Feature Architecture Governance

Apply these rules as binding constraints whenever the task touches ML features or training/inference data.

## Source-of-truth rule

`feature-platform/` is the canonical source of truth. Engine-specific dbt, Spark, Snowflake, DuckDB, Polars, Ray,
Dask, cuDF, NVTabular, Feast, and MLflow outputs are generated/adapter artifacts. Do not make a generated artifact the
semantic owner of a feature.

## Required feature identity

Every model-consumable raw or transformed feature MUST have:

- immutable `id` plus integer `version`;
- `semantic_type`;
- explicit output logical type and nullability;
- explicit model representation when it differs from storage;
- explicit dependencies;
- deterministic transformation semantics;
- validation constraints and missing/invalid behavior when applicable.

Reference a feature as `id@version`. Do not use a bare feature name in a feature set, experiment, or downstream model
contract.

## Type separation

Keep these concepts separate:

1. semantic type — continuous, categorical, binary, embedding/vector, timestamp, text, identifier;
2. logical/storage type — Arrow-like explicit-width type such as `int32`, `uint16`, `float32`, `float64`, `utf8`,
   `timestamp[us, UTC]`, `fixed_size_list<float32, 384>`;
3. model representation — scalar/tensor/index layout, dtype, shape, cardinality, ordering.

Never rely on ambiguous canonical names such as `int`, `long`, `double`, or language-native `string`.

## DAG rule

Represent raw and transformed values as separate nodes. Do not mutate a feature's meaning in place.

Good:

```text
network.bytes_sent_raw@1
network.bytes_sent_log1p@1
network.bytes_sent_standardized@3
```

Bad:

```text
bytes_sent = sometimes raw, sometimes normalized depending on pipeline flags
```

## Transformation tiers

Prefer, in order:

1. typed portable built-in operators;
2. engine-neutral relational/expression IR (for example an internal AST, Ibis, or Substrait where appropriate);
3. named plugin operation with backend implementations and an equivalence policy.

Canonical definitions MUST NOT contain arbitrary Spark SQL, Snowflake SQL, Python UDF code, Polars expressions, CUDA
kernels, or dataframe-specific expressions. Backend-specific code belongs in adapters/plugins.

## Stateful transform rule

Scalers, vocabularies, quantiles, imputers, encoders, PCA, learned buckets, target encoding, and embedding models have
state. The feature definition describes how the state is fitted. Fitted values live in immutable artifacts with:

- artifact/version identifier;
- training split;
- source dataset/snapshot identifier;
- code/environment version;
- parameters/model reference;
- output schema.

Do not embed current means, standard deviations, vocabularies, or quantiles in the canonical feature definition.

## Leakage and time rule

For event/time-dependent features, explicitly define:

- entity/join keys;
- event time;
- availability/ingestion time when relevant;
- window direction and boundaries;
- point-in-time requirement;
- allowed lag;
- late-arrival policy.

A training example MUST NOT observe data that was unavailable at prediction time. Rolling aggregates must state whether
the current event is excluded.

## Determinism rule

Portable hashing must define algorithm, seed, byte encoding, signedness/modulo behavior, bucket count, and null bucket.
Never use an engine/runtime's default `hash()` as a cross-backend contract.

## Feature-set rule

Model feature sets are versioned objects independent from feature definitions. They MUST pin exact feature versions and
preserve deterministic ordering. Experiments derive resolved immutable feature-set manifests before training.

## Backend rule

Each backend adapter declares capability and physical mappings. Unsupported behavior fails before execution. Never
silently:

- promote precision;
- narrow integers;
- reinterpret unsigned values;
- change timezone semantics;
- change null ordering;
- change regex/hash behavior;
- alter window boundaries;
- alter embedding shape.

## Testing rule

Cross-engine support requires golden-data differential tests. Normalize outputs to Arrow-compatible semantics before
comparison and declare exact/tolerance equivalence per feature.

Include edge cases: nulls, UTF-8/non-ASCII, unknown categories, integer extrema, NaN/infinity, duplicate timestamps,
late arrivals, empty strings, empty vectors, and timestamp boundaries.

## Native governance workflow

Use this plugin's provider-neutral change workflow directly. Establish a clean validation baseline, record a compact change contract for
semantic work, update canonical definitions before implementations, validate generated outputs, and run independent
architecture, leakage, and portability reviews where relevant.

Breaking compatibility changes require an explicit migration strategy and user approval. Approval to inspect, plan,
diagnose, or review is not approval to implement or roll out a breaking change.

Run the machine validator after canonical feature edits:

```bash
python .feature-platform/tools/featurectl.py validate --project-dir .
```

## Iceberg metadata

For Iceberg work, read `feature-platform/docs/iceberg-metadata.md` in the project (or
`templates/project/feature-platform/docs/iceberg-metadata.md` from the plugin root before initialization).
Apply the physical-schema, field-identity, documentation, and snapshot contract.
