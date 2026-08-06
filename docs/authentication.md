# Snowflake, R2, and Hugging Face authentication

This project authenticates in three places:

- **Snowflake**, for the dlt ingestion pipeline (`scripts/load_lamda.py`, `bench/`)
  and for dbt (`profiles.yml`).
- **Cloudflare R2**, for the Iceberg pipeline
  (`scripts/load_lamda_r2_iceberg.py`) — see
  [Cloudflare R2 and R2 Data Catalog](#cloudflare-r2-and-r2-data-catalog).
- **Hugging Face**, for reading the source dataset. Public datasets need nothing.

All of them read environment variables, so one `.env` configures the whole
project. Copy `.env.example` to `.env` and fill in the destination you are
running, or both destinations to benchmark them against each other.

## How credentials are resolved

`snowflake_credentials()` in `scripts/load_lamda.py` collects the `SNOWFLAKE_*`
variables, drops the empty ones, and hands the rest to dlt's Snowflake
destination. If **none** are set it returns `None`, which lets dlt fall back to
its own config providers — see [dlt config providers](#alternative-dlt-config-providers).

`profiles.yml` renders the same variables into the dbt Snowflake profile with
`env_var`, defaulting unused ones to an empty string. dbt-snowflake ignores
empty values, so unused auth fields never reach the connector.

| Variable | Used for |
| --- | --- |
| `SNOWFLAKE_ACCOUNT` | Account identifier, all methods |
| `SNOWFLAKE_USER` | Login name, all methods except some OAuth flows |
| `SNOWFLAKE_DATABASE` | Target database, all methods |
| `SNOWFLAKE_WAREHOUSE` | Warehouse for loading and dbt, all methods |
| `SNOWFLAKE_ROLE` | Role to assume, all methods |
| `SNOWFLAKE_SCHEMA` | dbt target schema, defaults to `analytics` |
| `SNOWFLAKE_PASSWORD` | Password auth |
| `SNOWFLAKE_PRIVATE_KEY_PATH` | Key-pair auth, path to a PKCS#8 PEM key |
| `SNOWFLAKE_PRIVATE_KEY` | Key-pair auth with an inline key (ingestion only) |
| `SNOWFLAKE_PRIVATE_KEY_PASSPHRASE` | Key-pair auth, when the key is encrypted |
| `SNOWFLAKE_AUTHENTICATOR` | Selects a non-password method |
| `SNOWFLAKE_TOKEN` | Token for `programmatic_access_token` and `oauth` |
| `HF_TOKEN` | Gated or private Hugging Face datasets |

The account identifier goes in as `<orgname>-<account_name>` (preferred) or the
legacy `<locator>.<region>` form, e.g. `abc12345.us-east-1`. Do not include
`.snowflakecomputing.com`.

## Which method should I use?

| Method | Ingestion (dlt) | dbt | Best for |
| --- | --- | --- | --- |
| [Key pair](#key-pair-recommended) | Yes | Yes | Automation, CI, scheduled loads |
| [Programmatic access token](#programmatic-access-token-pat) | Yes | Yes | Automation where your org prefers tokens over keys |
| [Password](#password) | Yes | Yes | Legacy accounts only; blocked by MFA policy on most |
| [Password + MFA](#password--mfa) | Yes | Yes | Interactive human use on MFA-enforced accounts |
| [External browser SSO](#external-browser-sso) | Yes | Yes | Interactive local development against an SSO account |
| [OAuth](#oauth) | Yes | Yes | An external IdP or Snowpark Container Services |
| [Okta native](#okta-native) | Yes | Yes | Okta-federated accounts, non-interactive |

Key pair is the default recommendation: Snowflake blocks password-only sign-in
for programmatic users under its MFA policy, so a plain password on a loader
account will usually be rejected.

Workload identity is **not** wired up. dlt has no field for
`workload_identity_provider`, and `profiles.yml` does not render the extra keys
dbt-snowflake requires for it.

## Common setup: role, warehouse, database

Every method needs a role that can create schemas in the target database, since
dlt creates `raw_lamda` (and `bench_*` for the benchmark suite) itself.

```sql
USE ROLE ACCOUNTADMIN;

CREATE ROLE IF NOT EXISTS LAMDA_LOADER;
CREATE WAREHOUSE IF NOT EXISTS LOADING_WH
  WITH WAREHOUSE_SIZE = XSMALL AUTO_SUSPEND = 60 AUTO_RESUME = TRUE;
CREATE DATABASE IF NOT EXISTS LAMDA;

GRANT USAGE ON WAREHOUSE LOADING_WH TO ROLE LAMDA_LOADER;
GRANT USAGE, CREATE SCHEMA ON DATABASE LAMDA TO ROLE LAMDA_LOADER;
```

The role owns whatever it creates, so if dbt runs under the same role it needs
no further grants. If dbt runs under a *different* role, give that role read
access to what the loader creates:

```sql
GRANT USAGE ON FUTURE SCHEMAS IN DATABASE LAMDA TO ROLE ANALYTICS_ROLE;
GRANT SELECT ON FUTURE TABLES IN DATABASE LAMDA TO ROLE ANALYTICS_ROLE;
```

## Key pair (recommended)

### 1. Generate the key pair

```bash
mkdir -p ~/.snowflake && cd ~/.snowflake

# Encrypted (recommended) — prompts for a passphrase
openssl genrsa 2048 | openssl pkcs8 -topk8 -inform PEM -out lamda_loader.p8

# Or unencrypted, if you have nowhere safe to keep the passphrase
# openssl genrsa 2048 | openssl pkcs8 -topk8 -inform PEM -nocrypt -out lamda_loader.p8

chmod 600 lamda_loader.p8
openssl rsa -in lamda_loader.p8 -pubout -out lamda_loader.pub
```

The key must be **PKCS#8 PEM** — `-----BEGIN PRIVATE KEY-----` or
`-----BEGIN ENCRYPTED PRIVATE KEY-----`, minimum 2048 bits. dlt reads
`private_key_path` as ASCII text, so a binary DER file fails with an explicit
"binary formats are not supported" error.

### 2. Create the user with the public key

Paste the public key as a single line, without the `BEGIN`/`END` markers and
without newlines:

```sql
CREATE USER IF NOT EXISTS LAMDA_LOADER
  TYPE = SERVICE                    -- service users cannot use passwords, which is the point
  DEFAULT_ROLE = LAMDA_LOADER
  DEFAULT_WAREHOUSE = LOADING_WH
  RSA_PUBLIC_KEY = 'MIIBIjANBgkqh...';

GRANT ROLE LAMDA_LOADER TO USER LAMDA_LOADER;
```

### 3. Verify the key registered

```sql
DESC USER LAMDA_LOADER;  -- read RSA_PUBLIC_KEY_FP
```

```bash
openssl rsa -pubin -in lamda_loader.pub -outform DER \
  | openssl dgst -sha256 -binary | openssl enc -base64
```

Snowflake shows that value prefixed with `SHA256:`.

### 4. Configure

```bash
SNOWFLAKE_ACCOUNT=myorg-my_account
SNOWFLAKE_USER=LAMDA_LOADER
SNOWFLAKE_PRIVATE_KEY_PATH=/home/you/.snowflake/lamda_loader.p8
SNOWFLAKE_PRIVATE_KEY_PASSPHRASE=...     # only if the key is encrypted
SNOWFLAKE_DATABASE=LAMDA
SNOWFLAKE_WAREHOUSE=LOADING_WH
SNOWFLAKE_ROLE=LAMDA_LOADER
```

Leave `SNOWFLAKE_AUTHENTICATOR` unset — the connector selects key-pair auth
whenever a private key is present.

### Inline keys for CI

Where writing a key file is awkward, the **ingestion pipeline** also accepts the
key inline through `SNOWFLAKE_PRIVATE_KEY`, as either PEM text or base64-encoded
DER. `profiles.yml` wires only `private_key_path`, so for dbt with an inline key
add a `private_key:` line to the profile:

```yaml
      private_key: "{{ env_var('SNOWFLAKE_PRIVATE_KEY', '') }}"
```

### Rotation

Snowflake holds two key slots. Add the new key as `RSA_PUBLIC_KEY_2`, switch
clients over, then retire the old one:

```sql
ALTER USER LAMDA_LOADER SET RSA_PUBLIC_KEY_2 = 'MIIBIjANBgkqh...';
-- once every client uses the new key
ALTER USER LAMDA_LOADER SET RSA_PUBLIC_KEY = 'MIIBIjANBgkqh...';
ALTER USER LAMDA_LOADER UNSET RSA_PUBLIC_KEY_2;
```

## Programmatic access token (PAT)

### 1. Mint the token

```sql
ALTER USER LAMDA_LOADER ADD PROGRAMMATIC ACCESS TOKEN lamda_loader_pat
  ROLE_RESTRICTION = 'LAMDA_LOADER'
  DAYS_TO_EXPIRY = 90;
```

The token value is shown **once**, at creation. Most accounts also require a
network policy on the user or the account before PAT authentication is allowed;
if the token is rejected outright, that is the first thing to check with your
Snowflake admin.

### 2. Configure

```bash
SNOWFLAKE_ACCOUNT=myorg-my_account
SNOWFLAKE_USER=LAMDA_LOADER
SNOWFLAKE_AUTHENTICATOR=programmatic_access_token
SNOWFLAKE_TOKEN=<the token printed by ALTER USER>
SNOWFLAKE_DATABASE=LAMDA
SNOWFLAKE_WAREHOUSE=LOADING_WH
SNOWFLAKE_ROLE=LAMDA_LOADER
```

Write the authenticator in **lowercase**. dbt-snowflake compares it
case-sensitively when deciding whether to forward `token`, so an uppercase value
silently drops the token for dbt while still working for the ingestion pipeline.

Tokens expire on the schedule you set. To rotate, add a second token, swap
`SNOWFLAKE_TOKEN`, then remove the old one:

```sql
ALTER USER LAMDA_LOADER REMOVE PROGRAMMATIC ACCESS TOKEN lamda_loader_pat;
```

## Password

Only viable on accounts that still permit single-factor password sign-in for
the user in question.

```sql
CREATE USER IF NOT EXISTS LAMDA_LOADER
  PASSWORD = '...'
  DEFAULT_ROLE = LAMDA_LOADER
  DEFAULT_WAREHOUSE = LOADING_WH
  MUST_CHANGE_PASSWORD = FALSE;

GRANT ROLE LAMDA_LOADER TO USER LAMDA_LOADER;
```

```bash
SNOWFLAKE_ACCOUNT=myorg-my_account
SNOWFLAKE_USER=LAMDA_LOADER
SNOWFLAKE_PASSWORD=...
SNOWFLAKE_DATABASE=LAMDA
SNOWFLAKE_WAREHOUSE=LOADING_WH
SNOWFLAKE_ROLE=LAMDA_LOADER
```

Leave `SNOWFLAKE_AUTHENTICATOR` unset. This is the one method where dlt requires
both a username and a password, and it will refuse to build the destination
without them.

## Password + MFA

For a human account with MFA enrolled, add the authenticator so the connector
performs the MFA exchange and caches the MFA token:

```bash
SNOWFLAKE_USER=your.name@example.com
SNOWFLAKE_PASSWORD=...
SNOWFLAKE_AUTHENTICATOR=username_password_mfa
```

Expect an MFA prompt on first connection. Not suitable for scheduled runs.

## External browser SSO

For local development against an SSO-federated account:

```bash
SNOWFLAKE_USER=your.name@example.com
SNOWFLAKE_AUTHENTICATOR=externalbrowser
```

No password or key is needed — the connector opens a browser for the IdP login.
This cannot work headlessly, so keep it out of CI and the benchmark suite.

## OAuth

With a token from your identity provider:

```bash
SNOWFLAKE_USER=your.name@example.com
SNOWFLAKE_AUTHENTICATOR=oauth
SNOWFLAKE_TOKEN=<access token>
```

Two caveats:

- **Ingestion:** setting `SNOWFLAKE_AUTHENTICATOR=oauth` *without* a token makes
  dlt look for a Snowflake-provided session token at `/snowflake/session/token`,
  which only exists inside Snowpark Container Services. Anywhere else it fails
  with "Snowflake-provided OAuth token not available". Inside SPCS, leave
  `SNOWFLAKE_TOKEN` unset and let dlt read the short-lived token itself.
- **dbt:** `profiles.yml` passes the token straight through, which suits an
  access token. For the refresh-token flow, dbt needs the client credentials as
  well, so add them to the profile:

  ```yaml
        oauth_client_id: "{{ env_var('SNOWFLAKE_OAUTH_CLIENT_ID', '') }}"
        oauth_client_secret: "{{ env_var('SNOWFLAKE_OAUTH_CLIENT_SECRET', '') }}"
  ```

## Okta native

For Okta-federated accounts that allow native (non-browser) sign-in, set the
authenticator to your Okta URL:

```bash
SNOWFLAKE_USER=your.name@example.com
SNOWFLAKE_PASSWORD=...
SNOWFLAKE_AUTHENTICATOR=https://myorg.okta.com
```

The connector passes any `https://` authenticator through as an Okta endpoint.

## Alternative: dlt config providers

The ingestion side does not have to use `SNOWFLAKE_*` at all. When none of them
are set, dlt falls back to its own providers, e.g. `.dlt/secrets.toml`:

```toml
[destination.snowflake.credentials]
host = "myorg-my_account"          # account identifier
database = "LAMDA"
username = "LAMDA_LOADER"
warehouse = "LOADING_WH"
role = "LAMDA_LOADER"
private_key_path = "/home/you/.snowflake/lamda_loader.p8"
```

or the equivalent environment variables:

```bash
DESTINATION__SNOWFLAKE__CREDENTIALS__HOST=myorg-my_account
DESTINATION__SNOWFLAKE__CREDENTIALS__DATABASE=LAMDA
DESTINATION__SNOWFLAKE__CREDENTIALS__USERNAME=LAMDA_LOADER
DESTINATION__SNOWFLAKE__CREDENTIALS__PRIVATE_KEY_PATH=/home/you/.snowflake/lamda_loader.p8
```

Note that dlt calls the account identifier `host`. `.dlt/` is git-ignored. This
fallback covers the loader and benchmarks only — dbt always reads `SNOWFLAKE_*`
through `profiles.yml`.

## Cloudflare R2 and R2 Data Catalog

The Iceberg pipeline authenticates twice against Cloudflare, because writing the
data and registering the tables are separate services:

| Variable | Purpose |
| --- | --- |
| `R2_ACCOUNT_ID` | Cloudflare account ID, used to derive the endpoints |
| `R2_BUCKET` | R2 bucket that holds the Iceberg warehouse |
| `R2_ACCESS_KEY_ID` / `R2_SECRET_ACCESS_KEY` | R2 **S3 API token** — writes the Parquet data and Iceberg metadata files |
| `R2_CATALOG_TOKEN` | Cloudflare **API token** with R2 and catalog permissions — authenticates to R2 Data Catalog |
| `R2_CATALOG_URI` | Catalog REST endpoint. Defaults to `https://catalog.cloudflarestorage.com/<account_id>/<bucket>` |
| `R2_CATALOG_WAREHOUSE` | Warehouse name. Defaults to `<account_id>_<bucket>` |
| `R2_S3_ENDPOINT` | Override for the S3 endpoint, normally derived as `https://<account_id>.r2.cloudflarestorage.com` |
| `R2_REGION` | S3 region, `auto` for R2 |

### 1. Create the bucket and enable its catalog

```bash
npx wrangler login
npx wrangler r2 bucket create lamda
npx wrangler r2 bucket catalog enable lamda
```

The `catalog enable` output prints the **Catalog URI** and **Warehouse** — set
them as `R2_CATALOG_URI` and `R2_CATALOG_WAREHOUSE` if they differ from the
defaults above. The same values appear on the catalog's page in the dashboard.

### 2. Create the S3 API token

In the dashboard, under R2 → **Manage API tokens**, create a token with **Object
Read & Write** on the bucket. The access key ID and secret it returns become
`R2_ACCESS_KEY_ID` and `R2_SECRET_ACCESS_KEY`. dlt passes these as AWS-style
credentials with R2's endpoint, since R2 implements the S3 API.

### 3. Create the catalog API token

Iceberg clients must authenticate to the catalog with a token carrying **both R2
and catalog permissions** — **Admin Read & Write** for a pipeline that creates
tables, or **Admin Read only** for query-only clients. That token is
`R2_CATALOG_TOKEN`.

The pipeline also forwards the S3 keys to PyIceberg as `s3.access-key-id`,
`s3.secret-access-key`, `s3.endpoint`, and `s3.region`, so reading and writing
data files never depends on the catalog vending credentials.

### How it reaches dlt

`scripts/load_lamda_r2_iceberg.py` builds a `filesystem` destination pointed at
`s3://$R2_BUCKET` with R2's endpoint, and publishes the catalog settings into
dlt's `iceberg_catalog` config section:

```python
dlt.config["iceberg_catalog.iceberg_catalog_type"] = "rest"
dlt.config["iceberg_catalog.iceberg_catalog_config"] = {...}
```

If you would rather not use environment variables, the same settings can live in
`.dlt/secrets.toml`, which dlt reads natively:

```toml
[iceberg_catalog]
iceberg_catalog_name = "r2_data_catalog"
iceberg_catalog_type = "rest"

[iceberg_catalog.iceberg_catalog_config]
type = "rest"
uri = "https://catalog.cloudflarestorage.com/<account_id>/<bucket>"
warehouse = "<account_id>_<bucket>"
token = "<catalog token>"
```

Any missing variable raises `R2ConfigurationError` naming the variable, before
the pipeline touches the network.

## Hugging Face

Public datasets, including `IQSeC-Lab/LAMDA`, need no credentials. For gated or
private datasets, either set

```bash
HF_TOKEN=hf_...
```

or log in once and let `huggingface_hub` use its cached token:

```bash
uv run hf auth login
```

The loader passes `HF_TOKEN` explicitly when set and otherwise falls back to the
cached token.

## Verifying your setup

Check the dbt side:

```bash
uv run dbt debug --profiles-dir .
```

Check the ingestion side without loading anything:

```bash
uv run python -c "
from scripts.load_lamda import build_pipeline
with build_pipeline().sql_client() as c:
    print(c.execute_sql('select current_account(), current_user(), current_role(), current_warehouse()'))
"
```

Then run a minimal load:

```bash
uv run python scripts/load_lamda.py --limit-per-file 10 --max-files 2
```

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `SnowflakeCredentials requires username and password ... when using snowflake authentication` | No key, token, or authenticator was found. Set one of the auth methods above. |
| `Make sure that private_key in dlt recognized format is at ...` | The key file is binary DER. Convert it to PKCS#8 PEM. |
| `Could not decode private key for key pair authentication` | Wrong key format or a missing/incorrect `SNOWFLAKE_PRIVATE_KEY_PASSPHRASE`. |
| `Snowflake-provided OAuth token not available` | `SNOWFLAKE_AUTHENTICATOR=oauth` with no `SNOWFLAKE_TOKEN`, outside Snowpark Container Services. |
| `Unknown authenticator` | Typo. Valid values: `snowflake`, `externalbrowser`, `oauth`, `programmatic_access_token`, `username_password_mfa`, `oauth_authorization_code`, `oauth_client_credentials`, or an `https://` Okta URL. |
| Ingestion works, dbt reports a missing token | `SNOWFLAKE_AUTHENTICATOR` is uppercase. dbt matches it case-sensitively; use lowercase. |
| `Incorrect username or password` on a password login | The account enforces MFA for that user. Switch to key pair or a PAT. |
| `Schema does not exist or not authorized` in dbt | The dbt role lacks access to schemas the loader created. Apply the `FUTURE SCHEMAS`/`FUTURE TABLES` grants above. |
