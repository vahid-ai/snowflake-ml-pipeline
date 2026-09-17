"""Observed raw metadata is generated separately from immutable required contracts."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pyarrow as pa
import yaml
from pyiceberg.exceptions import NoSuchTableError, TableAlreadyExistsError
from pyiceberg.expressions import EqualTo

from scripts.lamda.contracts import fingerprint


# Build descriptive metadata only from a completed nonempty scan; required feature contracts
# remain separate.
def observation(report):
    if not report["complete_scan"] or not report["rows"]:
        raise ValueError("Only completed, nonempty full scans can update observations")
    return {"spec_version": "1.0", "kind": "observed_raw_profile", "audit_id": report["audit_id"],
            "created_at": report["created_at"], "source": report["source"], "rows": report["rows"],
            "contract_sha256": report["contract_sha256"], "profiles": report["profiles"]}


# Persist immutable per-audit history and atomically replace the latest profile under an
# exclusive lock.
def publish_local(report, root: Path):
    value = observation(report)
    selection = {k: report["source"][k] for k in ("table_uuid", "dataset_id", "config_name")}
    directory = root / fingerprint(selection)[:24]
    directory.mkdir(parents=True, exist_ok=True)
    # Exclusive lock prevents concurrent writers replacing latest out of order.
    lock = directory / ".publish.lock"
    descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    try:
        latest = directory / "latest.yaml"
        changes = []
        if latest.exists():
            previous = yaml.load(latest.read_text(encoding="utf-8"), Loader=yaml.CSafeLoader)
            before = {p["column"]: p for p in previous["profiles"]}
            for profile in value["profiles"]:
                old = before.get(profile["column"], {})
                delta = {key: {"before": old.get(key), "after": profile.get(key)}
                         for key in ("observed_type", "observed_domain", "observed_nullable", "distinct_non_null", "min", "max")
                         if old.get(key) != profile.get(key)}
                if delta:
                    changes.append({"column": profile["column"], "changes": delta})
        history = directory / f"{report['source']['snapshot_id']}-{report['audit_id']}.yaml"
        payload = yaml.dump(value, Dumper=yaml.CSafeDumper, sort_keys=False, allow_unicode=True)
        if history.exists():
            if history.read_text(encoding="utf-8") != payload:
                raise ValueError("Immutable observation already exists with different contents")
        else:
            with history.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(payload)
        # latest means most recently audited, not greatest numeric snapshot ID.
        temporary = directory / f".{report['audit_id']}.tmp"
        temporary.write_text(payload, encoding="utf-8", newline="\n")
        os.replace(temporary, latest)
        return {"history": str(history), "latest": str(latest), "drift": changes}
    finally:
        os.close(descriptor)
        lock.unlink()


# Append field profiles and a completion record together, then verify the immutable audit by
# readback.
def publish_iceberg(report, catalog, identifier="raw_lamda.lamda_feature_observations"):
    value = observation(report)
    digest = fingerprint(value)
    schema = pa.schema([
        pa.field("audit_id", pa.string(), nullable=False),
        pa.field("record_kind", pa.string(), nullable=False),
        pa.field("source_table_uuid", pa.string(), nullable=False),
        pa.field("source_snapshot_id", pa.int64(), nullable=False),
        pa.field("dataset_id", pa.string(), nullable=False),
        pa.field("config_name", pa.string(), nullable=False),
        pa.field("field_id", pa.int32()), pa.field("column_name", pa.string()),
        pa.field("profile_json", pa.string(), nullable=False),
        pa.field("observation_sha256", pa.string(), nullable=False),
    ])
    try:
        table = catalog.load_table(identifier)
    except NoSuchTableError:
        try:
            table = catalog.create_table(identifier, schema=schema, properties={
                "comment": "Append-only completed raw LAMDA profiles. Join column_name feat_N to feature_id_baseline in lamda_feature_descriptions; contracts remain authoritative downstream.",
                "lamda.feature-descriptions-table": "raw_lamda.lamda_feature_descriptions"})
        except TableAlreadyExistsError:
            table = catalog.load_table(identifier)
    common = {"audit_id": value["audit_id"], "source_table_uuid": value["source"]["table_uuid"],
              "source_snapshot_id": value["source"]["snapshot_id"], "dataset_id": value["source"]["dataset_id"],
              "config_name": value["source"]["config_name"], "observation_sha256": digest}
    rows = [{**common, "record_kind": "field", "field_id": p.get("field_id"), "column_name": p["column"],
             "profile_json": json.dumps(p, allow_nan=False)} for p in value["profiles"]]
    rows.append({**common, "record_kind": "complete", "field_id": None, "column_name": None,
                 "profile_json": json.dumps({k: v for k, v in value.items() if k != "profiles"}, allow_nan=False)})
    existing = table.scan(row_filter=EqualTo("audit_id", value["audit_id"])).to_arrow()
    if len(existing):
        if len(existing) != len(rows) or set(existing["observation_sha256"].to_pylist()) != {digest}:
            raise ValueError("Conflicting immutable observation")
    else:
        # All fields and completion record commit in one Iceberg snapshot.
        table.append(pa.Table.from_pylist(rows, schema=schema))
    table.refresh()
    verified = table.scan(row_filter=EqualTo("audit_id", value["audit_id"])).to_arrow()
    if len(verified) != len(rows) or set(verified["observation_sha256"].to_pylist()) != {digest}:
        raise ValueError("Observation readback failed")
    return {"table": identifier, "snapshot_id": table.current_snapshot().snapshot_id,
            "audit_id": value["audit_id"], "records": len(verified), "sha256": digest}
