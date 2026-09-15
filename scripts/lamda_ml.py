"""Snapshot-pinned LAMDA training. Remote tables are strictly read-only.

Feature matrices are staged as sparse local shards, never a dense full dataset.
Only labels and predictions for one evaluation split are collected in memory.
"""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import re
import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import joblib
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import yaml
from pyiceberg.expressions import And, EqualTo
from pyiceberg.io.pyarrow import ArrowScan, schema_to_pyarrow
from pyiceberg.types import IntegerType, LongType, StringType
from scipy import sparse
from sklearn.linear_model import SGDClassifier
from sklearn.metrics import (
    average_precision_score, balanced_accuracy_score, confusion_matrix, f1_score,
    log_loss, precision_recall_curve, precision_score, recall_score, roc_auc_score,
)

ROOT = Path(__file__).resolve().parent.parent
METADATA = ("hash", "label", "year_month", "split_name", "config_name", "dataset_id")
SPLITS = ("train", "validation", "test")


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def load_contract() -> dict:
    """Resolve model input order from canonical definitions, not table order."""
    fs = yaml.safe_load((ROOT / "feature-platform/feature_sets/lamda.yaml").read_text(encoding="utf-8"))["feature_sets"][0]
    features = yaml.safe_load((ROOT / "feature-platform/features/lamda.yaml").read_text(encoding="utf-8"))["features"]
    by_ref = {f"{f['id']}@{f['version']}": f for f in features}
    selected = [by_ref[ref] for ref in fs["features"]]
    columns = [f["column"] for f in selected]
    contract = fs["input_contract"]
    if len(columns) != len(set(columns)) or len(columns) != contract["shape"][0]:
        raise ValueError("Canonical feature count/order is inconsistent")
    if fs["output_layout"]["numerical"]["features"] != fs["features"]:
        raise ValueError("Canonical model layout differs from feature order")
    for feature in selected:
        if (feature["source"] != contract["source"] or feature["semantic_type"] != "binary"
                or feature["output"] != {"type": "int64", "nullable": True}
                or feature["model_representation"]["dtype"] != "float32"
                or feature["missing"]["strategy"] != "error"
                or feature["validation"] != {"min": 0, "max": 1}):
            raise ValueError("Feature contract is unsupported by the binary adapter")
    if digest(ROOT / "data/lamda_feature_descriptions.json") != contract["dictionary_sha256"]:
        raise ValueError("Feature dictionary changed; register a new feature-set version")
    return {**contract, "id": f"{fs['id']}@{fs['version']}",
            "features": fs["features"], "columns": columns, "dtype": "float32"}


def binary_matrix(batch: pa.Table | pa.RecordBatch, columns: list[str]) -> sparse.csr_matrix:
    """Validate before casting; do not turn missing features into absent tokens."""
    if len(batch.schema.names) != len(set(batch.schema.names)):
        raise ValueError("Duplicate input column names")
    missing = set(columns) - set(batch.schema.names)
    if missing:
        raise ValueError(f"Missing required features: {sorted(missing)[:10]}")
    # Column-wise construction avoids even a batch-sized dense N x 4561 copy.
    indices, offsets = [], [0]
    for name in columns:
        array = batch[name]
        if not pa.types.is_integer(array.type) or array.null_count:
            raise ValueError(f"{name}: expected non-null binary integers, got {array.type}")
        values = array.to_numpy(zero_copy_only=False)
        if not np.isin(values, [0, 1]).all():
            raise ValueError(f"{name}: values must be 0 or 1")
        indices.append(np.flatnonzero(values).astype(np.int32))
        offsets.append(offsets[-1] + len(indices[-1]))
    rows = np.concatenate(indices) if indices else np.array([], dtype=np.int32)
    return sparse.csc_matrix(
        (np.ones(len(rows), dtype=np.float32), rows, np.asarray(offsets)),
        shape=(len(batch), len(columns)),
    ).tocsr()


def month(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value):
        raise ValueError("year_month and temporal cutoffs must be YYYY-MM")
    return value


@dataclass(frozen=True)
class SplitPolicy:
    seed: int = 42
    train_through: str | None = None
    validation_through: str | None = None

    def __post_init__(self):
        if self.seed < 0 or self.seed >= 2**32:
            raise ValueError("seed must be in [0, 2**32)")
        if (self.train_through is None) != (self.validation_through is None):
            raise ValueError("Supply both temporal cutoffs")
        if self.train_through is not None:
            if month(self.train_through) >= month(self.validation_through):
                raise ValueError("train_through must precede validation_through")

    def assign(self, apk: str, published: str, collected: str) -> str:
        month(collected)
        if published not in ("train", "test"):
            raise ValueError(f"Unknown published split: {published!r}")
        if self.train_through:
            return ("train" if collected <= self.train_through else
                    "validation" if collected <= self.validation_through else "test")
        if published == "test":
            return "test"
        value = hashlib.sha256(f"{self.seed}:{apk}".encode("utf-8")).digest()
        return "validation" if int.from_bytes(value[:8], "big") % 10000 < 2000 else "train"

    def manifest(self) -> dict:
        return {"id": "lamda.collection_month@1" if self.train_through else
                "lamda.published_hash_validation@1", **self.__dict__}


