# Load Loghub Android logs into local Iceberg

`scripts/load_loghub_android_local_iceberg.py` streams `Android_2k.log` from the
[Loghub Android dataset](https://github.com/logpai/loghub/tree/master/Android) through
dlt and replaces `raw_loghub.android_logs` in a local PyIceberg SQL catalog. Each row
keeps the original log text and source URL alongside parsed Date, Time, PID, TID,
Level, Component, and Content fields; a parse failure never discards its raw line.

Install the locked dependencies and run from the repository root:

```bash
uv sync --locked
uv run python scripts/load_loghub_android_local_iceberg.py
```

The default output is `data/android_iceberg/`, with `catalog.sqlite`, Iceberg data
under `warehouse/`, isolated dlt state under `.dlt/`, and JSON run reports under
`runs/`. Rerunning fully replaces the table rather than appending duplicate logs.
Use a small separate destination for a smoke test:

```bash
uv run python scripts/load_loghub_android_local_iceberg.py \
  --local-root data/android_iceberg_smoke --limit 100
```

`--source-url` can select an HTTPS mirror or a commit-pinned raw GitHub URL, and
`--dataset-name` changes the local namespace. The run report records the exact URL,
row count, Iceberg snapshot ID, and metadata location. For reproducible downstream
work, prefer a commit-pinned URL and retain the recorded Iceberg snapshot.
