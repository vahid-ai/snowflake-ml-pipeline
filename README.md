# snowflake-ml-pipeline

dlt and dbt pipelines that ingest the Hugging Face dataset `IQSeC-Lab/LAMDA`
**directly into a warehouse or lakehouse**. The Parquet shards are streamed from
the Hub in Arrow batches and handed to dlt, so the dataset is never downloaded to
a local database or file.

Two destinations are maintained side by side so they can be benchmarked against
each other:

| Pipeline | Destination | Script |
| --- | --- | --- |
| Snowflake | `raw_lamda` schema in a Snowflake database | `scripts/load_lamda.py` |
| R2 + Iceberg | Iceberg tables in a Cloudflare R2 bucket, registered in R2 Data Catalog | `scripts/load_lamda_r2_iceberg.py` |

Both read the same Hugging Face source and produce the same two tables with the
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
