"""Streaming preflight audit, EDA and training cache. No model fitting occurs here."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import html
import json
import math
from pathlib import Path
import re
import sqlite3
import traceback
from uuid import uuid4

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
from scipy import sparse

from scripts.lamda.contracts import ContractError, Plan, fingerprint
from scripts.lamda.data import METADATA, SPLITS, SplitPolicy, write_json


def serial(value):
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, (dict, list)):
        return json.loads(json.dumps(value, default=str))
    return value if value is None or isinstance(value, (str, int, float, bool)) else str(value)


class Issues:
    def __init__(self, examples=3):
        self.items = {}
        self.examples = examples
        self.error_count = 0

    def add(self, code, stage, feature, node, message, *, count=1, examples=(), severity="error"):
        if count <= 0:
            return
        key = (code, stage, feature)
        item = self.items.setdefault(key, {"code": code, "severity": severity, "stage": stage,
                                          "feature": feature, "column": node.get("column"),
                                          "location": node.get("location", "input_contract"),
                                          "message": message, "count": 0, "examples": []})
        item["count"] += int(count)
        item["examples"].extend(list(examples)[:max(0, self.examples - len(item["examples"]))])
        if severity == "error":
            self.error_count += int(count)

    def mask(self, code, stage, feature, node, message, mask, batch, offset, values=None):
        mask = pc.fill_null(mask, False)
        count = pc.sum(pc.cast(mask, pa.int64())).as_py() or 0
        if not count:
            return
        key = (code, stage, feature)
        needed = self.examples - len(self.items.get(key, {}).get("examples", []))
        examples = []
        if needed > 0:
            indices = np.flatnonzero(mask.to_numpy(zero_copy_only=False))[:needed]
            for i in indices:
                example = {"scan_row": offset + int(i)}
                for name in ("source_file", "row_number"):
                    if name in batch.schema.names:
                        example[name] = serial(batch[name][int(i)].as_py())
                if values is not None:
                    example["value"] = serial(values[int(i)].as_py())
                examples.append(example)
        self.add(code, stage, feature, node, message, count=count, examples=examples)

    def records(self):
        return sorted(self.items.values(), key=lambda x: (x["severity"] != "error", x["stage"], x["feature"], x["code"]))


class AuditError(ValueError):
    def __init__(self, report, directory):
        self.report, self.directory = report, directory
        errors = [i for i in report["issues"] if i["severity"] == "error"]
        detail = "; ".join(f"{i['code']} {i['feature']}: {i['message']}" for i in errors[:8])
        super().__init__(f"Preflight failed ({len(errors)} diagnostics). {detail}. Report: {directory / 'report.json'}")


class Profile:
    """Exact distinct counts; low cardinalities stay in RAM, others spill to SQLite."""
    def __init__(self, name, dtype, database, limit=512):
        self.name, self.dtype, self.db, self.limit = name, str(dtype), database, limit
        self.rows = self.nulls = self.nonfinite = self.n = 0
        self.mean = self.m2 = 0.0
        self.minimum = self.maximum = None
        self.counter, self.spilled = Counter(), False

    def add(self, array):
        self.rows += len(array)
        self.nulls += array.null_count
        numeric = pa.types.is_integer(array.type) or pa.types.is_floating(array.type)
        if numeric:
            arr = pc.drop_null(array).to_numpy(zero_copy_only=False)
            finite = np.isfinite(arr)
            self.nonfinite += int((~finite).sum())
            arr = arr[finite]
            if len(arr):
                lo, hi = arr.min().item(), arr.max().item()
                self.minimum = lo if self.minimum is None else min(lo, self.minimum)
                self.maximum = hi if self.maximum is None else max(hi, self.maximum)
                mean, n = float(np.mean(arr, dtype=np.float64)), len(arr)
                m2 = float(np.sum((arr.astype(np.float64) - mean) ** 2))
                delta = mean - self.mean
                self.m2 += m2 + delta * delta * self.n * n / (self.n + n)
                self.mean += delta * n / (self.n + n)
                self.n += n
        try:
            counts = pc.value_counts(pc.drop_null(array)).to_pylist()
        except pa.ArrowNotImplementedError:
            counts = [{"values": k, "counts": v} for k, v in
                      Counter(json.dumps(serial(v), sort_keys=True) for v in array.to_pylist() if v is not None).items()]
        rows = [(self.name, json.dumps(serial(x["values"]), sort_keys=True), int(x["counts"])) for x in counts]
        if not self.spilled:
            self.counter.update({value: count for _, value, count in rows})
            if len(self.counter) <= self.limit:
                return
            rows = [(self.name, value, count) for value, count in self.counter.items()]
            self.counter.clear()
            self.spilled = True
        self.db.executemany("INSERT INTO distinct_values VALUES (?, ?, ?) ON CONFLICT(col, value) DO UPDATE SET n=n+excluded.n", rows)

    def result(self):
        if self.spilled:
            distinct = self.db.execute("SELECT COUNT(*) FROM distinct_values WHERE col=?", (self.name,)).fetchone()[0]
            top = self.db.execute("SELECT value,n FROM distinct_values WHERE col=? ORDER BY n DESC,value LIMIT 12", (self.name,)).fetchall()
        else:
            distinct, top = len(self.counter), sorted(self.counter.items(), key=lambda kv: (-kv[1], kv[0]))[:12]
        # Never upload APK hashes or source IDs as EDA top values.
        if self.name in {"hash", "source_file", "row_number", "_dlt_id", "_dlt_load_id"}:
            top = []
        domain = "unobserved" if self.rows == self.nulls else "non_numeric"
        if self.minimum is not None:
            integer = self.dtype.startswith(("int", "uint"))
            domain = ("binary" if integer and self.minimum >= 0 and self.maximum <= 1 else
                      "nonnegative_integer" if integer and self.minimum >= 0 else "integer" if integer else "real")
        return {"column": self.name, "observed_type": self.dtype, "observed_domain": domain,
                "rows": self.rows, "null_count": self.nulls,
                "observed_nullable": self.nulls > 0, "distinct_non_null": distinct, "distinct_method": "exact",
                "nonfinite_count": self.nonfinite, "min": self.minimum, "max": self.maximum,
                "mean": serial(self.mean) if self.n else None,
                "stddev": serial(math.sqrt(max(0, self.m2 / self.n))) if self.n else None,
                "top_values": [{"value": json.loads(v), "count": n} for v, n in top]}


def render(report, directory):
    write_json(directory / "report.json", report)
    rows = "".join("<tr>" + "".join(f"<td>{html.escape(str(i[k]))}</td>" for k in
                                  ("severity", "code", "location", "feature", "count", "message")) + "</tr>"
                   for i in report["issues"])
    eda = "".join("<tr>" + "".join(f"<td>{html.escape(str(p.get(k)))}</td>" for k in
                                 ("column", "physical_type", "observed_type", "rows", "null_count",
                                  "distinct_non_null", "min", "max", "mean", "stddev")) + "</tr>"
                  for p in report["profiles"])
    document = f"""<!doctype html><html lang="en"><meta charset="utf-8"><title>LAMDA preflight</title>
