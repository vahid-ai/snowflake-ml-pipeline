"""Recover an interrupted LAMDA sample download by comparing provenance keys."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from uuid import uuid4

import dlt
from dlt.destinations import filesystem
from pyiceberg.catalog import Catalog, load_catalog
from pyiceberg.expressions import (
    AlwaysFalse, And, EqualTo, GreaterThanOrEqual, LessThanOrEqual, Or,
)
from pyiceberg.table import Table

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.load_lamda_local_iceberg import (
    CATALOG_NAME, DEFAULT_LOCAL_ROOT, DEFAULT_NAMESPACE, local_catalog_config,
    open_local_catalog, positive_int, snapshot_chunks,
)
from scripts.load_lamda_r2_iceberg import r2_catalog_config


def key_inventory(table: Table) -> dict[str, set[int]]:
    """Read only provenance columns; reject null or duplicated source keys."""
    keys: dict[str, set[int]] = {}
    count = 0
    snapshot = table.current_snapshot()
    scan = table.scan(
        snapshot_id=snapshot.snapshot_id if snapshot else None,
        selected_fields=("source_file", "row_number"),
    )
    with scan.to_arrow_batch_reader() as reader:
        for batch in reader:
            for source_file, row_number in zip(
                batch.column(batch.schema.get_field_index("source_file")).to_pylist(),
                batch.column(batch.schema.get_field_index("row_number")).to_pylist(),
            ):
                if source_file is None or row_number is None:
                    raise ValueError("Cannot resume: null provenance key")
                keys.setdefault(source_file, set()).add(row_number)
                count += 1
    if sum(map(len, keys.values())) != count:
        raise ValueError("Cannot resume: duplicate (source_file, row_number) keys")
    return keys


def missing_keys(expected: dict[str, set[int]], present: dict[str, set[int]]) -> dict[str, set[int]]:
    for name, rows in present.items():
        if rows - expected.get(name, set()):
            raise ValueError("Cannot resume: local keys are absent from the pinned source snapshot")
    return {name: absent for name, rows in expected.items()
            if (absent := rows - present.get(name, set()))}


def missing_filter(missing: dict[str, set[int]]):
    """Express contiguous row ranges without depending on scan/file ordering."""
    conditions = []
    for name, numbers in missing.items():
        ordered = sorted(numbers)
        start = end = ordered[0]
        ranges = []
        for number in ordered[1:]:
            if number == end + 1:
                end = number
            else:
                ranges.append((start, end))
                start = end = number
        ranges.append((start, end))
        for start, end in ranges:
            conditions.append(And(
                EqualTo("source_file", name),
                And(GreaterThanOrEqual("row_number", start), LessThanOrEqual("row_number", end)),
            ))
    # Balance the expression tree so many holes do not produce deep recursion.
    while len(conditions) > 1:
        conditions = [Or(*conditions[i:i + 2]) if i + 1 < len(conditions) else conditions[i]
                      for i in range(0, len(conditions), 2)]
    return conditions[0] if conditions else AlwaysFalse()


def resume_samples(
    source_snapshot_id: int,
    *, local_root: Path = DEFAULT_LOCAL_ROOT,
    rows_per_run: int = 25_000,
    source_catalog: Catalog | None = None,
) -> dict:
    if rows_per_run < 1:
        raise ValueError("rows_per_run must be positive")
    source_catalog = source_catalog if source_catalog is not None else load_catalog(
        "lamda_r2_recovery", **r2_catalog_config()
    )
    source = source_catalog.load_table((DEFAULT_NAMESPACE, "lamda_samples"))
    snapshot = source.current_snapshot()
    if snapshot is None or snapshot.snapshot_id != source_snapshot_id:
        raise ValueError("Source snapshot changed; refusing to mix snapshots during recovery")
    root = local_root.expanduser().resolve()
    local = open_local_catalog(root)
    try:
        target = local.load_table((DEFAULT_NAMESPACE, "lamda_samples"))
        expected = key_inventory(source)
        present = key_inventory(target)
        missing = missing_keys(expected, present)
        before = sum(map(len, present.values()))
        remaining = sum(map(len, missing.values()))
        print(f"Verified {before:,} existing rows; {remaining:,} rows missing", flush=True)
        run_id = uuid4().hex
        if remaining:
            config = local_catalog_config(root)
            dlt.config["iceberg_catalog.iceberg_catalog_name"] = CATALOG_NAME
            dlt.config["iceberg_catalog.iceberg_catalog_type"] = "sql"
            dlt.config["iceberg_catalog.iceberg_catalog_config"] = config
            pipeline = dlt.pipeline(
                pipeline_name="lamda_r2_local_recovery",
                pipelines_dir=str(root / ".dlt" / run_id),
                destination=filesystem(bucket_url=str(root / "warehouse")),
                dataset_name=DEFAULT_NAMESPACE,
            )
            loaded = 0
            for chunk in snapshot_chunks(source, rows_per_run, row_filter=missing_filter(missing)):
                if not chunk.num_rows:
                    continue
                resource = dlt.resource([chunk], name="lamda_samples", table_format="iceberg",
                                        write_disposition="append")
                info = pipeline.run(resource, loader_file_format="parquet")
                info.raise_on_failed_jobs()
                loaded += chunk.num_rows
                print(f"lamda_samples: {before + loaded:,} rows stored ({loaded:,} recovered)", flush=True)
            if loaded != remaining:
                raise RuntimeError(f"Expected {remaining} missing rows, recovered {loaded}")
        target.refresh()
        actual = key_inventory(target)
        if actual != expected:
            raise RuntimeError("Recovered keys do not exactly match the source snapshot")
        report = {
            "run_id": run_id, "operation": "recover_missing_samples",
            "source_namespace": DEFAULT_NAMESPACE, "local_namespace": DEFAULT_NAMESPACE,
            "limit_per_table": None,
            "tables": [{"table": "lamda_samples", "source_snapshot_id": source_snapshot_id,
                        "rows": sum(map(len, actual.values())), "recovered_rows": remaining,
                        "local_metadata": target.metadata_location}],
        }
        reports = root / "runs"
        reports.mkdir(exist_ok=True)
        report_path = reports / f"{run_id}.json"
        report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(f"Recovery complete; exact provenance keys verified. Report: {report_path}", flush=True)
        return report
    finally:
        local.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-snapshot-id", type=int, required=True,
                        help="Source snapshot ID observed for the interrupted download")
    parser.add_argument("--local-root", type=Path, default=DEFAULT_LOCAL_ROOT)
    parser.add_argument("--rows-per-run", type=positive_int, default=25_000)
    resume_samples(**vars(parser.parse_args()))


if __name__ == "__main__":
    main()
