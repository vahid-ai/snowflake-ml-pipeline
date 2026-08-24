---
name: r2-data-catalog
description: >
  Write and read Apache Iceberg tables in Cloudflare R2 via R2 Data Catalog
  (the Iceberg REST catalog on an R2 bucket). Use this skill whenever the task
  involves R2 Data Catalog, Iceberg on R2, dlt/PyIceberg/Spark/Trino pointed at
  an R2 bucket, enabling a catalog with wrangler, or any error mentioning
  "InvalidLocation" or "storage profile" from catalog.cloudflarestorage.com.
  Critically: table locations MUST live under the bucket's reserved
  `__r2_data_catalog/` prefix — writers that derive their own table paths
  (like dlt's filesystem destination) fail against the real service in a way
  local Iceberg testing never surfaces.
---

# R2 Data Catalog: Iceberg on Cloudflare R2

## The bug this skill exists to prevent

R2 Data Catalog enforces a **storage profile**: every table's location must be
a sublocation of `s3://<bucket>/__r2_data_catalog`. A `CreateTable` whose
location is anywhere else in the bucket is rejected with:

```
InvalidLocation: Provided location s3://<bucket>/<dataset>/<table> is not a
valid sublocation of the storage profile s3://<bucket>/__r2_data_catalog
```

Any writer that computes its own table paths trips this. dlt's `filesystem`
destination with `table_format="iceberg"` derives table locations from
`bucket_url`, so **point `bucket_url` at the prefix, not the bucket root**:

```python
bucket_url = f"s3://{bucket}/__r2_data_catalog"   # NOT s3://{bucket}
```

Keep the prefix configurable (e.g. an `R2_CATALOG_PREFIX` env var defaulting
to `__r2_data_catalog`) in case Cloudflare changes the convention.

Two things make this bug easy to ship:
- **Local testing cannot catch it.** A local filesystem bucket with a SQL
  catalog accepts any location; only the live REST catalog enforces the
  profile. Treat "works locally" as unproven until one real smoke load runs.
- **The failure is late and partial.** Data files upload fine (plain S3
  writes); only the catalog registration step fails. That can leave orphaned
  Parquet at the invalid location outside the prefix — after fixing the
  config, list and delete those stale objects, since nothing references them.

## Connection values

Enabling the catalog prints everything needed (`npx wrangler r2 bucket
catalog enable <bucket>`); the values also follow a derivable convention:

| Setting | Value |
| --- | --- |
| Catalog URI | `https://catalog.cloudflarestorage.com/<account_id>/<bucket>` |
| Warehouse | `<account_id>_<bucket>` |
| Auth | `token = <Cloudflare API token with R2 Data Catalog permission>` |
| S3 endpoint (data files) | `https://<account_id>.r2.cloudflarestorage.com`, region `auto` |

PyIceberg connection:

```python
from pyiceberg.catalog.rest import RestCatalog
catalog = RestCatalog(name="r2", uri=CATALOG_URI, warehouse=WAREHOUSE, token=TOKEN)
```

## One token can serve both planes

Reading/writing the data files needs S3 credentials; the catalog needs a
bearer token. These can be the same underlying token: for any Cloudflare API
token with R2 permissions, the S3 **access key ID is the token's ID** and the
**secret access key is the SHA-256 hex digest of the token's value**
(documented by Cloudflare). So a single API token with `Workers R2 Storage:
Edit` + `R2 Data Catalog: Edit` covers catalog auth, wrangler, and S3.
Don't rely on the catalog vending storage credentials to clients — pass S3
keys explicitly (PyIceberg fileio properties `s3.endpoint`,
`s3.access-key-id`, `s3.secret-access-key`, `s3.region`).

## Verification pattern

After a load, verify through the catalog as an external reader would — not by
trusting the writer's exit status:

```python
catalog.list_namespaces()
catalog.list_tables("<namespace>")
table = catalog.load_table("<namespace>.<table>")
table.scan(selected_fields=(...)).to_arrow()      # select few columns on wide tables
table.current_snapshot().summary["total-records"]  # authoritative row count
```

The snapshot summary (`total-records`, `total-files-size`) is the cheap,
authoritative source for row/byte counts — no full scan needed. On wide
tables (thousands of columns), never `to_arrow()` the whole schema just to
count things; select the two or three columns you need.

## Operational notes

- Enabling the catalog on a bucket is one-time (`wrangler r2 bucket catalog
  enable`); creating buckets and enabling the catalog both work
  non-interactively with `CLOUDFLARE_API_TOKEN` + `CLOUDFLARE_ACCOUNT_ID`.
- Namespaces are created by writers (dlt creates one per dataset); dropping a
  namespace requires dropping its tables first.
- dlt config for the catalog goes through the `iceberg_catalog` section:
  `iceberg_catalog_type = "rest"` and an `iceberg_catalog_config` dict of
  `{type, uri, warehouse, token, s3.*}`.
