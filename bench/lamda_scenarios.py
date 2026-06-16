from __future__ import annotations

import json
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import dlt
import duckdb
from datasets import get_dataset_config_names, get_dataset_split_names, load_dataset
from dlt.destinations import duckdb as dlt_duckdb

from bench.harness import BenchmarkContext
from scripts.load_lamda import (
    DEFAULT_DATASET_NAME,
    DATASET_ID,
    materialize_samples,
    parquet_manifest,
)


def _db_counts(db_path: Path, table: str = "lamda_samples") -> dict[str, int]:
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        rows = con.sql(
            f"""
            select config_name, split_name, count(*) as row_count
            from {DEFAULT_DATASET_NAME}.{table}
            group by 1, 2
            order by 1, 2
            """
        ).fetchall()
    finally:
        con.close()

    return {f"{config}.{split}": int(row_count) for config, split, row_count in rows}


def _table_count(db_path: Path, table: str) -> int:
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        return int(con.sql(f"select count(*) from {DEFAULT_DATASET_NAME}.{table}").fetchone()[0])
    finally:
        con.close()


def _probe_remote_bytes(files: list[dict], enabled: bool) -> int | None:
    if not enabled:
        return None

    total = 0
    for file in files:
        request = urllib.request.Request(file["url"], method="HEAD")
        with urllib.request.urlopen(request, timeout=60) as response:
            length = response.headers.get("content-length")
            if length is not None:
                total += int(length)
    return total


@dlt.resource(name="lamda_files", write_disposition="replace")
def selected_lamda_files(files: list[dict]) -> Iterator[dict]:
    yield from files


def _streaming_records(
    limit_per_split: int,
    batch_size: int,
    max_splits: int | None,
) -> Iterator[list[dict]]:
    splits_seen = 0
    for config_name in get_dataset_config_names(DATASET_ID):
        for split_name in get_dataset_split_names(DATASET_ID, config_name):
            if max_splits is not None and splits_seen >= max_splits:
                return
            splits_seen += 1

            rows = load_dataset(
                DATASET_ID,
                config_name,
                split=split_name,
                streaming=True,
            )
            batch: list[dict] = []
            for row_number, row in enumerate(rows):
                if row_number >= limit_per_split:
                    break

                batch.append(
                    {
                        "dataset_id": DATASET_ID,
                        "config_name": config_name,
                        "split_name": split_name,
                        "row_number": row_number,
                        **row,
                    }
                )

                if len(batch) >= batch_size:
                    yield batch
                    batch = []

            if batch:
                yield batch


@dlt.resource(name="lamda_samples", write_disposition="replace")
def streaming_lamda_samples(
    limit_per_split: int,
    batch_size: int,
    max_splits: int | None,
) -> Iterator[list[dict]]:
    yield from _streaming_records(
        limit_per_split=limit_per_split,
        batch_size=batch_size,
        max_splits=max_splits,
    )


def benchmark_manifest_parquet_duckdb(
    context: BenchmarkContext,
    options: dict[str, Any],
) -> None:
    limit_per_file = options.get("limit_per_file")
    max_files = options.get("max_files")
    probe_remote_bytes = bool(options.get("probe_remote_bytes", True))
    db_path = context.artifact_dir / "manifest_parquet.duckdb"

    with context.stage("discover_source_files"):
        files = parquet_manifest()
        if max_files is not None:
            files = files[: int(max_files)]

    context.set_counter("source_files", len(files))

    with context.stage("probe_source_bytes", enabled=probe_remote_bytes):
        ingress_bytes = _probe_remote_bytes(files, enabled=probe_remote_bytes)

    if ingress_bytes is not None:
        context.set_counter("source_ingress_bytes", ingress_bytes)

    with context.stage("dlt_load_file_manifest"):
        pipeline = dlt.pipeline(
            pipeline_name=f"bench_{context.scenario}",
            destination=dlt_duckdb(credentials=str(db_path)),
            dataset_name=DEFAULT_DATASET_NAME,
        )
        pipeline.run(selected_lamda_files(files))

    with context.stage("duckdb_materialize_remote_parquet"):
        materialize_samples(db_path=db_path, files=files, limit_per_file=limit_per_file)

    with context.stage("summarize_destination"):
        counts = _db_counts(db_path)
        manifest_rows = _table_count(db_path, "lamda_files")
        destination_bytes = db_path.stat().st_size

    context.set_counter("destination_rows_by_partition", counts)
    context.set_counter("destination_rows", sum(counts.values()))
    context.set_counter("manifest_rows", manifest_rows)
    context.set_counter("destination_egress_bytes", destination_bytes)
    context.set_counter("transformed_rows", sum(counts.values()))
    context.add_artifact("duckdb", db_path)


def benchmark_streaming_dlt_duckdb(
    context: BenchmarkContext,
    options: dict[str, Any],
) -> None:
    limit_per_split = int(options.get("limit_per_split") or 100)
    batch_size = int(options.get("batch_size") or 1_000)
    max_splits = options.get("max_splits")
    if max_splits is not None:
        max_splits = int(max_splits)
    db_path = context.artifact_dir / "streaming_dlt.duckdb"

    with context.stage("discover_configs_and_splits"):
        configs: dict[str, list[str]] = {}
        splits_seen = 0
        for config_name in get_dataset_config_names(DATASET_ID):
            selected_splits = []
            for split_name in get_dataset_split_names(DATASET_ID, config_name):
                if max_splits is not None and splits_seen >= max_splits:
                    break
                selected_splits.append(split_name)
                splits_seen += 1
            if selected_splits:
                configs[config_name] = selected_splits
            if max_splits is not None and splits_seen >= max_splits:
                break

    context.set_counter("configs", configs)
    context.set_counter(
        "requested_source_rows",
        sum(len(splits) for splits in configs.values()) * limit_per_split,
    )

    with context.stage("dlt_stream_huggingface_to_duckdb"):
        pipeline = dlt.pipeline(
            pipeline_name=f"bench_{context.scenario}",
            destination=dlt_duckdb(credentials=str(db_path)),
            dataset_name=DEFAULT_DATASET_NAME,
        )
        pipeline.run(
            streaming_lamda_samples(
                limit_per_split=limit_per_split,
                batch_size=batch_size,
                max_splits=max_splits,
            )
        )

    with context.stage("summarize_destination"):
        counts = _db_counts(db_path)
        destination_bytes = db_path.stat().st_size

    context.set_counter("destination_rows_by_partition", counts)
    context.set_counter("destination_rows", sum(counts.values()))
    context.set_counter("destination_egress_bytes", destination_bytes)
    context.set_counter("transformed_rows", sum(counts.values()))
    context.set_counter(
        "estimated_record_payload_bytes",
        _estimate_payload_bytes(db_path),
    )
    context.add_artifact("duckdb", db_path)


def _estimate_payload_bytes(db_path: Path) -> int:
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        return int(
            con.sql(
                f"""
                select coalesce(sum(length(to_json(t))), 0)::bigint
                from {DEFAULT_DATASET_NAME}.lamda_samples as t
                """
            ).fetchone()[0]
        )
    finally:
        con.close()


SCENARIOS = {
    "manifest_parquet_duckdb": benchmark_manifest_parquet_duckdb,
    "streaming_dlt_duckdb": benchmark_streaming_dlt_duckdb,
}


def scenario_names() -> list[str]:
    return sorted(SCENARIOS)


def get_scenario(name: str):
    return SCENARIOS[name]
