"""Browse local Android Iceberg logs: uv run --extra duckdb python scripts/duckdb_android_ui.py."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
import threading

import duckdb

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.load_loghub_android_local_iceberg import DEFAULT_LOCAL_ROOT, open_local_catalog


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-browser", action="store_true", help="Serve the UI without opening a browser")
    parser.add_argument("--port", type=int, default=4214, help="UI port (default: 4214)")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")

    catalog = open_local_catalog()
    try:
        metadata = catalog.load_table("raw_loghub.android_logs").metadata_location
    finally:
        catalog.close()

    # UI requests use separate connections, so configure their shared defaults.
    ui_home = DEFAULT_LOCAL_ROOT / "duckdb_ui"
    (ui_home / ".duckdb").mkdir(parents=True, exist_ok=True)
    with duckdb.connect(config={
        "home_directory": str(ui_home),
        "extension_directory": str(Path.home() / ".duckdb" / "extensions"),
    }) as con:
        con.sql("INSTALL iceberg; LOAD iceberg;")
        con.sql("ATTACH ':memory:' AS loghub")
        con.sql("USE loghub")
        con.sql("CREATE SCHEMA raw_loghub")
        path = metadata.replace("'", "''")
        con.sql(f"CREATE VIEW raw_loghub.android_logs AS SELECT * FROM iceberg_scan('{path}')")
        rows = con.sql("SELECT count(*) FROM raw_loghub.android_logs").fetchone()[0]
        print(f"loghub.raw_loghub.android_logs: {rows} rows (Iceberg-backed view)", flush=True)
        con.sql("INSTALL ui; LOAD ui;")
        con.sql(f"SET ui_local_port = {args.port}")
        con.sql("CALL start_ui_server()" if args.no_browser else "CALL start_ui()")
        if not con.sql("SELECT * FROM ui_is_started()").fetchone()[0]:
            raise SystemExit(f"Port {args.port} already serves another DuckDB UI; choose another --port.")
        print(f"DuckDB UI: http://localhost:{args.port} — Ctrl+C to stop", flush=True)
        print("Expand loghub → raw_loghub → android_logs in Attached databases.", flush=True)
        print("Restart this script after ingestion to view the latest snapshot.", flush=True)
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