class IcebergInput:
    def __init__(self, table, contract: dict, snapshot_id: int | None = None):
        self.table, self.contract = table, contract
        snapshot = table.snapshot_by_id(snapshot_id) if snapshot_id is not None else table.current_snapshot()
        if snapshot is None:
            raise ValueError("Source has no requested snapshot (empty table or expired snapshot)")
        self.snapshot_id = snapshot.snapshot_id
        self.fields = tuple(METADATA) + tuple(contract["columns"])
        self.scan = table.scan(
            snapshot_id=self.snapshot_id, selected_fields=self.fields,
            row_filter=And(EqualTo("dataset_id", contract["dataset_id"]),
                           EqualTo("config_name", contract["config_name"])),
        )
        schema = self.scan.projection()
        bindings = []
        for name in self.fields:
            field = schema.find_field(name)
            allowed = (IntegerType, LongType) if name == "label" or name in contract["columns"] else (StringType,)
            if not isinstance(field.field_type, allowed):
                raise ValueError(f"Unsupported Iceberg type for {name}: {field.field_type}")
            bindings.append({"column": name, "field_id": field.field_id,
                             "physical_type": str(field.field_type), "required": field.required})
        self.manifest = {
            "table": ".".join((table.catalog.name, *table.name())),
            "table_uuid": str(table.metadata.table_uuid),
            "snapshot_id": self.snapshot_id, "schema_id": schema.schema_id,
            "fields": bindings, "dataset_id": contract["dataset_id"],
            "config_name": contract["config_name"],
        }

    def batches(self, batch_size: int):
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        # Stable file order makes SGD replay independent of parallel scan scheduling.
        # One file at a time retains Iceberg deletes and schema projection. PyIceberg
        # may materialize one file internally; batch_size is not a hard memory limit.
        projection = self.scan.projection()
        schema = schema_to_pyarrow(projection)
        tasks = sorted(self.scan.plan_files(), key=lambda task: task.file.file_path)
        for task in tasks:
            reader = ArrowScan(self.scan.table_metadata, self.table.io, projection,
                               self.scan.row_filter, self.scan.case_sensitive)
            for batch in reader.to_record_batches([task]):
                for offset in range(0, len(batch), batch_size):
                    yield pa.Table.from_batches([batch.slice(offset, batch_size)]).cast(schema)


