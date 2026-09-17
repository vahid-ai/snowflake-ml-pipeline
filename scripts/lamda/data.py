"""Snapshot-pinned source validation and reusable local sparse data preparation."""
from __future__ import annotations
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import yaml
from pyiceberg.expressions import AlwaysTrue, And, EqualTo
from pyiceberg.io.pyarrow import schema_to_pyarrow
from scipy import sparse

ROOT = Path(__file__).resolve().parents[2]
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


def load_contract(feature_set="lamda.malware_baseline@1") -> dict:
    """Resolve model input order from canonical definitions, not table order."""
    from scripts.lamda.contracts import read_definitions
    sets = [fs for path in sorted((ROOT / "feature-platform/feature_sets").glob("*.yaml"))
            for fs in yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.CSafeLoader)["feature_sets"]]
    fs = next((fs for fs in sets if f"{fs['id']}@{fs['version']}" == feature_set), None)
    if fs is None:
        raise ValueError(f"Unknown feature set: {feature_set}")
    by_ref = read_definitions(ROOT)
    selected, columns = {}, []
    def resolve(ref):
        if ref in selected:
            return
        feature = by_ref[ref]
        selected[ref] = feature
        for dep in feature.get("inputs", []):
            resolve(dep)
        if "column" in feature and feature["column"] not in columns:
            columns.append(feature["column"])
    for ref in fs["features"]:
        resolve(ref)
    contract = fs["input_contract"]
    if len(columns) != len(set(columns)) or len(columns) != contract["shape"][0]:
        raise ValueError("Canonical feature count/order is inconsistent")
    if fs["output_layout"]["numerical"]["features"] != fs["features"]:
        raise ValueError("Canonical model layout differs from feature order")
    if digest(ROOT / "data/lamda_feature_descriptions.json") != contract["dictionary_sha256"]:
        raise ValueError("Feature dictionary changed; register a new feature-set version")
    return {**contract, "id": f"{fs['id']}@{fs['version']}",
            "features": fs["features"], "columns": columns, "dtype": "float32", "definitions": selected}


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
        snapshot_schema = table.schemas()[snapshot.schema_id if snapshot.schema_id is not None else table.schema().schema_id]
        predicate = (And(EqualTo("dataset_id", contract["dataset_id"]), EqualTo("config_name", contract["config_name"]))
                     if {"dataset_id", "config_name"} <= set(snapshot_schema.column_names) else AlwaysTrue())
        self.scan = table.scan(
            snapshot_id=self.snapshot_id, selected_fields=self.fields,
            row_filter=predicate,
        )
        self.audit_scan = table.scan(snapshot_id=self.snapshot_id, row_filter=predicate,
                                     selected_fields=tuple(snapshot_schema.column_names))
        schema = self.audit_scan.projection()
        bindings = []
        for name in schema.column_names:
            field = schema.find_field(name)
            bindings.append({"column": name, "field_id": field.field_id,
                             "physical_type": str(field.field_type), "required": field.required})
        self.manifest = {
            "table": ".".join((table.catalog.name, *table.name())),
            "table_uuid": str(table.metadata.table_uuid),
            "snapshot_id": self.snapshot_id, "schema_id": schema.schema_id,
            "fields": [field for field in bindings if field["column"] in self.fields],
            "audit_fields": bindings, "dataset_id": contract["dataset_id"],
            "config_name": contract["config_name"],
        }

    def batches(self, batch_size: int):
        yield from self._batches(self.scan, batch_size)

    def audit_batches(self, batch_size: int):
        yield from self._batches(self.audit_scan, batch_size)

    def _batches(self, scan, batch_size: int):
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        from scripts.lamda.iceberg_reader import batches
        projection = scan.projection()
        schema = schema_to_pyarrow(projection)
        for batch in batches(self.table, scan, batch_size):
            yield pa.Table.from_batches([batch]).cast(schema)


def stage(source: IcebergInput, directory: Path, policy: SplitPolicy, batch_size: int,
          *, model="sgd", observations_root=None, publish_catalog=None) -> dict:
    """Prepare shards during a complete audit; never release a failing cache to fit."""
    from scripts.lamda.audit import AuditError, require_certified, run_audit
    audit_directory = directory.parent / (directory.name + "_audit")
    report = run_audit(source, audit_directory, policy=policy, batch_size=batch_size,
                       stage_directory=directory, model=model, observations_root=observations_root,
                       publish_catalog=publish_catalog)
    if not report["certified"]:
        raise AuditError(report, audit_directory)
    require_certified(report, source, policy, model)
    return json.loads((directory / "manifest.json").read_text(encoding="utf-8"))


def cached(directory: Path, stems: list[str]):
    for name in stems:
        stem = directory / name
        yield sparse.load_npz(stem.with_suffix(".npz")), pq.read_table(stem.with_suffix(".parquet"))
