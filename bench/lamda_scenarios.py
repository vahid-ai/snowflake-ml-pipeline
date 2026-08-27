from __future__ import annotations

import urllib.request
from collections.abc import Iterator
from typing import Any

import dlt
from datasets import get_dataset_config_names, get_dataset_split_names, load_dataset

from bench.harness import BenchmarkContext
from scripts.load_lamda import (
    DATASET_ID,
    DEFAULT_BATCH_SIZE,
    build_pipeline,
    lamda_files,
    lamda_samples,
    parquet_manifest,
)
from scripts.load_lamda_r2_iceberg import (
    DEFAULT_FILES_PER_RUN,
    TABLE_FORMAT,
    build_pipeline as build_r2_iceberg_pipeline,
)


def _query(pipeline: dlt.Pipeline, sql: str) -> list[tuple]:
    with pipeline.sql_client() as client:
        with client.execute_query(sql) as cursor:
            return cursor.fetchall()


def _db_counts(pipeline: dlt.Pipeline, table: str = "lamda_samples") -> dict[str, int]:
    with pipeline.sql_client() as client:
        qualified = client.make_qualified_table_name(table)
        with client.execute_query(
            f"""
            select config_name, split_name, count(*) as row_count
            from {qualified}
            group by 1, 2
            order by 1, 2
            """
        ) as cursor:
            rows = cursor.fetchall()

    return {f"{config}.{split}": int(row_count) for config, split, row_count in rows}


def _table_count(pipeline: dlt.Pipeline, table: str) -> int:
    with pipeline.sql_client() as client:
        qualified = client.make_qualified_table_name(table)
        with client.execute_query(f"select count(*) from {qualified}") as cursor:
            return int(cursor.fetchone()[0])


def _destination_bytes(pipeline: dlt.Pipeline) -> int:
    """Storage footprint of the benchmark schema, straight from Snowflake."""
    rows = _query(
        pipeline,
        f"""
        select coalesce(sum(bytes), 0)
        from information_schema.tables
        where table_schema = upper('{pipeline.dataset_name}')
        """,
    )
    return int(rows[0][0])


def _drop_dataset(pipeline: dlt.Pipeline) -> None:
    with pipeline.sql_client() as client:
        client.drop_dataset()


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


def _finalize(
    context: BenchmarkContext,
    pipeline: dlt.Pipeline,
    options: dict[str, Any],
    manifest_table: bool = False,
) -> None:
    with context.stage("summarize_destination"):
        counts = _db_counts(pipeline)
        destination_bytes = _destination_bytes(pipeline)
        manifest_rows = _table_count(pipeline, "lamda_files") if manifest_table else None

    context.set_counter("destination_dataset", pipeline.dataset_name)
    context.set_counter("destination_rows_by_partition", counts)
    context.set_counter("destination_rows", sum(counts.values()))
    context.set_counter("destination_egress_bytes", destination_bytes)
    context.set_counter("transformed_rows", sum(counts.values()))
    if manifest_rows is not None:
        context.set_counter("manifest_rows", manifest_rows)

    if options.get("drop_destination_dataset"):
        with context.stage("drop_destination_dataset"):
            _drop_dataset(pipeline)


