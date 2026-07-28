"""Ingest the IQSeC-Lab/LAMDA Hugging Face dataset straight into Snowflake with dlt.

The Parquet shards are streamed from the Hugging Face Hub in Arrow batches and
handed to dlt, so no copy of the dataset is materialized on the local disk.
"""

from __future__ import annotations

import argparse
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import dlt
import pyarrow as pa
import pyarrow.parquet as pq
from datasets import get_dataset_config_names
from dlt.destinations import snowflake as dlt_snowflake
from huggingface_hub import HfFileSystem, hf_hub_url, list_repo_files


DATASET_ID = "IQSeC-Lab/LAMDA"
DEFAULT_DATASET_NAME = "raw_lamda"
DEFAULT_BATCH_SIZE = 25_000
PIPELINE_NAME = "lamda_huggingface"

SOURCE_COLUMNS = ("dataset_id", "config_name", "split_name", "row_number", "source_file")


def parquet_manifest() -> list[dict]:
    configs = set(get_dataset_config_names(DATASET_ID))
    files = []

    for repo_path in list_repo_files(DATASET_ID, repo_type="dataset"):
        if not repo_path.endswith(".parquet"):
            continue

        config_name = repo_path.split("/", 1)[0]
        if config_name not in configs:
            continue

        file_name = Path(repo_path).name
        if file_name.endswith("_train.parquet"):
            split_name = "train"
        elif file_name.endswith("_test.parquet"):
            split_name = "test"
        else:
            continue

        files.append(
            {
                "dataset_id": DATASET_ID,
                "config_name": config_name,
                "split_name": split_name,
                "repo_path": repo_path,
                "fs_path": f"datasets/{DATASET_ID}/{repo_path}",
                "url": hf_hub_url(DATASET_ID, repo_path, repo_type="dataset"),
            }
        )

    return sorted(files, key=lambda row: (row["config_name"], row["repo_path"]))


@dlt.resource(name="lamda_files", write_disposition="replace")
def lamda_files(files: list[dict] | None = None) -> Iterator[dict]:
    yield from files if files is not None else parquet_manifest()


def _with_source_columns(table: pa.Table, file: dict, start_row: int) -> pa.Table:
    """Prefix a batch with the provenance columns the dbt models rely on."""
    row_count = table.num_rows
    source_columns = {
        "dataset_id": pa.array([file["dataset_id"]] * row_count, pa.string()),
        "config_name": pa.array([file["config_name"]] * row_count, pa.string()),
        "split_name": pa.array([file["split_name"]] * row_count, pa.string()),
        "row_number": pa.array(range(start_row, start_row + row_count), pa.int64()),
        "source_file": pa.array([file["repo_path"]] * row_count, pa.string()),
    }

    for name in SOURCE_COLUMNS:
        if name in table.column_names:
            table = table.drop_columns(name)

    for position, name in enumerate(SOURCE_COLUMNS):
        table = table.add_column(position, name, source_columns[name])

    return table


def _stream_remote_parquet(
    fs: HfFileSystem,
    file: dict,
    batch_size: int,
    limit_per_file: int | None,
) -> Iterator[pa.Table]:
    """Yield Arrow batches read directly from a Hub-hosted Parquet shard."""
    with fs.open(file["fs_path"], "rb") as handle:
        parquet_file = pq.ParquetFile(handle)
        emitted = 0

        for batch in parquet_file.iter_batches(batch_size=batch_size):
            if limit_per_file is not None:
                remaining = limit_per_file - emitted
                if remaining <= 0:
                    return
                if batch.num_rows > remaining:
                    batch = batch.slice(0, remaining)

            table = _with_source_columns(pa.Table.from_batches([batch]), file, emitted)
            emitted += table.num_rows
            yield table


@dlt.resource(name="lamda_samples", write_disposition="replace")
def lamda_samples(
    files: list[dict] | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    limit_per_file: int | None = None,
    hf_token: str | None = None,
) -> Iterator[pa.Table]:
    files = files if files is not None else parquet_manifest()
    fs = HfFileSystem(token=hf_token or os.getenv("HF_TOKEN"))

    for file in files:
        yield from _stream_remote_parquet(
            fs=fs,
            file=file,
            batch_size=batch_size,
            limit_per_file=limit_per_file,
        )


