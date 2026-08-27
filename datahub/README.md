# DataHub over the R2 Iceberg catalog

Runs a local [DataHub](https://datahubproject.io) instance and ingests the
metadata of the Iceberg tables that `scripts/load_lamda_r2_iceberg.py` writes
to Cloudflare R2 Data Catalog, so the LAMDA tables are browsable — schema (all
~4.5k columns), snapshots, and properties — in a data catalog UI.

## 1. Start DataHub

DataHub's quickstart runs as a Docker Compose stack (GMS, frontend, MySQL,
OpenSearch, Kafka, actions). Install the CLI in an isolated environment so its
pyiceberg pin cannot conflict with the project's dlt dependencies:

```bash
uv tool install "acryl-datahub[iceberg]"
datahub docker quickstart
```

First start pulls ~7 images and takes a few minutes. The UI then serves at
<http://localhost:9002> (default login `datahub` / `datahub`) and the GMS API
at <http://localhost:8080>.

## 2. Ingest the R2 Iceberg metadata

`iceberg_r2.dhub.yml` is the ingestion recipe: an `iceberg` source whose
catalog properties mirror `scripts/load_lamda_r2_iceberg.py` (REST catalog
URI, warehouse, token, and the R2 S3 endpoint/keys for reading manifests), and
a `datahub-rest` sink pointing at the local GMS. `ingest.sh` derives the same
defaults from `R2_ACCOUNT_ID`/`R2_BUCKET` that the pipeline uses, so with
secrets in Infisical the whole run is:

```bash
infisical run --projectId=<project-id> --env=dev -- datahub/ingest.sh
```

or with a filled-in `.env`: `set -a && . ./.env && set +a && datahub/ingest.sh`.

The run registers each Iceberg table as a DataHub dataset under platform
`iceberg`, instance `r2_lamda` — e.g. `raw_lamda.lamda_samples` with its full
column schema and table properties. Re-running refreshes them.

## Operational notes

- The quickstart stack is stateful across restarts (Docker volumes). Stop it
  with `datahub docker quickstart --stop`; wipe it with `datahub docker nuke`.
- The Iceberg source reads only *metadata* (catalog listings and table
  manifests) — no table data rows are scanned unless profiling is enabled.
- In an ephemeral container, DataHub's state dies with the container; this
  setup is for exploration, not a durable catalog deployment.
