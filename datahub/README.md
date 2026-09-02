# DataHub over the R2 Iceberg catalog

Runs a local [DataHub](https://datahubproject.io) instance and ingests the
metadata of the Iceberg tables that `scripts/load_lamda_r2_iceberg.py` writes
to Cloudflare R2 Data Catalog, so the LAMDA tables are browsable — schema (all
~4.5k columns), snapshots, and properties — in a data catalog UI.

## 1. Start DataHub

DataHub's quickstart runs as a Docker Compose stack (GMS, frontend, MySQL,
OpenSearch, Kafka, actions). Install the CLI in an isolated environment so its
pyiceberg pin cannot conflict with the project's dlt dependencies:

```bash
uv tool install "acryl-datahub[iceberg]"
datahub docker quickstart
```

First start pulls ~7 images and takes a few minutes. The UI then serves at
<http://localhost:9002> (default login `datahub` / `datahub`) and the GMS API
at <http://localhost:8080>.

## 2. Ingest the R2 Iceberg metadata

`iceberg_r2.dhub.yml` is the ingestion recipe: an `iceberg` source whose
catalog properties mirror `scripts/load_lamda_r2_iceberg.py` (REST catalog
URI, warehouse, token, and the R2 S3 endpoint/keys for reading manifests), and
a `datahub-rest` sink pointing at the local GMS. `ingest.sh` derives the same
defaults from `R2_ACCOUNT_ID`/`R2_BUCKET` that the pipeline uses, so with
secrets in Infisical the whole run is:

```bash
infisical run --projectId=<project-id> --env=dev -- datahub/ingest.sh
```

or with a filled-in `.env`: `set -a && . ./.env && set +a && datahub/ingest.sh`.

The run registers each Iceberg table as a DataHub dataset under platform
`iceberg`, instance `r2_lamda` — e.g. `raw_lamda.lamda_samples` with its full
column schema and table properties. Re-running refreshes them.

## Troubleshooting the quickstart in constrained containers

Two host-level issues surfaced running this in a sandboxed container, both
fatal to the `system-update` job (it dies at `BuildIndicesIncremental`):

- **`vm.max_map_count` too low** — OpenSearch wants 262144:
  `sysctl -w vm.max_map_count=262144`.
- **OpenSearch blocks index creation on disk pressure**
  (`index_create_block_exception … cluster create-index blocked`). Containers
  with a small disk allowance on a large filesystem trip the ~90% high
  watermark even with plenty of space free. Disable the threshold check:

  ```bash
  docker exec datahub-opensearch-1 curl -s -X PUT "localhost:9200/_cluster/settings" \
    -H 'Content-Type: application/json' \
    -d '{"persistent": {"cluster.routing.allocation.disk.threshold_enabled": false, "cluster.blocks.create_index": null}}'
  ```

  The compose stack keeps restarting the update job, so it recovers on the
  next attempt once the block is cleared.

## Troubleshooting the quickstart on Windows

Neither issue below affects the stack itself — `system-update` completes
cleanly on a normal Windows host, so the two container fixes above are not
needed there. Both are host-level problems that make a working setup look
broken.

- **The quickstart ends in a traceback even when it worked.** The last thing
  `datahub docker quickstart` does is print `✔ DataHub is now running`, and a
  `cp1252` console cannot encode `✔`:

  ```text
  UnicodeEncodeError: 'charmap' codec can't encode character '\u2714' in position 0: character maps to <undefined>
  ```

  The traceback fires *after* the stack is up and the command still exits 0,
  so it is safe to ignore; `PYTHONIOENCODING=utf-8` silences it. Confirm the
  real state with `docker ps` — six containers up, plus `system-update`
  exited 0.

- **Docker Desktop crash-looping on stale sockets.** If the backend dies at
  startup with `initializing Inference manager` or `initializing Secrets
  Engine` and `The file cannot be accessed by the system`, orphaned AF_UNIX
  socket files from an earlier crash are blocking it. Windows refuses to
  delete them (`del`, `Remove-Item`, and `File.Delete` all fail), but
  renaming their parent directories works. With Docker fully stopped, move
  both aside:

  ```powershell
  Get-Process "Docker Desktop","com.docker.backend" -ErrorAction SilentlyContinue |
    Stop-Process -Force
  Rename-Item "$env:LOCALAPPDATA\Docker\run" run.stale -ErrorAction SilentlyContinue
  Rename-Item "$env:LOCALAPPDATA\docker-secrets-engine" `
    docker-secrets-engine.stale -ErrorAction SilentlyContinue
  ```

  Docker recreates both on the next start. Clear them in the same pass —
  fixing one at a time just moves the crash to the other, because each
  failed start leaves behind a fresh socket the next one cannot remove.

## Operational notes

- The quickstart stack is stateful across restarts (Docker volumes). Stop it
  with `datahub docker quickstart --stop`; wipe it with `datahub docker nuke`.
- The Iceberg source reads only *metadata* (catalog listings and table
  manifests) — no table data rows are scanned unless profiling is enabled.
- In an ephemeral container, DataHub's state dies with the container; this
  setup is for exploration, not a durable catalog deployment.