def snowflake_credentials() -> dict[str, Any] | None:
    """Build dlt Snowflake credentials from the environment.

    Supports password, key-pair, and token auth (programmatic access tokens or
    OAuth, selected with ``SNOWFLAKE_AUTHENTICATOR``). Returns ``None`` when
    nothing is set so dlt can fall back to its own config providers
    (``.dlt/secrets.toml`` or ``DESTINATION__SNOWFLAKE__*`` variables).
    """
    credentials = {
        "host": os.getenv("SNOWFLAKE_ACCOUNT"),
        "username": os.getenv("SNOWFLAKE_USER"),
        "password": os.getenv("SNOWFLAKE_PASSWORD"),
        "database": os.getenv("SNOWFLAKE_DATABASE"),
        "warehouse": os.getenv("SNOWFLAKE_WAREHOUSE"),
        "role": os.getenv("SNOWFLAKE_ROLE"),
        "authenticator": os.getenv("SNOWFLAKE_AUTHENTICATOR"),
        "token": os.getenv("SNOWFLAKE_TOKEN"),
        "private_key": os.getenv("SNOWFLAKE_PRIVATE_KEY"),
        "private_key_path": os.getenv("SNOWFLAKE_PRIVATE_KEY_PATH"),
        "private_key_passphrase": os.getenv("SNOWFLAKE_PRIVATE_KEY_PASSPHRASE"),
    }
    credentials = {key: value for key, value in credentials.items() if value}
    return credentials or None


def snowflake_destination(**kwargs: Any):
    return dlt_snowflake(credentials=snowflake_credentials(), **kwargs)


def build_pipeline(
    dataset_name: str = DEFAULT_DATASET_NAME,
    pipeline_name: str = PIPELINE_NAME,
) -> dlt.Pipeline:
    return dlt.pipeline(
        pipeline_name=pipeline_name,
        destination=snowflake_destination(),
        dataset_name=dataset_name,
    )


def run_pipeline(
    dataset_name: str = DEFAULT_DATASET_NAME,
    limit_per_file: int | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_files: int | None = None,
) -> None:
    files = parquet_manifest()
    if max_files is not None:
        files = files[:max_files]
    if not files:
        raise RuntimeError(f"No Parquet shards found for {DATASET_ID}")

    pipeline = build_pipeline(dataset_name=dataset_name)
    load_info = pipeline.run(
        [
            lamda_files(files),
            lamda_samples(
                files=files,
                batch_size=batch_size,
                limit_per_file=limit_per_file,
            ),
        ],
        loader_file_format="parquet",
    )
    print(load_info)

    row_counts = pipeline.last_trace.last_normalize_info.row_counts
    loaded_rows = row_counts.get("lamda_samples", 0)
    print(
        f"Streamed {loaded_rows} rows from {len(files)} Hugging Face Parquet files "
        f"into Snowflake schema {dataset_name}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Stream the IQSeC-Lab/LAMDA Hugging Face dataset directly into Snowflake with dlt."
        )
    )
    parser.add_argument(
        "--dataset-name",
        default=DEFAULT_DATASET_NAME,
        help="Destination Snowflake schema. Defaults to raw_lamda.",
    )
    parser.add_argument(
        "--limit-per-file",
        type=int,
        default=None,
        help="Optional row limit per Parquet file, useful for smoke tests.",
    )
    parser.add_argument(
        "--max-files",
        type=int,
        default=None,
        help="Optional cap on the number of Parquet files to ingest.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help="Rows per Arrow batch streamed from Hugging Face.",
    )
    args = parser.parse_args()

    run_pipeline(
        dataset_name=args.dataset_name,
        limit_per_file=args.limit_per_file,
        batch_size=args.batch_size,
        max_files=args.max_files,
    )


if __name__ == "__main__":
    main()
