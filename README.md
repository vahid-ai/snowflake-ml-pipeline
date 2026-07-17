# snowflake-ml-pipeline

dlt and dbt pipelines for loading the Hugging Face dataset
`IQSeC-Lab/LAMDA` into DuckDB (local) or Snowflake.

## Load data

```powershell
uv run python scripts/load_lamda.py
```

For a fast smoke test:

```powershell
uv run python scripts/load_lamda.py --limit-per-file 10
```

The DuckDB database is written to `data/lamda.duckdb`. The `data/` directory
and DuckDB files are intentionally ignored by Git.

## Load data into Snowflake

`scripts/load_lamda_snowflake.py` uses [dlt](https://dlthub.com/) with the
Snowflake destination. It streams the LAMDA Parquet files straight from the
Hugging Face hub as Arrow record batches (no full local download) and loads
them into two tables in the target schema (default `raw_lamda`):

- `lamda_samples` — all rows from every config/split, with `dataset_id`,
  `config_name`, `split_name`, `row_number`, and `source_file` metadata columns
- `lamda_files` — the manifest of ingested Parquet files

Configure Snowflake credentials the standard dlt way, either in
`.dlt/secrets.toml` (gitignored):

```toml
[destination.snowflake.credentials]
database = "LAMDA_DB"
username = "LOADER"
password = "..."
host = "<account_identifier>"
warehouse = "COMPUTE_WH"
role = "LOADER_ROLE"
```

or via environment variables
(`DESTINATION__SNOWFLAKE__CREDENTIALS__DATABASE`,
`DESTINATION__SNOWFLAKE__CREDENTIALS__USERNAME`,
`DESTINATION__SNOWFLAKE__CREDENTIALS__PASSWORD`,
`DESTINATION__SNOWFLAKE__CREDENTIALS__HOST`,
`DESTINATION__SNOWFLAKE__CREDENTIALS__WAREHOUSE`,
`DESTINATION__SNOWFLAKE__CREDENTIALS__ROLE`). The database, warehouse, and
role must already exist; dlt creates the schema and tables.

Then run:

```powershell
uv run python scripts/load_lamda_snowflake.py
```

For a fast smoke test (100 rows per file):

```powershell
uv run python scripts/load_lamda_snowflake.py --limit-per-file 100
```

To exercise the same pipeline locally without Snowflake credentials:

```powershell
uv run python scripts/load_lamda_snowflake.py --destination duckdb --limit-per-file 100
```

## Run dbt

```powershell
uv run dbt run --profiles-dir .
uv run dbt test --profiles-dir .
```

## Benchmark pipelines

```powershell
uv run python -m bench.run
```

Benchmark metrics, flame graphs, and benchmark DuckDB files are written under
`bench/artifacts/`, which is ignored by Git.
