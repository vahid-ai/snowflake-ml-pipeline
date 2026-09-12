# snowflake-ml-pipeline

Repository-wide agent guidance and ML feature governance are configured in [AGENTS.md](AGENTS.md).
See [plugin setup and validation](docs/plugins.md) for the packages under `plugins/` and client configuration.

dlt and dbt pipelines that ingest the Hugging Face dataset `IQSeC-Lab/LAMDA`
**directly into a warehouse or lakehouse**. The Parquet shards are streamed from
the Hub in Arrow batches and handed to dlt. A separate downloader copies the
existing R2 Iceberg tables into a local Iceberg warehouse for offline work.

Two ingestion destinations can be benchmarked against each other, with a third
pipeline for downloading the R2 output:

| Pipeline | Destination | Script |
| --- | --- | --- |
| Snowflake | `raw_lamda` schema in a Snowflake database | `scripts/load_lamda.py` |
| R2 + Iceberg | Iceberg tables in a Cloudflare R2 bucket, registered in R2 Data Catalog | `scripts/load_lamda_r2_iceberg.py` |
| R2 → local Iceberg | Local Iceberg warehouse with a persistent SQLite catalog | `scripts/load_lamda_local_iceberg.py` |

The two ingestion pipelines read the same Hugging Face source and produce the same two tables with the
same provenance columns, so their metrics are directly comparable. dbt currently
models the Snowflake output.

## Configure Snowflake

Copy `.env.example` and fill it in — both the dlt pipeline and the dbt profile
read the same variables:

```powershell
Copy-Item .env.example .env
```

A minimal key-pair setup, which is the recommended method:

```bash
SNOWFLAKE_ACCOUNT=myorg-my_account       # or legacy abc12345.us-east-1
SNOWFLAKE_USER=LAMDA_LOADER
SNOWFLAKE_PRIVATE_KEY_PATH=/home/you/.snowflake/lamda_loader.p8
SNOWFLAKE_DATABASE=LAMDA
SNOWFLAKE_WAREHOUSE=LOADING_WH
SNOWFLAKE_ROLE=LAMDA_LOADER              # needs CREATE SCHEMA on the database
```

**[docs/authentication.md](docs/authentication.md)** documents every supported
method — key pair, programmatic access tokens, password, password + MFA,
external browser SSO, OAuth, and Okta — with the SQL and shell steps to set each
one up, the `.dlt/secrets.toml` alternative for the ingestion side, Hugging Face
tokens for gated datasets, and a troubleshooting table.

Check the configuration before loading anything:

```powershell
uv run dbt debug --profiles-dir .
```

## Ingest data into Snowflake

```powershell
uv run python scripts/load_lamda.py
```

For a fast smoke test:

```powershell
uv run python scripts/load_lamda.py --limit-per-file 10 --max-files 2
```

The pipeline writes two tables into the `raw_lamda` schema of
`SNOWFLAKE_DATABASE`:

- `lamda_samples` — dataset rows, prefixed with `dataset_id`, `config_name`,
  `split_name`, `row_number`, and `source_file`
- `lamda_files` — the Hugging Face Parquet manifest that was ingested

Both are loaded with `write_disposition="replace"`, so a run fully refreshes the
schema. Use `--dataset-name` to load into a different Snowflake schema and
`--batch-size` to tune the Arrow batch size streamed from the Hub.

## Ingest data into Cloudflare R2 as Iceberg

```powershell
uv run python scripts/load_lamda_r2_iceberg.py
```

For a fast smoke test:

```powershell
uv run python scripts/load_lamda_r2_iceberg.py --limit-per-file 10 --max-files 2
```

This writes the same `lamda_samples` and `lamda_files` tables, in Iceberg format,
into the `raw_lamda` namespace of your R2 bucket, registering them in R2 Data
Catalog so engines like DuckDB, PyIceberg, Spark, or Trino can query them. It
reuses the Hugging Face streaming resources from `scripts/load_lamda.py` — only
the destination differs.

Setup, in short:

1. Create an R2 bucket and enable its catalog
   (`npx wrangler r2 bucket catalog enable <bucket>`), noting the catalog URI and
   warehouse it prints.
2. Create an R2 S3 API token (Object Read & Write) for the data files, and a
   Cloudflare API token with R2 *and* catalog permissions for the catalog.
3. Fill in the `R2_*` block of `.env.example`.

The credentials and their permissions are documented in
[docs/authentication.md](docs/authentication.md#cloudflare-r2-and-r2-data-catalog).

## Download R2 tables into local Iceberg

Inject the R2 credentials with the Infisical CLI, then run:

```powershell
infisical run --projectId=0cfed731-cdf4-46b8-b831-2d74be495575 --env=dev -- uv run python scripts/load_lamda_local_iceberg.py
```

This refreshes all `lamda_*` tables in a local warehouse under
`data/lamda_iceberg/`, with a persistent `catalog.sqlite` for offline access.
For a small test, add `--local-root data/lamda_iceberg_smoke --limit-per-table 100`.
See [docs/local_iceberg.md](docs/local_iceberg.md) for table selection, offline
queries, memory controls and refresh/recovery behavior.

## LAMDA feature descriptions

LAMDA's `feat_*` columns are Drebin-style binary features whose meaning lives
in the Hub's `feature_mapping.csv` files. Two scripts turn that mapping into a
queryable data dictionary:

```powershell
uv run python scripts/build_lamda_feature_descriptions.py
uv run python scripts/load_lamda_feature_descriptions.py
```

The first classifies every static-analysis token into its Drebin feature set
(hardware components, requested/used permissions, app components, intent
filters, restricted/suspicious API calls, network addresses) and generates,
from a curated Android-malware knowledge base, a description of what the token
is plus how it serves as a malware-detection signal. The result is committed at
[data/lamda_feature_descriptions.json](data/lamda_feature_descriptions.json).

The second loads that JSON with dlt into the same R2 Data Catalog namespace as
the raw data, adding three Iceberg tables that join against `lamda_samples`:

- `lamda_feature_descriptions` — one row per token, with the config-specific
  `feat_*` ids (`feature_id_baseline`, `feature_id_var_thresh_0_01`; ids are
  **not** interchangeable between configs) and the two description texts
- `lamda_feature_categories` — the ten Drebin feature sets with category-level
  detection rationale
- `lamda_column_glossary` — the non-feature metadata and provenance columns of
  `lamda_samples`

## Run dbt

```powershell
uv run dbt run --profiles-dir .
uv run dbt test --profiles-dir .
```

## Benchmark pipelines

```powershell
uv run python -m bench.run
```

Benchmark metrics and flame graphs are written under `bench/artifacts/`, which
is ignored by Git.
