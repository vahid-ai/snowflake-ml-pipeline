# Benchmark Harness

This suite compares Hugging Face → Snowflake ingestion methods and writes
per-run metrics under `bench/artifacts/`, which is ignored by Git. It uses the
same Snowflake configuration as the loader — see
[`docs/authentication.md`](../docs/authentication.md). Interactive methods such
as `externalbrowser` are a poor fit here, since a benchmark run opens several
connections.

## Scenarios

- `arrow_parquet_snowflake`: Hub-hosted Parquet shards are read over fsspec in
  Arrow batches and loaded into Snowflake by dlt as Parquet.
- `streaming_dlt_snowflake`: Hugging Face `datasets` streaming rows are batched
  through dlt into Snowflake.

Each scenario loads into its own `bench_<scenario>` schema so the production
`raw_lamda` schema is untouched. Pass `--drop-destination-dataset` to drop those
benchmark schemas once metrics are collected.

## Metrics

Each run writes `metrics.json`, `summary.json`, and an HTML pyinstrument flame
graph. Metrics include:

- total wall/process time
- wall/process time by named pipeline stage
- peak RSS and Python allocation peak
- source file count and optional source ingress byte estimate
- destination bytes reported by Snowflake `information_schema`
- rows loaded and transformed

## Examples

Smoke benchmark both scenarios:

```powershell
uv run python -m bench.run
```

By default the smoke run limits Parquet ingestion to 4 files and streaming to
4 Hugging Face splits. Use `--full` when you explicitly want full-dataset runs.

Benchmark only the Parquet path on the full dataset:

```powershell
uv run python -m bench.run --scenario arrow_parquet_snowflake --full
```

Disable flame graph generation:

```powershell
uv run python -m bench.run --no-profile
```
