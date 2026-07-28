# snowflake-ml-pipeline

dlt and dbt pipeline that ingests the Hugging Face dataset `IQSeC-Lab/LAMDA`
**directly into Snowflake**. The Parquet shards are streamed from the Hub in
Arrow batches and handed to dlt, so the dataset is never downloaded to a local
database or file.

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
