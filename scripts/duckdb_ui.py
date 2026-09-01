"""Browse the R2 Data Catalog Iceberg tables in the DuckDB UI.

The read-only companion to `scripts/load_lamda_r2_iceberg.py`: that script
writes the LAMDA tables to Cloudflare R2 as Iceberg, this one attaches the same
REST catalog in DuckDB and opens the DuckDB UI at <http://localhost:4213>, so
the tables can be browsed and queried with SQL.

Like `datahub/ingest.sh`, it derives the catalog URI and warehouse from
`R2_ACCOUNT_ID`/`R2_BUCKET` exactly as the loader does, so it runs unchanged
under Infisical:

    infisical run --projectId=<id> --env=dev -- python scripts/duckdb_ui.py

R2 Data Catalog vends storage credentials to the client, so the catalog token
is the only secret needed — the `R2_ACCESS_KEY_ID`/`R2_SECRET_ACCESS_KEY` pair
the loader uses for writing is not required to read.

`duckdb` is the only dependency, deliberately: it is not one of the project's
dlt/pyiceberg requirements, so this runs on any interpreter that has it.
"""

from __future__ import annotations

import argparse
import os
import threading

import duckdb

CATALOG_ALIAS = "r2"
UI_URL = "http://localhost:4213"


class R2ConfigurationError(RuntimeError):
    """Raised when the R2 environment is incompletely configured."""


def _require(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise R2ConfigurationError(
            f"`{name}` is not set. See docs/authentication.md for the R2 variables "
            "the Iceberg pipeline needs."
        )
    return value


def r2_catalog_uri(account_id: str, bucket: str) -> str:
    """R2 Data Catalog's Iceberg REST endpoint, shown when the catalog is enabled."""
    return os.getenv("R2_CATALOG_URI") or (
        f"https://catalog.cloudflarestorage.com/{account_id}/{bucket}"
    )


def r2_catalog_warehouse(account_id: str, bucket: str) -> str:
    """Warehouse name for the catalog; derived from account and bucket when unset."""
    return os.getenv("R2_CATALOG_WAREHOUSE") or f"{account_id}_{bucket}"


def attach_catalog(con: duckdb.DuckDBPyConnection, alias: str = CATALOG_ALIAS) -> str:
    """Attach R2 Data Catalog to `con` as `alias`, returning the catalog URI."""
    account_id = _require("R2_ACCOUNT_ID")
    bucket = _require("R2_BUCKET")
    uri = r2_catalog_uri(account_id, bucket)

    con.sql("INSTALL iceberg; LOAD iceberg;")
    # The token is interpolated rather than bound: CREATE SECRET does not take
    # prepared-statement parameters. It stays in memory; nothing is persisted.
    con.sql(
        "CREATE OR REPLACE SECRET r2_catalog "
        f"(TYPE ICEBERG, TOKEN '{_require('R2_CATALOG_TOKEN')}')"
    )
    con.sql(
        f"ATTACH IF NOT EXISTS '{r2_catalog_warehouse(account_id, bucket)}' "
        f"AS {alias} (TYPE ICEBERG, ENDPOINT '{uri}')"
    )
    return uri


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Attach the R2 Data Catalog Iceberg tables in DuckDB and open the "
            "DuckDB UI to browse them."
        )
    )
    parser.add_argument(
        "--alias",
        default=CATALOG_ALIAS,
        help=f"Name to attach the catalog under. Defaults to {CATALOG_ALIAS}.",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Serve the UI without opening a browser window.",
    )
    args = parser.parse_args()

    # In-memory: the catalog is remote and the UI is a viewer, so there is no
    # local database worth persisting between runs.
    con = duckdb.connect()
    uri = attach_catalog(con, args.alias)

    tables = con.sql(
        "SELECT database, schema, name FROM (SHOW ALL TABLES) "
        f"WHERE database = '{args.alias}' ORDER BY schema, name"
    ).fetchall()
    print(f"attached {args.alias} -> {uri} ({len(tables)} tables)")
    for _, schema, name in tables:
        print(f"  {schema}.{name}")

    con.sql("INSTALL ui; LOAD ui;")
    con.sql("CALL start_ui_server()" if args.no_browser else "CALL start_ui()")
    print(f"\nDuckDB UI at {UI_URL} — Ctrl+C to stop")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
