# snowflake-ml-pipeline

dlt and dbt pipeline that ingests the Hugging Face dataset `IQSeC-Lab/LAMDA`
**directly into Snowflake**. The Parquet shards are streamed from the Hub in
Arrow batches and handed to dlt, so the dataset is never downloaded to a local
database or file.

## Configure Snowflake

Copy `.env.example` and export the variables (both the dlt pipeline and the dbt
profile read them):

```powershell
Copy-Item .env.example .env
```

| Variable | Purpose |
| --- | --- |
| `SNOWFLAKE_ACCOUNT` | Account identifier, e.g. `abc12345.us-east-1` |
| `SNOWFLAKE_USER` | Login name |
| `SNOWFLAKE_PASSWORD` | Password auth (omit when using key-pair or a token) |
| `SNOWFLAKE_PRIVATE_KEY` | Key-pair auth with an inline PEM or base64 DER key, handy in CI (ingestion only) |
| `SNOWFLAKE_PRIVATE_KEY_PATH` / `SNOWFLAKE_PRIVATE_KEY_PASSPHRASE` | Key-pair auth, PKCS#8 PEM key |
| `SNOWFLAKE_AUTHENTICATOR` | Optional, e.g. `programmatic_access_token`, `oauth`, `externalbrowser` |
| `SNOWFLAKE_TOKEN` | Token for `programmatic_access_token` or `oauth` authenticators |
| `SNOWFLAKE_DATABASE` | Target database |
| `SNOWFLAKE_WAREHOUSE` | Warehouse used for loading and dbt |
| `SNOWFLAKE_ROLE` | Role with create-schema rights on the database |
| `SNOWFLAKE_SCHEMA` | dbt target schema, defaults to `analytics` |
| `HF_TOKEN` | Only needed for gated or private Hugging Face datasets |

Instead of these variables you can use any dlt config provider for the
ingestion side, e.g. `.dlt/secrets.toml` or `DESTINATION__SNOWFLAKE__*`
variables; the loader falls back to them when the `SNOWFLAKE_*` variables are
unset.

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
