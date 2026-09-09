# Download LAMDA from R2 into local Iceberg

`scripts/load_lamda_local_iceberg.py` uses dlt to refresh local Iceberg tables
from the existing `raw_lamda` namespace in Cloudflare R2 Data Catalog. It reads
through PyIceberg so snapshot selection, schema evolution and supported Iceberg
deletes are handled by the table engine. It does not download directly from
Hugging Face or copy obsolete Parquet files from the bucket.

## Run on Windows

From the repository root, install the locked dependencies:

```powershell
uv sync --locked
```

Use the Infisical CLI login on this machine (`infisical login` if needed). The
LAMDA credentials are in project `0cfed731-cdf4-46b8-b831-2d74be495575`, environment
`dev`, at the root secret path. Inject them into each command with
[`infisical run`](https://infisical.com/docs/cli/commands/run); no `.env` export is
needed. This project ID is configuration, not a secret.

The downloader reuses the existing R2 configuration: `R2_ACCOUNT_ID`, `R2_BUCKET`,
`R2_CATALOG_TOKEN`, and optional S3 credentials `R2_ACCESS_KEY_ID` and
`R2_SECRET_ACCESS_KEY`. Without explicit S3 credentials, the catalog must vend
credentials for reading the data. Catalog URI, warehouse and S3 endpoint
overrides match the existing R2 writer. Snowflake and Hugging Face credentials
are not used. Infisical supplies these variables only to the child process.

The source needs R2 storage read and R2 Data Catalog read permissions. See
[Cloudflare's read-only token documentation](https://developers.cloudflare.com/changelog/post/2026-07-09-r2-data-catalog-read-only-tokens/)
and the repository's [authentication guide](authentication.md#cloudflare-r2-and-r2-data-catalog).

Start with a small copy in a separate directory:

```powershell
infisical run --projectId=0cfed731-cdf4-46b8-b831-2d74be495575 --env=dev -- uv run python scripts/load_lamda_local_iceberg.py --local-root data/lamda_iceberg_smoke --limit-per-table 100
```

Download all rows:

```powershell
infisical run --projectId=0cfed731-cdf4-46b8-b831-2d74be495575 --env=dev -- uv run python scripts/load_lamda_local_iceberg.py
```

By default the downloader discovers every `lamda_*` table in `raw_lamda`,
including feature dictionaries when present. `lamda_samples` and `lamda_files`
must exist. Remote `_dlt_*` tables are excluded, and remote `_dlt_*` columns are
removed from the copied rows; dlt manages its own local load metadata. Business
columns such as `config_name`, `source_file`, `row_number` and `feat_*` are copied.

To select tables or change the location and chunk size:

```powershell
infisical run --projectId=0cfed731-cdf4-46b8-b831-2d74be495575 --env=dev -- uv run python scripts/load_lamda_local_iceberg.py --tables lamda_samples lamda_files --local-root "D:/datasets/LAMDA" --rows-per-run 10000
```

`--source-namespace` selects the R2 namespace; `--dataset-name` selects the local
namespace. Both default to `raw_lamda`. `--limit-per-table` must be positive and
produces a partial copy, replacing any existing selected local tables. Use a
separate root for smoke tests. Relative custom roots resolve from the current
working directory; the default root is relative to the repository.

## Local output and offline queries

The default root is `data/lamda_iceberg/`, already covered by `.gitignore`:

- `catalog.sqlite`: persistent Iceberg SQL catalog registrations.
- `warehouse/`: local Iceberg metadata, manifests and Parquet data, plus dlt metadata.
- `.dlt/<run-id>/`: dlt extraction/load state for each invocation.
- `runs/<run-id>.json`: successful refresh report containing source snapshot IDs,
  copied row counts and local metadata paths. A report appears only after all
  selected tables finish and their local row counts are verified.

Reopen the tables from another Python process without R2 credentials:

```python
from scripts.load_lamda_local_iceberg import open_local_catalog

catalog = open_local_catalog()  # or open_local_catalog(Path("D:/datasets/LAMDA"))
try:
    table = catalog.load_table("raw_lamda.lamda_samples")
    print(table.scan(selected_fields=("config_name", "row_number"), limit=10).to_arrow())
finally:
    catalog.close()
```

Keep the catalog and warehouse together at their original absolute paths.
Moving the directory requires rewriting the catalog and Iceberg file locations.
The SQLite database contains catalog metadata; the dataset itself is stored as
local Iceberg tables. The copy preserves current rows, not remote snapshot
history, field IDs or partition specifications.

The local catalog uses `scripts.local_iceberg_io.LocalFileIO` to handle Windows
drive letters and decode dlt's escaped file URLs. Keep this repository on the
Python import path when reopening the catalog with PyIceberg.

## Memory, refreshes and failures

Each source table's metadata is loaded before local writes, and its snapshot is
pinned for the scan. Concurrent source appends are picked up on the next run.
Snapshots are per table, not a transaction spanning the namespace. The source
catalog is used only for listing/loading/scanning; dlt receives a separate local
SQL catalog configuration.

The first chunk replaces a selected local table and later chunks append. This
makes reruns full refreshes without duplicate rows. Empty sources create or clear
local tables while retaining their schema. Unselected local tables are untouched.
Do not run two refreshes concurrently against the same local namespace.

A refresh is **not atomic** across chunks or tables. Readers can see a partial
copy during a refresh or after a failure. Rerun the command to restart the full
refresh; failed invocation state is isolated so pending appends are not replayed
into the next refresh. Old local Iceberg snapshots and per-run dlt state can
accumulate; this script does not expire snapshots or garbage-collect the warehouse.

dlt's Iceberg writer materializes a whole load package at commit time. The
default cap of 25,000 rows per run limits that work even for LAMDA's thousands
of columns. The reader passes one planned Iceberg file at a time to PyIceberg,
preventing its executor from prefetching the entire dataset. PyIceberg may still
materialize that file's Arrow batches, so this cap is **not a process memory
limit**; one decompressed source file and commit buffers need additional memory.
Allow disk space for data, staging and retained snapshots.

## Recover an interrupted sample download

When the four smaller tables are already complete, recover only missing
`lamda_samples` rows with `scripts/resume_lamda_local_iceberg.py`. It compares
the `(source_file, row_number)` keys in R2 and the local table, then appends only
the difference. It does not use scan order or a numeric offset. Duplicate/null
keys, local keys absent from the source, and a changed source snapshot stop the
operation before appending. This assumes the existing rows came from the named
snapshot; it verifies key coverage, not every existing feature value.

For the interrupted download observed in this checkout:

```powershell
infisical run --projectId=0cfed731-cdf4-46b8-b831-2d74be495575 --env=dev -- uv run python scripts/resume_lamda_local_iceberg.py --source-snapshot-id 7499953728686860711
```

Recovery uses 25,000-row commits by default. If it is interrupted again, rerun
the same command: committed keys are detected and skipped. Run only one writer
against this local table at a time. Successful recovery writes a report under
`data/lamda_iceberg/runs/` after checking exact key equality and uniqueness.
This report covers the recovered sample table; the four smaller tables are
left untouched. For a different interrupted source snapshot, pass its recorded
ID instead; never substitute a newer ID merely to bypass the snapshot check.

## Verify without R2

```powershell
uv run python -m unittest discover -s tests -v
```

Tests use real local Iceberg source/destination catalogs and dlt loads to check
snapshot pinning, deleted rows, nulls, empty tables, bounded chunks, repeat
refreshes, recovery after interrupted reads, table selection, row limits and
offline reopening in a separate Python process. They do not contact R2.

Implementation references: [dlt filesystem destination](https://dlthub.com/docs/dlt-ecosystem/destinations/filesystem),
[PyIceberg API](https://py.iceberg.apache.org/api/), and
[Cloudflare PyIceberg configuration](https://developers.cloudflare.com/r2-data-catalog/config-examples/pyiceberg/).
