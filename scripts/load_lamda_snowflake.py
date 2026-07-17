"""Ingest the IQSeC-Lab/LAMDA Hugging Face dataset into Snowflake with dlt.

Snowflake credentials are read by dlt from the standard locations:

* ``.dlt/secrets.toml`` (gitignored)::

    [destination.snowflake.credentials]
    database = "LAMDA_DB"
    username = "LOADER"
    password = "..."
    host = "<account_identifier>"
    warehouse = "COMPUTE_WH"
    role = "LOADER_ROLE"

* or environment variables::

    DESTINATION__SNOWFLAKE__CREDENTIALS__DATABASE
    DESTINATION__SNOWFLAKE__CREDENTIALS__USERNAME
    DESTINATION__SNOWFLAKE__CREDENTIALS__PASSWORD
    DESTINATION__SNOWFLAKE__CREDENTIALS__HOST
    DESTINATION__SNOWFLAKE__CREDENTIALS__WAREHOUSE
    DESTINATION__SNOWFLAKE__CREDENTIALS__ROLE

Usage::

    uv run python scripts/load_lamda_snowflake.py
    uv run python scripts/load_lamda_snowflake.py --limit-per-file 100
    uv run python scripts/load_lamda_snowflake.py --destination duckdb  # local smoke test
"""

from __future__ import annotations

import argparse
from collections.abc import Iterator
from pathlib import Path

import dlt
import pyarrow as pa
import pyarrow.parquet as pq
from huggingface_hub import HfFileSystem

from load_lamda import DATASET_ID, DEFAULT_DATASET_NAME, parquet_manifest

DEFAULT_PIPELINE_NAME = "lamda_huggingface_snowflake"
DEFAULT_DUCKDB_PATH = Path("data/lamda_snowflake_smoke.duckdb")


def _with_metadata(
    batch: pa.RecordBatch,
    file: dict,
    start_row: int,
) -> pa.RecordBatch:
    """Prepend dataset/config/split/provenance columns to an Arrow batch."""
    n = batch.num_rows
    metadata_columns = {
        "dataset_id": pa.array([file["dataset_id"]] * n, pa.string()),
        "config_name": pa.array([file["config_name"]] * n, pa.string()),
        "split_name": pa.array([file["split_name"]] * n, pa.string()),
        "row_number": pa.array(range(start_row, start_row + n), pa.int64()),
        "source_file": pa.array([file["url"]] * n, pa.string()),
    }
    arrays = list(metadata_columns.values()) + batch.columns
    names = list(metadata_columns.keys()) + batch.schema.names
    return pa.RecordBatch.from_arrays(arrays, names=names)


@dlt.resource(name="lamda_files", write_disposition="replace")
def lamda_files(files: list[dict]) -> Iterator[dict]:
    yield from files


@dlt.resource(name="lamda_samples", write_disposition="replace")
def lamda_samples(
    files: list[dict],
    limit_per_file: int | None = None,
    batch_size: int = 10_000,
) -> Iterator[pa.RecordBatch]:
    """Stream Parquet row batches straight from the Hugging Face hub."""
    fs = HfFileSystem()

    for file in files:
        hf_path = f"datasets/{file['dataset_id']}/{file['repo_path']}"
        remaining = limit_per_file
        row_number = 0

        with fs.open(hf_path, "rb") as handle:
            parquet_file = pq.ParquetFile(handle)
            for batch in parquet_file.iter_batches(batch_size=batch_size):
                if remaining is not None:
                    if remaining <= 0:
                        break
                    batch = batch.slice(0, remaining)
                    remaining -= batch.num_rows
                yield _with_metadata(batch, file, start_row=row_number)
                row_number += batch.num_rows


@dlt.source(name="lamda")
def lamda_source(limit_per_file: int | None = None):
    files = parquet_manifest()
    return (
        lamda_files(files),
        lamda_samples(files, limit_per_file=limit_per_file),
    )


def run_pipeline(
    destination_name: str,
    dataset_name: str,
    limit_per_file: int | None,
    duckdb_path: Path,
) -> None:
    if destination_name == "snowflake":
        destination = dlt.destinations.snowflake()
    elif destination_name == "duckdb":
        duckdb_path.parent.mkdir(parents=True, exist_ok=True)
        destination = dlt.destinations.duckdb(credentials=str(duckdb_path))
    else:
        raise ValueError(f"Unsupported destination: {destination_name}")

    pipeline = dlt.pipeline(
        pipeline_name=DEFAULT_PIPELINE_NAME,
        destination=destination,
        dataset_name=dataset_name,
    )
    load_info = pipeline.run(lamda_source(limit_per_file=limit_per_file))
    print(load_info)

    row_counts = pipeline.last_trace.last_normalize_info
    print(row_counts)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            f"Ingest the {DATASET_ID} Hugging Face dataset into Snowflake with dlt."
        )
    )
    parser.add_argument(
        "--destination",
        choices=["snowflake", "duckdb"],
        default="snowflake",
        help=(
            "Destination to load into. Defaults to snowflake; duckdb is a local "
            "smoke-test target that needs no credentials."
        ),
    )
    parser.add_argument(
        "--dataset-name",
        default=DEFAULT_DATASET_NAME,
        help=(
            "Target schema (dlt dataset) in the destination. "
            f"Defaults to {DEFAULT_DATASET_NAME}."
        ),
    )
    parser.add_argument(
        "--limit-per-file",
        type=int,
        default=None,
        help="Optional row limit per Parquet file, useful for smoke tests.",
    )
    parser.add_argument(
        "--duckdb-path",
        type=Path,
        default=DEFAULT_DUCKDB_PATH,
        help=(
            "DuckDB database path used when --destination duckdb is selected. "
            f"Defaults to {DEFAULT_DUCKDB_PATH}."
        ),
    )
    args = parser.parse_args()

    run_pipeline(
        destination_name=args.destination,
        dataset_name=args.dataset_name,
        limit_per_file=args.limit_per_file,
        duckdb_path=args.duckdb_path,
    )


if __name__ == "__main__":
    main()
