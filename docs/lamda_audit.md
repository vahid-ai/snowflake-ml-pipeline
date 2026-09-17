# LAMDA preflight audit

Every training run now completes an audit before creating a model candidate or
calling `fit`. The same audit can run on its own. It reads the complete selected
Iceberg snapshot, collects independent errors, and produces terminal diagnostics,
JSON, HTML EDA, and MLflow artifacts.

## Run it

From the repository root, with the existing Infisical project session:

```powershell
# Strict existing binary contract; exit 2 means errors were found.
infisical run --projectId=0cfed731-cdf4-46b8-b831-2d74be495575 --env=dev -- uv run --locked --extra ml python scripts/audit_lamda.py --snapshot-id 7499953728686860711 --output data/audit-strict

# Explicit count-to-presence semantics, including raw values such as 2.
infisical run --projectId=0cfed731-cdf4-46b8-b831-2d74be495575 --env=dev -- uv run --locked --extra ml python scripts/audit_lamda.py --feature-set lamda.malware_presence@1 --model autoencoder --output data/audit-presence

# The same audit runs automatically before training any supported model.
infisical run --projectId=0cfed731-cdf4-46b8-b831-2d74be495575 --env=dev -- uv run --locked --extra ml --extra lightning python scripts/train_lamda_malware.py --feature-set lamda.malware_presence@1 --model mlp --epochs 1 --output data/presence-mlp
```

Use `--local-root <directory>` with an existing local Iceberg catalog. Use
`--no-publish-observations` for read-only R2 credentials: reports and MLflow still
work, but generated raw YAML and the companion Iceberg table are not updated.
Otherwise, both CLI entry points automatically publish complete observations and
require catalog/object write access for the companion table. They never change
the source sample rows or source Iceberg schema.

Standalone `--max-rows N` creates an advisory sample and exits **3** even when no
errors are found. Samples cannot certify training or publish raw observations.
Full passing audits exit **0**; detected errors exit **2**. Training has no switch
to bypass its full audit. `--inspect-only` records schema/snapshot metadata only;
it does not certify values. `--examples 0` suppresses diagnostic row examples in
the standalone audit; otherwise at most three examples per diagnostic are kept.

## The three stages

| Stage | Authority | Behavior |
| --- | --- | --- |
| Raw observations | Actual selected snapshot | Profile all source columns, record actual types, field IDs, nullability, exact distinct counts, min/max, mean, population standard deviation and top values. Update generated observations after complete scans, even if a downstream binary contract fails. |
| Engineered features | Versioned canonical feature definitions | Execute the declared DAG and explicit operations, then check every node's output type, missing policy, domain, categories and bounds. No inferred recoding or fitted statistics. |
| Model inputs | Ordered feature set plus model requirements | Check CSR shape, float32 dtype, finite values, exact integer representation and feature order. Check the BCE autoencoder's additional `[0,1]` requirement. Trace each model index back to its raw field and transform. |

`lamda.malware_baseline@1` keeps the existing strict binary requirements. A raw
`feat_2247=2` therefore produces `BINARY_DOMAIN`, its YAML location, occurrence
count, bounded provenance examples, and the path to the model input.

`lamda.malware_presence@1` is an **opt-in new feature set**. Each column has two
separate immutable nodes:

```text
lamda.count.feat_2247@1       integer >= 0, nulls rejected
    positive_presence       1 if count > 0, otherwise 0
lamda.presence.feat_2247@1    binary integer, non-null
    model input index 2247  float32 scalar
```

Thus `[0,1,2,5]` becomes `[0,1,1,1]` only under this explicit contract. Negative
values and nulls still fail. Observations retain the original magnitudes; the
source table is not recoded. The operation assumes that any positive value means
token presence and intentionally discards magnitude. Select it only for that
modeling interpretation.

Supported deterministic scalar operations are `identity`, `cast`, `fill_null`,
`clip`, `log1p_nonnegative`, and `positive_presence`. Output constraints are checked
after the operation; an incorrect fill or clipping rule still fails. Unsupported
fitted transforms, categorical encoders, vector features, or multivariate DAG
operators produce a contract diagnostic and cannot silently run. Adding those
requires an adapter and governed definitions, including train-only fitted state.

## Diagnostics and EDA