def benchmark_arrow_parquet_snowflake(
    context: BenchmarkContext,
    options: dict[str, Any],
) -> None:
    """Stream Hub-hosted Parquet shards through Arrow batches into Snowflake."""
    limit_per_file = options.get("limit_per_file")
    max_files = options.get("max_files")
    batch_size = int(options.get("arrow_batch_size") or DEFAULT_BATCH_SIZE)
    probe_remote_bytes = bool(options.get("probe_remote_bytes", True))

    with context.stage("discover_source_files"):
        files = parquet_manifest()
        if max_files is not None:
            files = files[: int(max_files)]

    context.set_counter("source_files", len(files))

    with context.stage("probe_source_bytes", enabled=probe_remote_bytes):
        ingress_bytes = _probe_remote_bytes(files, enabled=probe_remote_bytes)

    if ingress_bytes is not None:
        context.set_counter("source_ingress_bytes", ingress_bytes)

    pipeline = build_pipeline(
        dataset_name=f"bench_{context.scenario}",
        pipeline_name=f"bench_{context.scenario}",
    )

    with context.stage("dlt_stream_huggingface_parquet_to_snowflake"):
        pipeline.run(
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

    _finalize(context, pipeline, options, manifest_table=True)


def benchmark_streaming_dlt_snowflake(
    context: BenchmarkContext,
    options: dict[str, Any],
) -> None:
    """Stream Hugging Face `datasets` rows through dlt into Snowflake."""
    limit_per_split = int(options.get("limit_per_split") or 100)
    batch_size = int(options.get("batch_size") or 1_000)
    max_splits = options.get("max_splits")
    if max_splits is not None:
        max_splits = int(max_splits)

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

    pipeline = build_pipeline(
        dataset_name=f"bench_{context.scenario}",
        pipeline_name=f"bench_{context.scenario}",
    )

    with context.stage("dlt_stream_huggingface_rows_to_snowflake"):
        pipeline.run(
            streaming_lamda_samples(
                limit_per_split=limit_per_split,
                batch_size=batch_size,
                max_splits=max_splits,
            )
        )

    _finalize(context, pipeline, options)


def _iceberg_table(pipeline: dlt.Pipeline, table: str):
    catalog = pipeline.destination_client().get_open_table_catalog(TABLE_FORMAT)
    return catalog.load_table(f"{pipeline.dataset_name}.{table}")


def _iceberg_counts(pipeline: dlt.Pipeline, table: str = "lamda_samples") -> dict[str, int]:
    """Row counts per partition, reading only the two columns needed."""
    arrow = (
        _iceberg_table(pipeline, table)
        .scan(selected_fields=("config_name", "split_name"))
        .to_arrow()
    )
    counts: dict[str, int] = {}
    for config, split in zip(
        arrow.column("config_name").to_pylist(), arrow.column("split_name").to_pylist()
    ):
        key = f"{config}.{split}"
        counts[key] = counts.get(key, 0) + 1
    return counts


def _iceberg_snapshot_metrics(pipeline: dlt.Pipeline, table: str = "lamda_samples") -> dict[str, int]:
    """Rows and stored bytes straight from the Iceberg snapshot summary."""
    snapshot = _iceberg_table(pipeline, table).current_snapshot()
    if snapshot is None:
        return {"rows": 0, "bytes": 0}
    return {
        "rows": int(snapshot.summary["total-records"]),
        "bytes": int(snapshot.summary["total-files-size"]),
    }


def _drop_iceberg_dataset(pipeline: dlt.Pipeline) -> None:
    """Drop the benchmark namespace AND its files.

    `drop_table` only removes the catalog entry; without a purge the data and
    metadata files would accumulate in R2 across benchmark runs.
    """
    import os
    from urllib.parse import urlparse

    import boto3
    from scripts.load_lamda_r2_iceberg import r2_bucket_url, r2_s3_endpoint, r2_secret_access_key

    catalog = pipeline.destination_client().get_open_table_catalog(TABLE_FORMAT)
    namespace = pipeline.dataset_name
    for identifier in catalog.list_tables(namespace):
        try:
            catalog.purge_table(identifier)
        except Exception:
            catalog.drop_table(identifier)
    catalog.drop_namespace(namespace)

    # Purge support varies by catalog; delete any files left under the
    # namespace prefix so repeated benchmark runs don't accumulate storage.
    parsed = urlparse(r2_bucket_url())
    bucket, base_prefix = parsed.netloc, parsed.path.strip("/")
    prefix = f"{base_prefix}/{namespace}/"
    s3 = boto3.client(
        "s3",
        endpoint_url=r2_s3_endpoint(),
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=r2_secret_access_key(),
        region_name=os.getenv("R2_REGION", "auto"),
    )
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        keys = [{"Key": obj["Key"]} for obj in page.get("Contents", [])]
        if keys:
            s3.delete_objects(Bucket=bucket, Delete={"Objects": keys})


def benchmark_arrow_parquet_r2_iceberg(
    context: BenchmarkContext,
    options: dict[str, Any],
) -> None:
    """Stream Hub-hosted Parquet shards into Cloudflare R2 as Iceberg tables."""
    limit_per_file = options.get("limit_per_file")
    max_files = options.get("max_files")
    batch_size = int(options.get("arrow_batch_size") or DEFAULT_BATCH_SIZE)
    probe_remote_bytes = bool(options.get("probe_remote_bytes", True))

    with context.stage("discover_source_files"):
        files = parquet_manifest()
        if max_files is not None:
            files = files[: int(max_files)]

    context.set_counter("source_files", len(files))

    with context.stage("probe_source_bytes", enabled=probe_remote_bytes):
        ingress_bytes = _probe_remote_bytes(files, enabled=probe_remote_bytes)

    if ingress_bytes is not None:
        context.set_counter("source_ingress_bytes", ingress_bytes)

    pipeline = build_r2_iceberg_pipeline(
        dataset_name=f"bench_{context.scenario}",
        pipeline_name=f"bench_{context.scenario}",
    )

    # Grouped runs, mirroring run_pipeline: the Iceberg commit materializes
    # every load file of a table in memory, so rows per run must stay bounded.
    files_per_run = int(options.get("files_per_run") or DEFAULT_FILES_PER_RUN)
    groups = [files[i : i + files_per_run] for i in range(0, len(files), files_per_run)]

    with context.stage("dlt_stream_huggingface_parquet_to_r2_iceberg", groups=len(groups)):
        for index, group in enumerate(groups):
            resources = [
                lamda_samples(
                    files=group,
                    batch_size=batch_size,
                    limit_per_file=limit_per_file,
                )
            ]
            if index == 0:
                resources.insert(0, lamda_files(files))
            pipeline.run(
                resources,
                loader_file_format="parquet",
                table_format=TABLE_FORMAT,
                write_disposition="replace" if index == 0 else "append",
            )

    with context.stage("summarize_destination"):
        counts = _iceberg_counts(pipeline)
        snapshot = _iceberg_snapshot_metrics(pipeline)
        manifest_rows = _iceberg_snapshot_metrics(pipeline, "lamda_files")["rows"]

    context.set_counter("destination_dataset", pipeline.dataset_name)
    context.set_counter("destination_rows_by_partition", counts)
    context.set_counter("destination_rows", snapshot["rows"])
    context.set_counter("destination_egress_bytes", snapshot["bytes"])
    context.set_counter("transformed_rows", snapshot["rows"])
    context.set_counter("manifest_rows", manifest_rows)

    if options.get("drop_destination_dataset"):
        with context.stage("drop_destination_dataset"):
            _drop_iceberg_dataset(pipeline)


SCENARIOS = {
    "arrow_parquet_snowflake": benchmark_arrow_parquet_snowflake,
    "arrow_parquet_r2_iceberg": benchmark_arrow_parquet_r2_iceberg,
    "streaming_dlt_snowflake": benchmark_streaming_dlt_snowflake,
}


def scenario_names() -> list[str]:
    return sorted(SCENARIOS)


def get_scenario(name: str):
    return SCENARIOS[name]