def stage(source: IcebergInput, directory: Path, policy: SplitPolicy, batch_size: int) -> dict:
    """Audit all APK identities before fitting; retain bounded sparse local shards."""
    directory.mkdir()
    counts = {split: [0, 0] for split in SPLITS}
    files = {split: [] for split in SPLITS}
    for split in SPLITS:
        (directory / split).mkdir()
    database = sqlite3.connect(directory / "identity.sqlite")
    try:
        database.execute("CREATE TABLE apk (hash TEXT PRIMARY KEY, split TEXT NOT NULL)")
        for index, batch in enumerate(source.batches(batch_size)):
            if not len(batch):
                continue
            meta = {name: batch[name].to_pylist() for name in METADATA}
            identities, assigned = [], []
            for row in range(len(batch)):
                apk = meta["hash"][row]
                if not isinstance(apk, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", apk):
                    raise ValueError("Every row requires a non-null SHA256 APK hash")
                apk = apk.lower()
                if meta["label"][row] not in (0, 1):
                    raise ValueError("label must be a non-null binary integer")
                if (meta["config_name"][row] != source.contract["config_name"] or
                        meta["dataset_id"][row] != source.contract["dataset_id"]):
                    raise ValueError("Mixed dataset/configuration in selected source")
                split = policy.assign(apk, meta["split_name"][row], meta["year_month"][row])
                identities.append((apk, split))
                assigned.append(split)
            try:
                database.executemany("INSERT INTO apk VALUES (?, ?)", identities)
                database.commit()
            except sqlite3.IntegrityError as exc:
                raise ValueError("Repeated APK hash: resolve duplicate/overlapping samples before training") from exc
            matrix = binary_matrix(batch, source.contract["columns"])
            labels = np.asarray(meta["label"], dtype=np.int64)
            assigned = np.asarray(assigned)
            for split in SPLITS:
                mask = assigned == split
                if not mask.any():
                    continue
                stem = directory / split / f"{index:08d}"
                sparse.save_npz(stem.with_suffix(".npz"), matrix[mask])
                metadata = pa.table({
                    "label": labels[mask],
                    "hash": np.asarray([x[0] for x in identities])[mask],
                    "year_month": np.asarray(meta["year_month"])[mask],
                })
                pq.write_table(metadata, stem.with_suffix(".parquet"))
                counts[split] = (np.asarray(counts[split]) + np.bincount(labels[mask], minlength=2)).tolist()
                files[split].append(str(stem.relative_to(directory)))
            if index % 10 == 0:
                print(f"Staged {sum(sum(c) for c in counts.values())} rows", flush=True)
    finally:
        database.close()
    for split, classes in counts.items():
        if min(classes) == 0:
            raise ValueError(f"{split} must contain both classes; found benign/malware counts {classes}")
    report = {"counts": counts, "shards": files, "split": policy.manifest(), "source": source.manifest}
    write_json(directory / "manifest.json", report)
    return report


def cached(directory: Path, stems: list[str]):
    for name in stems:
        stem = directory / name
        yield sparse.load_npz(stem.with_suffix(".npz")), pq.read_table(stem.with_suffix(".parquet"))


def score(model, directory: Path, stems: list[str]):
    metadata, predictions = [], []
    for matrix, meta in cached(directory, stems):
        predictions.append(model.predict_proba(matrix)[:, 1])
        metadata.append(meta)
    return pa.concat_tables(metadata), np.concatenate(predictions)


def choose_threshold(labels, probabilities) -> float:
    precision, recall, thresholds = precision_recall_curve(labels, probabilities)
    f1 = np.divide(2 * precision[:-1] * recall[:-1], precision[:-1] + recall[:-1],
                   out=np.zeros(len(thresholds)), where=(precision[:-1] + recall[:-1]) > 0)
    # On ties prefer the higher threshold to avoid additional false positives.
    return float(thresholds[np.flatnonzero(f1 == f1.max())[-1]])


def metrics(labels, probabilities, threshold: float) -> dict:
    labels, probabilities = np.asarray(labels), np.asarray(probabilities)
    predicted = probabilities >= threshold
    tn, fp, fn, tp = confusion_matrix(labels, predicted, labels=[0, 1]).ravel()
    both = len(np.unique(labels)) == 2
    return {
        "rows": len(labels), "threshold": threshold,
        "malware_prevalence": float(labels.mean()),
        "average_precision": float(average_precision_score(labels, probabilities)) if both else None,
        "roc_auc": float(roc_auc_score(labels, probabilities)) if both else None,
        "f1": float(f1_score(labels, predicted, zero_division=0)),
        "precision": float(precision_score(labels, predicted, zero_division=0)),
        "recall": float(recall_score(labels, predicted, zero_division=0)),
        "balanced_accuracy": float(balanced_accuracy_score(labels, predicted)) if both else None,
        "false_positive_rate": float(fp / (fp + tn)) if fp + tn else None,
        "log_loss": float(log_loss(labels, probabilities, labels=[0, 1])),
        "confusion_matrix": [[int(tn), int(fp)], [int(fn), int(tp)]],
    }


def environment_manifest() -> dict:
    """Pin working source bytes too, because a run may precede its commit."""
    def git(*args):
        result = subprocess.run(
            ["git", "-c", f"safe.directory={ROOT.as_posix()}", *args], cwd=ROOT,
            capture_output=True, text=True, check=False,
        )
        return result.stdout.strip() if result.returncode == 0 else None
    files = [*ROOT.glob("scripts/*.py"), *ROOT.glob("feature-platform/**/*.yaml"),
             ROOT / "pyproject.toml", ROOT / "uv.lock"]
    return {
        "git_commit": git("rev-parse", "HEAD"), "git_status": git("status", "--porcelain"),
        "source_sha256": {str(p.relative_to(ROOT)): digest(p) for p in sorted(files)},
        "packages": {p: importlib.metadata.version(p) for p in
                     ("scikit-learn", "scipy", "numpy", "pyarrow", "pyiceberg", "joblib", "pyyaml")},
    }


def train(source: IcebergInput, output: Path, *, policy: SplitPolicy = SplitPolicy(),
          batch_size: int = 4096, epochs: int = 3, alphas=(0.0001, 0.001)) -> dict:
    if batch_size < 1 or epochs < 1 or not alphas or any(not np.isfinite(a) or a <= 0 for a in alphas):
        raise ValueError("batch_size, epochs and alphas must be positive and finite")
    # Exclusive output creation avoids mixing old models with new/failed runs.
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / "status.json", {"status": "running"})
    try:
        environment = environment_manifest()
        write_json(output / "input_contract.json", source.contract)
        directory = output / "cache"
        data = stage(source, directory, policy, batch_size)
        count = np.asarray(data["counts"]["train"])
        weights = count.sum() / (2.0 * count)
        best_model, best_ap, best_validation = None, -1.0, None
        candidates = []
        for alpha in alphas:
            model = SGDClassifier(loss="log_loss", alpha=alpha, random_state=policy.seed,
                                  average=True, shuffle=True, learning_rate="optimal")
            rng = np.random.default_rng(policy.seed)
            for epoch in range(epochs):
                order = rng.permutation(data["shards"]["train"]).tolist()
                for matrix, meta in cached(directory, order):
                    labels = meta["label"].to_numpy()
                    model.partial_fit(matrix, labels, classes=np.array([0, 1]),
                                      sample_weight=weights[labels].astype(np.float32))
                print(f"alpha={alpha}: epoch {epoch + 1}/{epochs}", flush=True)
            validation, probabilities = score(model, directory, data["shards"]["validation"])
            ap = float(average_precision_score(validation["label"].to_numpy(), probabilities))
            candidates.append({"alpha": alpha, "validation_average_precision": ap})
            if ap > best_ap:
                best_model, best_ap, best_validation = model, ap, (validation, probabilities)
        validation, val_scores = best_validation
        threshold = choose_threshold(validation["label"].to_numpy(), val_scores)
        # This is the first model-scoring access to the test split.
        test, test_scores = score(best_model, directory, data["shards"]["test"])
        test_y = test["label"].to_numpy()
        years = np.array([m[:4] for m in test["year_month"].to_pylist()])
        report = {
            "selection": candidates, "selected_alpha": best_model.alpha,
            "threshold_policy": "Maximum validation F1, higher threshold wins ties",
            "validation": metrics(validation["label"].to_numpy(), val_scores, threshold),
            "test": metrics(test_y, test_scores, threshold),
            "test_at_0_5": metrics(test_y, test_scores, 0.5),
            "test_by_year": {year: metrics(test_y[years == year], test_scores[years == year], threshold)
                             for year in sorted(set(years))},
            "training_prior_baseline": metrics(test_y, np.full(len(test_y), count[1] / count.sum()), 0.5),
        }
        artifact = {"format_version": 1, "model": best_model, "threshold": threshold,
                    "contract": source.contract, "source": source.manifest,
                    "split": policy.manifest(), "environment": environment}
        model_path = output / "model.joblib"
        joblib.dump(artifact, model_path)
        scored = test.append_column("malware_probability", pa.array(test_scores))
        pq.write_table(scored.append_column("prediction", pa.array((test_scores >= threshold).astype(np.int8))),
                       output / "test_predictions.parquet")
        write_json(output / "metrics.json", report)
        manifest = {
            "status": "complete", "created_at": datetime.now(timezone.utc).isoformat(),
            "experiment": "lamda.malware_detection@1", "feature_set": source.contract["id"],
            "source": source.manifest, "split": policy.manifest(), "class_counts": data["counts"],
            "backend": "lamda_iceberg_scipy_v1", "environment": environment,
            "training": {"epochs": epochs, "alphas": list(alphas), "batch_size": batch_size,
                         "class_weights": weights.tolist(), "selected_alpha": best_model.alpha},
            "fitted_artifact": {"path": "model.joblib", "sha256": digest(model_path)},
            "input_contract_sha256": digest(output / "input_contract.json"),
        }
        write_json(output / "manifest.json", manifest)
        write_json(output / "status.json", {"status": "complete"})
        return report
    except Exception:
        # Do not persist exception text: remote errors can contain signed URLs.
        write_json(output / "status.json", {"status": "failed"})
        raise


def predict(artifact: dict, batch: pa.Table | pa.RecordBatch) -> pa.Table:
    if artifact.get("format_version") != 1:
        raise ValueError("Unsupported model artifact version")
    contract = artifact["contract"]
    # Require provenance even for unlabeled inference to prevent config mixups.
    for name in ("config_name", "dataset_id"):
        if name not in batch.schema.names or any(v != contract[name] for v in batch[name].to_pylist()):
            raise ValueError(f"Inference requires matching {name} on every row")
    matrix = binary_matrix(batch, contract["columns"])
    probabilities = artifact["model"].predict_proba(matrix)[:, 1]
    result = pa.table({"malware_probability": probabilities,
                       "prediction": (probabilities >= artifact["threshold"]).astype(np.int8)})
    if "hash" in batch.schema.names:
        result = result.append_column("hash", batch["hash"])
    return result
