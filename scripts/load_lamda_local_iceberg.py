"""Refresh local Iceberg tables from snapshots in the LAMDA R2 catalog."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Iterator, Sequence
from pathlib import Path
from uuid import uuid4

import dlt
import pyarrow as pa
from dlt.destinations import filesystem
from pyiceberg.catalog import Catalog, load_catalog
from pyiceberg.expressions import AlwaysTrue, BooleanExpression
from pyiceberg.io.pyarrow import ArrowScan, schema_to_pyarrow
from pyiceberg.table import Table

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.load_lamda_r2_iceberg import r2_catalog_config


DEFAULT_LOCAL_ROOT = Path(__file__).resolve().parent.parent / "data" / "lamda_iceberg"
DEFAULT_ROWS_PER_RUN = 25_000
DEFAULT_NAMESPACE = "raw_lamda"
CATALOG_NAME = "lamda_local"


def local_catalog_config(local_root: Path) -> dict[str, str]:
    """Use absolute paths so the catalog can be reopened from any directory."""
    root = local_root.expanduser().resolve()
    return {
        "type": "sql",
        "uri": f"sqlite:///{(root / 'catalog.sqlite').as_posix()}",
        # fsspec handles Windows drive letters and literal spaces. PyIceberg's
        # Arrow FileIO currently interprets file:///C:/ as /C:/ on Windows.
        "warehouse": (root / "warehouse").as_uri(),
        "py-io-impl": "scripts.local_iceberg_io.LocalFileIO",
    }


def open_local_catalog(local_root: Path = DEFAULT_LOCAL_ROOT) -> Catalog:
    return load_catalog(CATALOG_NAME, **local_catalog_config(local_root))


def select_tables(
    catalog: Catalog, namespace: str, names: Sequence[str] | None
) -> list[Table]:
    """Resolve every source before starting a local refresh; never select dlt tables."""
    if not namespace or any(not part for part in namespace.split(".")):
        raise ValueError("Source namespace must contain nonempty dot-separated names")
    prefix = tuple(namespace.split("."))
    if names is None:
        names = sorted(
            identifier[-1]
            for identifier in catalog.list_tables(prefix)
            if identifier[-1].startswith("lamda_")
        )
        missing = {"lamda_samples", "lamda_files"} - set(names)
        if missing:
            raise ValueError(f"Missing required source tables in {namespace}: {sorted(missing)}")
    if not names:
        raise ValueError("Select at least one LAMDA table")
    for name in names:
        if not re.fullmatch(r"lamda_[a-z0-9_]+", name):
            raise ValueError(f"Expected an unqualified lamda_* table name, got {name!r}")
    return [catalog.load_table((*prefix, name)) for name in dict.fromkeys(names)]


def snapshot_chunks(
    table: Table, rows_per_run: int, limit: int | None = None,
    *, row_filter: BooleanExpression = AlwaysTrue(),
) -> Iterator[pa.Table]:
    """Scan a pinned Iceberg snapshot (including deletes), yielding bounded loads.

    PyIceberg controls the underlying read batch size. This cap controls the
    rows materialized by each dlt Iceberg commit, not total process memory.
    Always yield the schema, including for tables without any visible rows.
    """
    if rows_per_run < 1 or (limit is not None and limit < 1):
        raise ValueError("Row counts must be positive")
    snapshot = table.current_snapshot()
    columns = tuple(c.name for c in table.schema().columns if not c.name.startswith("_dlt_"))
    scan = table.scan(
        snapshot_id=snapshot.snapshot_id if snapshot else None,
        selected_fields=columns,
        limit=limit,
        row_filter=row_filter,
    )
    schema = schema_to_pyarrow(scan.projection())

    def read_files() -> Iterator[pa.RecordBatch]:
        # PyIceberg 0.11's batch reader eagerly submits every file to an
        # executor and materializes each file's batches. Passing one planned
        # task at a time prevents the entire wide dataset from accumulating
        # while dlt commits chunks. Keep PyIceberg's delete/schema handling.
        remaining = limit
        for task in scan.plan_files():
            if remaining is not None and remaining <= 0:
                break
            arrow_scan = ArrowScan(
                scan.table_metadata, table.io, scan.projection(), scan.row_filter,
                scan.case_sensitive, remaining,
            )
            for batch in arrow_scan.to_record_batches([task]):
                yield batch
                if remaining is not None:
                    remaining -= batch.num_rows

    with pa.RecordBatchReader.from_batches(schema, read_files()).cast(schema) as reader:
        batches: list[pa.RecordBatch] = []
        rows = 0
        emitted = False
        for batch in reader:
            offset = 0
            while offset < batch.num_rows:
                length = min(rows_per_run - rows, batch.num_rows - offset)
                batches.append(batch.slice(offset, length))
                rows += length
                offset += length
                if rows == rows_per_run:
                    yield pa.Table.from_batches(batches, schema=reader.schema)
                    emitted = True
                    batches, rows = [], 0
        if batches or not emitted:
            yield pa.Table.from_batches(batches, schema=reader.schema)


def run_pipeline(
    *,
    local_root: Path = DEFAULT_LOCAL_ROOT,
    source_namespace: str = DEFAULT_NAMESPACE,
    dataset_name: str = DEFAULT_NAMESPACE,
    tables: Sequence[str] | None = None,
    rows_per_run: int = DEFAULT_ROWS_PER_RUN,
    limit_per_table: int | None = None,
    source_catalog: Catalog | None = None,
) -> dict:
    """Fully refresh selected tables. Rerunning starts a new refresh after failure.

    A refresh commits in chunks and is not atomic across chunks or tables.
    The source catalog is read-only; only the local catalog is passed to dlt.
    """
    if rows_per_run < 1 or (limit_per_table is not None and limit_per_table < 1):
        raise ValueError("Row counts must be positive")
    if not re.fullmatch(r"[a-z][a-z0-9_]*", dataset_name):
        raise ValueError("Local dataset name must be lowercase letters, digits and underscores")
    catalog = source_catalog if source_catalog is not None else load_catalog(
        "lamda_r2_source", **r2_catalog_config()
    )
    sources = select_tables(catalog, source_namespace, tables)
    root = local_root.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    config = local_catalog_config(root)
    # Explicit destination config prevents accidental use of the R2 writer's
    # REST catalog. The source catalog above is a separate PyIceberg object.
    dlt.config["iceberg_catalog.iceberg_catalog_name"] = CATALOG_NAME
    dlt.config["iceberg_catalog.iceberg_catalog_type"] = "sql"
    dlt.config["iceberg_catalog.iceberg_catalog_config"] = config
    run_id = uuid4().hex
    pipeline = dlt.pipeline(
        pipeline_name="lamda_r2_local_iceberg",
        pipelines_dir=str(root / ".dlt" / run_id),
        destination=filesystem(bucket_url=str(root / "warehouse")),
        dataset_name=dataset_name,
        progress="log",
    )
    # Keep staging state per invocation so failed pending appends cannot be
    # replayed into the next full refresh. Failed state remains for diagnosis.
    report = {
        "run_id": run_id,
        "source_namespace": source_namespace,
        "local_namespace": dataset_name,
        "catalog_uri": config["uri"],
        "limit_per_table": limit_per_table,
        "tables": [],
    }
    local = open_local_catalog(root)
    try:
        for source in sources:
            name = source.name()[-1]
            snapshot = source.current_snapshot()
            rows = 0
            for index, chunk in enumerate(snapshot_chunks(source, rows_per_run, limit_per_table)):
                resource = dlt.resource(
                    [chunk], name=name, table_format="iceberg",
                    write_disposition="replace" if index == 0 else "append",
                )
                info = pipeline.run(resource, loader_file_format="parquet")
                info.raise_on_failed_jobs()
                rows += chunk.num_rows
                print(f"{name}: {rows} rows loaded", flush=True)
            target = local.load_table((dataset_name, name))
            actual_rows = target.scan().count()
            if actual_rows != rows:
                raise RuntimeError(f"Row count mismatch for {name}: extracted {rows}, local {actual_rows}")
            report["tables"].append({
                "table": name,
                "source_snapshot_id": snapshot.snapshot_id if snapshot else None,
                "rows": rows,
                "local_metadata": target.metadata_location,
            })
    finally:
        local.close()
    reports = root / "runs"
    reports.mkdir(exist_ok=True)
    report_path = reports / f"{run_id}.json"
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"Completed local refresh. Report: {report_path}", flush=True)
    return report


def positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return number


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local-root", type=Path, default=DEFAULT_LOCAL_ROOT,
                        help="Folder for catalog.sqlite, warehouse, dlt state and run reports")
    parser.add_argument("--source-namespace", default=DEFAULT_NAMESPACE)
    parser.add_argument("--dataset-name", default=DEFAULT_NAMESPACE, help="Local Iceberg namespace")
    parser.add_argument("--tables", nargs="+", help="Explicit lamda_* tables; default: all LAMDA tables")
    parser.add_argument("--rows-per-run", type=positive_int, default=DEFAULT_ROWS_PER_RUN,
                        help="Maximum rows per dlt commit (default: 25000)")
    parser.add_argument("--limit-per-table", type=positive_int,
                        help="Copy at most this many rows per table for a smoke test")
    args = parser.parse_args()
    run_pipeline(**vars(args))


if __name__ == "__main__":
    main()
