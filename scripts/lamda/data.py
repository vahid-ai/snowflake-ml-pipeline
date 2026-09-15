"""Snapshot-pinned source validation and reusable local sparse data preparation."""
from __future__ import annotations
import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import yaml
from pyiceberg.expressions import And, EqualTo
from pyiceberg.io.pyarrow import ArrowScan, schema_to_pyarrow
from pyiceberg.types import IntegerType, LongType, StringType
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
