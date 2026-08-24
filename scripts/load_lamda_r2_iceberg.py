"""Ingest the IQSeC-Lab/LAMDA Hugging Face dataset into Cloudflare R2 as Iceberg tables.

This is the R2 counterpart of `scripts/load_lamda.py`: it reuses the same Hugging
Face Arrow streaming resources, but writes to the `filesystem` destination backed
by an R2 bucket with `table_format="iceberg"`, registering tables in R2 Data
Catalog (an Iceberg REST catalog). Both pipelines exist side by side so their
throughput and cost can be compared.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from pathlib import Path
from typing import Any

import dlt
from dlt.common.configuration.specs import AwsCredentials
from dlt.destinations import filesystem as dlt_filesystem

if __package__ in (None, ""):
    # Allow `python scripts/load_lamda_r2_iceberg.py` alongside `python -m scripts...`
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.load_lamda import (
    DATASET_ID,
    DEFAULT_BATCH_SIZE,
    lamda_files,
    lamda_samples,
    parquet_manifest,
)


DEFAULT_DATASET_NAME = "raw_lamda"
PIPELINE_NAME = "lamda_huggingface_r2_iceberg"
TABLE_FORMAT = "iceberg"

# Unlike Snowflake, where COPY INTO does the heavy lifting server-side, the
# Iceberg path writes client-side: pyiceberg materializes each load file as a
# single in-memory Arrow table. One monolithic file for this dataset (~2M rows
# x ~4.5k columns) needs >14 GB and gets OOM-killed, so cap rows per load file
# and load-worker parallelism. Both are overridable via the environment.
MAX_ROWS_PER_LOAD_FILE = "200000"
MAX_LOAD_WORKERS = "2"


def apply_memory_limits() -> None:
    # Arrow resources are written to Parquet at EXTRACT time and normalize
    # passes those files through as-is, so the extract writer is the one that
    # must rotate files; the normalize setting is kept for non-arrow resources.
    os.environ.setdefault("EXTRACT__DATA_WRITER__FILE_MAX_ITEMS", MAX_ROWS_PER_LOAD_FILE)
    os.environ.setdefault("NORMALIZE__DATA_WRITER__FILE_MAX_ITEMS", MAX_ROWS_PER_LOAD_FILE)
    os.environ.setdefault("LOAD__WORKERS", MAX_LOAD_WORKERS)


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


def r2_bucket_url(bucket: str | None = None) -> str:
    """Base URL for table storage inside the bucket.

    R2 Data Catalog only accepts table locations under its storage profile,
    the reserved `__r2_data_catalog/` prefix, so table data lives there.
    """
    prefix = os.getenv("R2_CATALOG_PREFIX", "__r2_data_catalog").strip("/")
    return f"s3://{bucket or _require('R2_BUCKET')}/{prefix}"


def r2_s3_endpoint(account_id: str | None = None) -> str:
    """R2's S3-compatible endpoint, where Iceberg data and metadata files land."""
    return os.getenv("R2_S3_ENDPOINT") or (
        f"https://{account_id or _require('R2_ACCOUNT_ID')}.r2.cloudflarestorage.com"
    )


def r2_secret_access_key() -> str:
    """The S3 secret key for R2.

    Cloudflare documents that any API token with R2 permissions doubles as an
    S3 key pair: the access key ID is the token's ID and the secret access key
    is the SHA-256 hex digest of the token's value. When `R2_SECRET_ACCESS_KEY`
    is not set, derive it from `R2_CATALOG_TOKEN`/`CLOUDFLARE_API_TOKEN` —
    `R2_ACCESS_KEY_ID` must then be that same token's ID.
    """
    secret = os.getenv("R2_SECRET_ACCESS_KEY")
    if secret:
        return secret
    token = os.getenv("R2_CATALOG_TOKEN") or os.getenv("CLOUDFLARE_API_TOKEN")
    if token:
        return hashlib.sha256(token.encode()).hexdigest()
    return _require("R2_SECRET_ACCESS_KEY")


def r2_credentials() -> AwsCredentials:
    """R2 speaks the S3 API, so dlt's AWS credentials carry the R2 token."""
    return AwsCredentials(
        aws_access_key_id=_require("R2_ACCESS_KEY_ID"),
        aws_secret_access_key=r2_secret_access_key(),
        endpoint_url=r2_s3_endpoint(),
        region_name=os.getenv("R2_REGION", "auto"),
    )


def r2_catalog_config() -> dict[str, Any]:
    """Config for R2 Data Catalog, which is an Iceberg REST catalog.

    `R2_CATALOG_URI` and `R2_CATALOG_WAREHOUSE` are shown when you enable the
    catalog on a bucket; both are derived from the account and bucket when unset.
    """
    account_id = _require("R2_ACCOUNT_ID")
    bucket = _require("R2_BUCKET")

    config: dict[str, Any] = {
        "type": "rest",
        "uri": os.getenv("R2_CATALOG_URI")
        or f"https://catalog.cloudflarestorage.com/{account_id}/{bucket}",
        "warehouse": os.getenv("R2_CATALOG_WAREHOUSE") or f"{account_id}_{bucket}",
        "token": _require("R2_CATALOG_TOKEN"),
    }

    # Hand pyiceberg the same R2 keys for reading and writing the data files, so
    # the pipeline does not depend on the catalog vending credentials.
    access_key = os.getenv("R2_ACCESS_KEY_ID")
    secret_key = r2_secret_access_key() if access_key else None
    if access_key and secret_key:
        config.update(
            {
                "s3.endpoint": r2_s3_endpoint(account_id),
                "s3.access-key-id": access_key,
                "s3.secret-access-key": secret_key,
                "s3.region": os.getenv("R2_REGION", "auto"),
            }
        )

    return config


def apply_catalog_config(catalog_name: str = "r2_data_catalog") -> None:
    """Publish the catalog settings where dlt's `iceberg_catalog` section reads them."""
    dlt.config["iceberg_catalog.iceberg_catalog_name"] = catalog_name
    dlt.config["iceberg_catalog.iceberg_catalog_type"] = "rest"
    dlt.config["iceberg_catalog.iceberg_catalog_config"] = r2_catalog_config()


def r2_destination(**kwargs: Any):
    return dlt_filesystem(
        bucket_url=r2_bucket_url(),
        credentials=r2_credentials(),
        **kwargs,
    )


def build_pipeline(
    dataset_name: str = DEFAULT_DATASET_NAME,
    pipeline_name: str = PIPELINE_NAME,
    catalog_name: str = "r2_data_catalog",
) -> dlt.Pipeline:
    apply_memory_limits()
    apply_catalog_config(catalog_name)
    return dlt.pipeline(
        pipeline_name=pipeline_name,
        destination=r2_destination(),
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
        table_format=TABLE_FORMAT,
    )
    print(load_info)

    row_counts = pipeline.last_trace.last_normalize_info.row_counts
    loaded_rows = row_counts.get("lamda_samples", 0)
    print(
        f"Streamed {loaded_rows} rows from {len(files)} Hugging Face Parquet files "
        f"into Iceberg namespace {dataset_name} on {r2_bucket_url()}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Stream the IQSeC-Lab/LAMDA Hugging Face dataset into Cloudflare R2 as "
            "Iceberg tables with dlt."
        )
    )
    parser.add_argument(
        "--dataset-name",
        default=DEFAULT_DATASET_NAME,
        help="Destination Iceberg namespace. Defaults to raw_lamda.",
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