<style>body{{font:14px system-ui;margin:32px}}table{{border-collapse:collapse;margin-bottom:32px}}td,th{{border:1px solid #ccc;padding:6px;text-align:left}}th{{background:#eee;position:sticky;top:0}}</style>
<h1>LAMDA preflight: {html.escape(report['status'])}</h1>
<p>Rows: {report['rows']:,}. Complete scan: {report['complete_scan']}. Snapshot: {report['source']['snapshot_id']}.</p>
<p>Exact non-null distinct counts. Raw observations describe data; model requirements remain fixed. Diagnostic examples and full lineage are in report.json.</p>
<h2>Diagnostics</h2><table><tr><th>Severity</th><th>Code</th><th>Location</th><th>Feature</th><th>Count</th><th>Message</th></tr>{rows}</table>
<h2>Raw EDA</h2><table><tr><th>Column</th><th>Iceberg type</th><th>Arrow type</th><th>Rows</th><th>Nulls</th><th>Distinct</th><th>Min</th><th>Max</th><th>Mean</th><th>Stddev</th></tr>{eda}</table></html>"""
    (directory / "report.html").write_text(document, encoding="utf-8")
    lines = [f"{i['location']}: {i['severity']} {i['code']} [{i['feature']}] ({i['count']}): {i['message']}" for i in report["issues"]]
    (directory / "diagnostics.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    for line in lines[:80]:
        print(line, flush=True)
    print(f"Audit {report['status']}: {report['rows']:,} rows, {len(lines)} diagnostics. {directory / 'report.html'}", flush=True)


def run_audit(source, directory: Path, *, policy=SplitPolicy(), batch_size=4096,
              stage_directory=None, model="sgd", max_rows=None, examples=3,
              observations_root=None, publish_catalog=None):
    """Scan the pinned selection completely. Only a complete, error-free scan certifies it.

    Sample reports never certify training or update authoritative observations.
    Cache shards can be prepared during the scan, but callers must check `certified`.
    """
    if batch_size < 1 or (max_rows is not None and max_rows < 1) or examples < 0:
        raise ValueError("Invalid audit limits")
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=False)
    issues, profiles, rows = Issues(examples), {}, 0
    report = {"audit_version": 1, "audit_id": uuid4().hex, "created_at": datetime.now(timezone.utc).isoformat(),
              "source": source.manifest, "split": policy.manifest(), "model": model,
              "contract_sha256": fingerprint(source.contract), "status": "running", "rows": 0,
              "complete_scan": False, "certified": False, "profiles": [], "issues": [], "lineage": []}
    database = sqlite3.connect(directory / "working.sqlite")
    database.execute("CREATE TABLE distinct_values (col TEXT,value TEXT,n INTEGER,PRIMARY KEY(col,value)) WITHOUT ROWID")
    database.execute("CREATE TABLE apk (hash TEXT PRIMARY KEY,split TEXT) WITHOUT ROWID")
    counts, files = {s: [0, 0] for s in SPLITS}, {s: [] for s in SPLITS}
    if stage_directory is not None:
        stage_directory = Path(stage_directory)
        stage_directory.mkdir()
        for split in SPLITS:
            (stage_directory / split).mkdir()
    plan = None
    scan_finished = False
    try:
        try:
            plan = Plan(source.contract)
            report["resolved_contract_sha256"] = plan.digest
            report["lineage"] = plan.lineage()
            binding = {field["column"]: field for field in source.manifest["fields"]}
            for output in report["lineage"]:
                for node in output["nodes"]:
                    if "column" in node:
                        node["iceberg_binding"] = binding.get(node["column"])
        except (ContractError, KeyError, TypeError, ValueError) as exc:
            issues.add("CONTRACT_INVALID", "contract", source.contract.get("id", "unknown"), {}, str(exc))
        schema = source.audit_scan.projection()
        names = set(schema.column_names)
        for name in set(METADATA) | set(source.contract["columns"]):
            if name not in names:
                issues.add("MISSING_COLUMN", "schema", name, {}, f"Required source column {name} is absent")
        for name in METADATA:
            if name in names:
                observed = str(schema.find_field(name).field_type)
                allowed = ("int", "long") if name == "label" else ("string",)
                if observed not in allowed:
                    issues.add("METADATA_TYPE", "schema", name, {}, f"Expected {allowed}, observed {observed}")
        # A missing predicate column cannot define the requested dataset selection.
        can_scan = {"dataset_id", "config_name"} <= names
        if can_scan:
            for index, batch in enumerate(source.audit_batches(batch_size)):
                if max_rows is not None and rows >= max_rows:
                    break
                if max_rows is not None:
                    batch = batch.slice(0, max_rows - rows)
                if not len(batch):
                    continue
                for field in batch.schema:
                    profile = profiles.setdefault(field.name, Profile(field.name, field.type, database))
                    profile.add(batch[field.name])
                    physical = schema.find_field(field.name)
                    if physical.required and batch[field.name].null_count:
                        issues.mask("ICEBERG_REQUIRED_NULL", "schema", field.name, {}, "Null contradicts required Iceberg field",
                                    pc.is_null(batch[field.name]), batch, rows)
                start_errors = issues.error_count
                assigned, identities = [], []
                if set(METADATA) <= set(batch.schema.names):
                    meta = {name: batch[name].to_pylist() for name in METADATA}
                    for row in range(len(batch)):
                        apk, label = meta["hash"][row], meta["label"][row]
                        split = None
                        example = [{"scan_row": rows + row}]
                        if not isinstance(apk, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", apk):
                            issues.add("APK_IDENTITY", "split", "hash", {}, "Every row requires a SHA256 APK hash", examples=example)
                        else:
                            apk = apk.lower()
                            try:
                                split = policy.assign(apk, meta["split_name"][row], meta["year_month"][row])
                            except ValueError as exc:
                                issues.add("SPLIT_INVALID", "split", "split_name/year_month", {}, str(exc), examples=example)
                            try:
                                database.execute("INSERT INTO apk VALUES (?, ?)", (apk, split))
                            except sqlite3.IntegrityError:
                                prior = database.execute("SELECT split FROM apk WHERE hash=?", (apk,)).fetchone()[0]
                                issues.add("SPLIT_OVERLAP" if split != prior else "DUPLICATE_APK", "split", "hash", {},
                                           "Repeated APK hash: resolve duplicate/overlapping samples before training", examples=example)
                        if type(label) is not int or label not in (0, 1):
                            issues.add("LABEL_DOMAIN", "split", "label", {}, "label must be a non-null binary integer", examples=example)
                        elif split is not None:
                            counts[split][label] += 1
                        for name in ("config_name", "dataset_id"):
                            if meta[name][row] != source.contract[name]:
                                issues.add("DATASET_MISMATCH", "raw_contract", name, {}, "Mixed dataset/configuration", examples=example)
                        assigned.append(split)
                        identities.append(apk)
                else:
                    meta = None
                matrix = plan.execute(batch, issues, offset=rows, model=model) if plan is not None else None
                if (stage_directory is not None and matrix is not None and meta is not None
                        and issues.error_count == start_errors):
                    assigned = np.asarray(assigned)
                    labels = np.asarray(meta["label"], dtype=np.int64)
                    for split in SPLITS:
                        mask = assigned == split
                        if not mask.any():
                            continue
                        stem = stage_directory / split / f"{index:08d}"
                        sparse.save_npz(stem.with_suffix(".npz"), matrix[mask])
                        pq.write_table(pa.table({"label": labels[mask], "hash": np.asarray(identities)[mask],
                                                "year_month": np.asarray(meta["year_month"])[mask]}), stem.with_suffix(".parquet"))
                        files[split].append(str(stem.relative_to(stage_directory)))
                rows += len(batch)
                database.commit()
                if index % 10 == 0:
                    print(f"Audited {rows:,} rows; {len(issues.items)} diagnostics", flush=True)
            else:
                scan_finished = True
        if not rows:
            issues.add("EMPTY_DATASET", "raw_contract", "dataset", {}, "No selected rows were read")
        for split, classes in counts.items():
            if min(classes) == 0:
                issues.add("SPLIT_CLASSES", "split", split, {}, f"{split} must contain both classes; found {classes}")
        if max_rows is not None:
            issues.add("SAMPLED_AUDIT", "coverage", "dataset", {}, "Sample audits cannot certify training or update observations", severity="warning")
    except Exception as exc:
        # Connector exceptions can include signed URLs; never persist their text.
        issues.add("SCAN_FAILED", "source", "dataset", {}, f"Scan interrupted ({type(exc).__name__}); incomplete reports cannot certify training")
        report["failure_trace"] = [{"file": Path(frame.filename).name, "line": frame.lineno, "function": frame.name}
                                   for frame in traceback.extract_tb(exc.__traceback__)]
    finally:
        try:
            report["profiles"] = [profile.result() for profile in profiles.values()]
            fields = {f["column"]: f for f in source.manifest.get("audit_fields", source.manifest["fields"])}
            for profile in report["profiles"]:
                profile.update({k: v for k, v in fields.get(profile["column"], {}).items() if k != "column"})
                # A descriptive warning, never a rule inferred for model inputs.
                sd = profile["stddev"]
                if (isinstance(sd, (int, float)) and sd > 0 and profile["max"] is not None
                        and profile["observed_domain"] != "binary"
                        and profile["max"] > profile["mean"] + 8 * sd):
                    issues.add("STATISTICAL_OUTLIER", "eda", profile["column"], {},
                               "Observed maximum is over 8 population standard deviations above the mean; inspect the distribution. No contract was changed.",
                               severity="warning")
        finally:
            database.close()
        # Working exact-distinct/identity data stays local only and is no longer needed.
        (directory / "working.sqlite").unlink(missing_ok=True)
    report.update(rows=rows, complete_scan=scan_finished and max_rows is None,
                  class_counts=counts, issues=issues.records())
    report["certified"] = report["complete_scan"] and issues.error_count == 0
    report["status"] = "passed" if report["certified"] else "failed" if issues.error_count else "advisory"
    if report["complete_scan"] and observations_root is not None:
        from scripts.lamda.observations import publish_local
        try:
            report["observations"] = publish_local(report, Path(observations_root))
        except Exception as exc:
            issues.add("OBSERVATION_WRITE", "publication", "yaml", {}, f"Observation publication failed ({type(exc).__name__})")
    if report["complete_scan"] and publish_catalog is not None:
        from scripts.lamda.observations import publish_iceberg
        try:
            report["iceberg_observations"] = publish_iceberg(report, publish_catalog)
        except Exception as exc:
            issues.add("OBSERVATION_WRITE", "publication", "iceberg", {}, f"Observation publication failed ({type(exc).__name__})")
    report["issues"] = issues.records()
    if issues.error_count:
        report.update(certified=False, status="failed")
    render(report, directory)
    if stage_directory is not None:
        manifest = {"counts": counts, "shards": files, "split": policy.manifest(), "source": source.manifest,
                    "audit_id": report["audit_id"], "contract_sha256": report["contract_sha256"], "certified": report["certified"]}
        write_json(stage_directory / "manifest.json", manifest)
    return report


def require_certified(report, source, policy, model):
    if (not report.get("certified") or not report.get("complete_scan")
            or report.get("contract_sha256") != fingerprint(source.contract)
            or report.get("source") != source.manifest or report.get("split") != policy.manifest()
            or report.get("model") != model):
        raise ValueError("Audit certificate is incomplete, failed, or bound to a different snapshot/contract/split/model")
