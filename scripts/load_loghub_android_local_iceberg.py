"""Download Loghub's Android sample and replace a local Iceberg table with it."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import BinaryIO
from urllib.request import Request, urlopen
from uuid import uuid4

import dlt
from dlt.destinations import filesystem
from pyiceberg.catalog import Catalog, load_catalog

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.load_lamda_local_iceberg import local_catalog_config


DEFAULT_SOURCE_URL = (
    "https://raw.githubusercontent.com/logpai/loghub/master/Android/Android_2k.log"
)
DEFAULT_LOCAL_ROOT = Path(__file__).resolve().parent.parent / "data" / "android_iceberg"
DEFAULT_DATASET_NAME = "raw_loghub"
TABLE_NAME = "android_logs"
CATALOG_NAME = "loghub_android_local"

# Loghub documents Android's format as Date, Time, PID, TID, Level, Component, Content.
# Component is absent in a few real-world log messages, so keep the raw line as the
# lossless source and expose nullable parsed fields for convenient queries.
ANDROID_LINE = re.compile(
    r"^(?P<date>\d{2}-\d{2})\s+"
    r"(?P<time>\d{2}:\d{2}:\d{2}\.\d+)\s+"
    r"(?P<pid>\d+)\s+(?P<tid>\d+)\s+"
    r"(?P<level>[A-Z])\s+"
    r"(?:(?P<component>[^:]+):\s*)?"
    r"(?P<content>.*)$"
)

OpenUrl = Callable[[Request], BinaryIO]


def open_local_catalog(local_root: Path = DEFAULT_LOCAL_ROOT) -> Catalog:
    """Reopen the Android catalog using the writer's catalog identity."""
    return load_catalog(CATALOG_NAME, **local_catalog_config(local_root))


def parse_android_line(line: str, line_number: int, source_url: str) -> dict[str, object]:
    """Return a lossless row plus nullable fields parsed from Loghub's log format."""
    match = ANDROID_LINE.match(line)
    parsed = match.groupdict() if match else {}
    return {
        "line_number": line_number,
        "date": parsed.get("date"),
        "time": parsed.get("time"),
        "pid": int(parsed["pid"]) if parsed.get("pid") else None,
        "tid": int(parsed["tid"]) if parsed.get("tid") else None,
        "level": parsed.get("level"),
        "component": parsed.get("component", "").strip() or None,
        "content": parsed.get("content"),
        "raw_line": line,
        "source_url": source_url,
    }


def android_rows(
    source_url: str = DEFAULT_SOURCE_URL,
    limit: int | None = None,
    *,
    opener: OpenUrl = urlopen,
) -> Iterator[dict[str, object]]:
    """Stream UTF-8 log lines over HTTPS without staging the source file on disk."""
    if limit is not None and limit < 1:
        raise ValueError("Row limit must be positive")
    request = Request(source_url, headers={"User-Agent": "snowflake-ml-pipeline/0.1"})
    with opener(request) as response:
        for line_number, raw_line in enumerate(response, start=1):
            if limit is not None and line_number > limit:
                break
            line = raw_line.decode("utf-8").rstrip("\r\n")
            yield parse_android_line(line, line_number, source_url)


def run_pipeline(
    *,
    source_url: str = DEFAULT_SOURCE_URL,
    local_root: Path = DEFAULT_LOCAL_ROOT,
    dataset_name: str = DEFAULT_DATASET_NAME,
    limit: int | None = None,
    opener: OpenUrl = urlopen,
) -> dict[str, object]:
    """Replace the Android table, verify its row count, and write a run report."""
    if limit is not None and limit < 1:
        raise ValueError("Row limit must be positive")
    if not re.fullmatch(r"[a-z][a-z0-9_]*", dataset_name):
        raise ValueError("Local dataset name must be lowercase letters, digits and underscores")
    if not source_url.startswith("https://"):
        raise ValueError("Source URL must use HTTPS")

    root = local_root.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    config = local_catalog_config(root)
    dlt.config["iceberg_catalog.iceberg_catalog_name"] = CATALOG_NAME
    dlt.config["iceberg_catalog.iceberg_catalog_type"] = "sql"
    dlt.config["iceberg_catalog.iceberg_catalog_config"] = config
    run_id = uuid4().hex
    pipeline = dlt.pipeline(
        pipeline_name="loghub_android_local_iceberg",
        pipelines_dir=str(root / ".dlt" / run_id),
        destination=filesystem(bucket_url=str(root / "warehouse")),
        dataset_name=dataset_name,
        progress="log",
    )
    columns = {
        "line_number": {"data_type": "bigint", "nullable": False},
        "date": {"data_type": "text"},
        "time": {"data_type": "text"},
        "pid": {"data_type": "bigint"},
        "tid": {"data_type": "bigint"},
        "level": {"data_type": "text"},
        "component": {"data_type": "text"},
        "content": {"data_type": "text"},
        "raw_line": {"data_type": "text", "nullable": False},
        "source_url": {"data_type": "text", "nullable": False},
    }
    resource = dlt.resource(
        android_rows(source_url, limit, opener=opener),
        name=TABLE_NAME,
        columns=columns,
        table_format="iceberg",
        write_disposition="replace",
    )
    info = pipeline.run(resource, loader_file_format="parquet")
    info.raise_on_failed_jobs()

    catalog = open_local_catalog(root)
    try:
        table = catalog.load_table((dataset_name, TABLE_NAME))
        rows = table.scan().count()
        metadata_location = table.metadata_location
        snapshot = table.current_snapshot()
    finally:
        catalog.close()
    report: dict[str, object] = {
        "run_id": run_id,
        "source_url": source_url,
        "local_namespace": dataset_name,
        "table": TABLE_NAME,
        "rows": rows,
        "iceberg_snapshot_id": snapshot.snapshot_id if snapshot else None,
        "local_metadata": metadata_location,
    }
    reports = root / "runs"
    reports.mkdir(exist_ok=True)
    report_path = reports / f"{run_id}.json"
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"Loaded {rows} Android log rows. Report: {report_path}", flush=True)
    return report


def positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return number


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-url", default=DEFAULT_SOURCE_URL)
    parser.add_argument("--local-root", type=Path, default=DEFAULT_LOCAL_ROOT)
    parser.add_argument("--dataset-name", default=DEFAULT_DATASET_NAME)
    parser.add_argument("--limit", type=positive_int, help="Load only the first N lines")
    run_pipeline(**vars(parser.parse_args()))


if __name__ == "__main__":
    main()
