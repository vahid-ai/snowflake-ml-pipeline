# Iceberg metadata contract

Canonical feature definitions own the required logical types, nullability, ML semantics,
feature IDs and versions. Iceberg owns the actual persisted schema and data versions.
Adapters must compare the actual schema with the required contract before reading or
materializing, using declared physical type mappings and range/shape constraints.
Do not silently rewrite the canonical contract to match physical drift. For example,
canonical uint16 needs an explicit checked mapping to Iceberg int; it is not a native
Iceberg unsigned type. Canonical source columns describe required logical inputs.

## Metadata placement

| Metadata | Owner |
| --- | --- |
| Required logical type/nullability, transforms, model layout | Canonical feature contract |
| Actual column name/type/nullability, field IDs, partitions, schema evolution | Iceberg table metadata |
| Persisted human description | Iceberg field doc |
| Semantic types, entities, owner, tags, PII classification | Canonical registry/catalog |
| Data version | Table identity plus Iceberg snapshot ID |
| Feature meaning/version | Canonical id@version |

Iceberg fields have a human-readable doc, not an arbitrary per-field key/value map.
Use concise prose; never serialize ML configuration into doc. Table properties are
available for table-wide metadata, but do not replace the canonical feature registry.
A canonical description can supply a desired doc during materialization; report drift
and reconcile it explicitly. Preserve other catalog metadata and external lineage.

## Physical bindings

An optional feature storage binding describes a materialized field. Assign real IDs
from the catalog; never invent IDs for an unmaterialized feature. Field IDs are scoped
to a table, not global feature identities. Use a catalog-qualified table identifier;
record the table UUID when available to distinguish drop/recreate at the same name.
Column names are readable lookup hints: a rename must preserve the field identity and
must also be checked against downstream name-based consumers. Do not repurpose a field
ID for a different meaning or assume a dropped/recreated field keeps its identity.

```yaml
storage:
  format: iceberg
  table: prod.ml.device_features
  column: bytes_sent_standardized
  iceberg_field_id: 12
  doc: Standardized log-transformed bytes sent by the device.
```

The numbers in this guide are illustrative, not catalog observations.
Store tags/governance/ownership on the feature or in an interoperable catalog. Keep
raw, aggregate, and normalized features as separate id@version DAG nodes. Represent
StandardScaler with transform.kind: fitted, transform.fit.split: train, and an
external immutable state artifact; never use ml.normalization as a pipeline toggle.

## Reproducibility

Experiment plans may leave datasets unresolved, but every resolved training/evaluation
run must record each input table and exact snapshot, independently of feature-set
version, plus split, fitted artifacts, code/environment and seed references.
An experiment's optional dataset (or datasets list) is a resolved binding:

```yaml
dataset:
  format: iceberg
  table: prod.ml.device_features
  iceberg_snapshot_id: 918273645
```

Snapshots reference schemas; schema IDs and snapshot IDs are different identities.
Preserve referenced snapshots/data with an appropriate retention policy: a recorded
snapshot ID alone does not stop expiration. Verify availability before replay.

## Enforcement boundary

featurectl validates declared storage bindings and resolved experiment dataset pins,
rejects malformed IDs and inline normalization, and retains existing DAG/fitted-state
checks. Storage is optional for unmaterialized/non-Iceberg features. It does not connect
to a catalog, verify snapshot existence, reconcile live schema/docs, enforce retention,
or prove that a prose doc contains no configuration. Those checks belong to adapters
and integration tests; report them separately from static validation.

## Sources

Adapted from the user-provided Apache Iceberg Column Metadata for ML Feature Pipelines
guide, with the schema-authority and normalization interpretations approved in this conversation.
See the [Iceberg specification](https://iceberg.apache.org/spec/) for field and snapshot
metadata and [snapshot expiration](https://iceberg.apache.org/docs/latest/spark-procedures/#expire_snapshots)
for retention behavior.