Diagnostics distinguish missing columns, incompatible types, integer overflow,
unhandled nulls, NaN/infinity, binary/category/range violations, invalid metadata,
duplicate APKs, split overlap, missing split classes, unsupported contracts,
transformation failures, float32 precision loss, and model tensor mismatches.
Hash identity is normalized to lowercase before duplicate checking. Label,
identity, provenance and split metadata cannot be selected as model features.

Statistical extremes are warnings: a maximum more than eight population standard
deviations above the mean is flagged for inspection, except for binary columns.
This is a simple descriptive heuristic, not a robust anomaly detector. Crossing
an explicitly declared bound is an error. Neither warning nor error automatically
weakens an engineered requirement or fits a transformation on evaluation data.

Distinct counts are exact, exclude nulls, and use disk-backed aggregation when
cardinality grows. Top-value lists are bounded; APK hashes and source identifiers
are omitted from these lists. Local reports can include feature values and source
file/row provenance in diagnostics; those reports are uploaded to the configured
MLflow artifact store. Dataset rows, identity databases, and matrix caches are not.

## Generated observations and Iceberg metadata

The CLI generates raw-profile YAML under
`feature-platform/generated/raw_profiles/<selection-id>/`. Files include source
table UUID, snapshot ID, schema/field IDs, dataset/configuration, audit ID and the
contract fingerprint. Each completed audit gets an immutable history file;
`latest.yaml` is replaced atomically under a per-selection writer lock. “Latest”
means most recently audited, including an explicitly requested historical snapshot.
The audit report also records changes from the previous local observation.

`raw_lamda.lamda_feature_observations` is a companion Iceberg table. Each append
contains field observations and one completion record in a **single atomic
Iceberg commit**, followed by readback verification. The completion record carries
source and contract provenance. Consumers should select a completed `audit_id`
for the desired source UUID, snapshot and configuration; snapshot IDs are opaque,
not timestamps. The `observation_sha256` identifies the immutable payload.

Join Baseline `column_name=feat_N` to `feature_id_baseline=N` in the existing
`raw_lamda.lamda_feature_descriptions` table. Human descriptions and other metadata
remain intact. Actual observations are stored in this linked table rather than
replacing the existing dictionary or encoding configuration into Iceberg field
docs. Required feature YAML is not automatically rewritten to accept new values.

Complete scans with data errors can publish observations. Interrupted and sampled
scans cannot. Publication failures are explicit errors and block training when
publication is enabled. Retrying the same completed audit checks its immutable
payload before appending; concurrent independent audits have different IDs.

## Training, inference and resource limits

During training, audited transformed batches are written to local sparse shards.
The complete scan must pass before adapters receive training shards. The gate is
bound to source identity/snapshot, contract fingerprint, split policy and model;
an unrelated or partial certificate is rejected. Inference uses the exact frozen
feature definitions saved in the model artifact and reruns their value/tensor
checks. It does not read current YAML or require training-only labels.

Compressed Parquet files are streamed to temporary disk, then decoded without
Arrow dataset prefetch in bounded batches. Iceberg snapshot filtering, schema/field
mapping, partition defaults and positional deletes are retained. This avoids
materializing an entire wide file as the standard ArrowScan reader did. The
adapter uses PyIceberg helpers tested against the locked 0.11.1 version; dependency
upgrades must rerun the reader tests. Only Parquet data files are supported.

A passing audit proves the checked data contracts for that selected snapshot.
It cannot guarantee later network availability, free disk/RAM, GPU compatibility,
optimizer behavior, predictive quality, or recovery from a native process crash.
Python-level failures retain diagnostics and fail the MLflow run; a hard process
termination can leave a running status, never a completed certificate. Rerun into
a new output directory. Source snapshot retention remains an operational concern.

## Tests

```powershell
uv run --locked --extra ml --extra lightning python -m unittest discover -s tests -v
uv run --locked --extra ml python .feature-platform/tools/featurectl.py validate --project-dir .
```

Tests exercise deliberate values `2,3,4,5`, rare failures at the end of scans,
nulls with and without explicit fills, bad types/categories/ranges, float32
overflow and precision loss, DAG/layout errors, duplicate identities, split
contamination, interrupted scans, sample certificates, HTML escaping, metadata
history/idempotence/readback, and failures before any candidate exists. Golden
cases check training/inference agreement for the presence transform across SGD,
MLP and autoencoder models. Synthetic scores validate software behavior only.
