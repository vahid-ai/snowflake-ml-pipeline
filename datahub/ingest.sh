#!/usr/bin/env bash
# Ingest R2 Data Catalog (Iceberg) metadata into DataHub.
#
# Reads the same R2_* variables as scripts/load_lamda_r2_iceberg.py and derives
# the same defaults, so it runs unchanged under `infisical run`:
#   infisical run --projectId=<id> --env=dev -- datahub/ingest.sh
set -euo pipefail

: "${R2_ACCOUNT_ID:?R2_ACCOUNT_ID is required}"
: "${R2_BUCKET:?R2_BUCKET is required}"
: "${R2_CATALOG_TOKEN:?R2_CATALOG_TOKEN is required}"
: "${R2_ACCESS_KEY_ID:?R2_ACCESS_KEY_ID is required}"
: "${R2_SECRET_ACCESS_KEY:?R2_SECRET_ACCESS_KEY is required}"

export R2_CATALOG_URI="${R2_CATALOG_URI:-https://catalog.cloudflarestorage.com/${R2_ACCOUNT_ID}/${R2_BUCKET}}"
export R2_CATALOG_WAREHOUSE="${R2_CATALOG_WAREHOUSE:-${R2_ACCOUNT_ID}_${R2_BUCKET}}"
export R2_S3_ENDPOINT="${R2_S3_ENDPOINT:-https://${R2_ACCOUNT_ID}.r2.cloudflarestorage.com}"
export R2_REGION="${R2_REGION:-auto}"
export DATAHUB_GMS_URL="${DATAHUB_GMS_URL:-http://localhost:8080}"

exec datahub ingest -c "$(dirname "$0")/iceberg_r2.dhub.yml"
