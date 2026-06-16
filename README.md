# snowflake-ml-pipeline

Local dlt and dbt pipeline for loading the Hugging Face dataset
`IQSeC-Lab/LAMDA` into DuckDB.

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
