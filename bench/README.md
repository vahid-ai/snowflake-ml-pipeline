# Benchmark Harness

This suite compares source-to-destination pipeline implementations and writes
per-run metrics under `bench/artifacts/`, which is ignored by Git.

## Scenarios

- `manifest_parquet_duckdb`: dlt loads a Hugging Face Parquet file manifest,
  then DuckDB materializes the remote Parquet files into a local DuckDB table.
- `streaming_dlt_duckdb`: Hugging Face `datasets` streaming rows are batched
  through dlt into a local DuckDB table.

## Metrics

Each run writes `metrics.json`, `summary.json`, an HTML pyinstrument flame graph,
and a benchmark-local DuckDB database. Metrics include:

- total wall/process time
- wall/process time by named pipeline stage
- peak RSS and Python allocation peak
- source file count and optional source ingress byte estimate
- destination database bytes
- rows loaded and transformed

## Examples

Smoke benchmark both built-in scenarios:

```powershell
uv run python -m bench.run
```

By default the smoke run limits Parquet ingestion to 4 files and streaming to
4 Hugging Face splits. Use `--full` when you explicitly want full-dataset runs.

Benchmark only the Parquet materialization path on the full dataset:

```powershell
uv run python -m bench.run --scenario manifest_parquet_duckdb --full
```

Disable flame graph generation:

```powershell
uv run python -m bench.run --no-profile
```
