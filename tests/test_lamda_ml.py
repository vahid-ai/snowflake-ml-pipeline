"""Offline ML integration: actual Iceberg snapshots, sparse fitting and inference.

Synthetic fixtures validate software behavior; their scores are not LAMDA results.
"""
import contextlib
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

import joblib
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from pyiceberg.catalog import load_catalog
from sklearn.metrics import f1_score
from mlflow import MlflowClient
from scripts.lamda.tracking import TrackingConfig

from scripts.lamda.data import dictionary_digest
from scripts.lamda_ml import (
    IcebergInput, SplitPolicy, binary_matrix, choose_threshold, load_contract,
    metrics, predict, stage, train,
)
from scripts.load_lamda_local_iceberg import local_catalog_config


# Generate reproducible binary features, APK identities, published splits, and intentionally
# excluded leakage columns.
def fixture(n=400, offset=0):
    ids = np.arange(offset, offset + n)
    return pa.table({
        "hash": [hashlib.sha256(str(i).encode()).hexdigest() for i in ids],
        "label": pa.array(ids % 2, type=pa.int64()),
        "year_month": [f"{2021 + i % 3}-06" for i in ids],
        "split_name": ["test" if i % 5 == 0 else "train" for i in ids],
        "config_name": ["Baseline"] * n,
        "dataset_id": ["IQSeC-Lab/LAMDA"] * n,
        "feat_0": pa.array(ids % 2, type=pa.int64()),
        "feat_1": pa.array(1 - ids % 2, type=pa.int64()),
        "feat_2": pa.array((ids // 2) % 2, type=pa.int64()),
        # Tempting perfect label leakage must never enter the feature matrix.
        "vt_count": ids % 2 * 20,
        "family": ["benign" if i % 2 == 0 else "malware" for i in ids],
    })


# Check raw-to-sparse conversion, canonical ordering, and split/threshold rules independently of
# storage.
class BinaryTests(unittest.TestCase):
    def test_golden_integer_widths_order_and_metadata_exclusion(self):
        for dtype in (pa.int8(), pa.int16(), pa.int32(), pa.int64(), pa.uint8()):
            batch = pa.table({"feat_1": pa.array([1, 0, 1], type=dtype),
                              "label": [1, 1, 1], "feat_0": pa.array([0, 1, 1], type=dtype)})
            actual = binary_matrix(batch, ["feat_0", "feat_1"])
            np.testing.assert_array_equal(actual.toarray(), [[0, 1], [1, 0], [1, 1]])
            self.assertEqual(actual.dtype, np.float32)

    def test_reject_null_nonbinary_float_and_missing(self):
        for values in ([0, None], [0, 2], [-1, 1], [0.0, 1.0], [float("nan"), 1.0], [float("inf"), 1.0]):
            with self.assertRaises(ValueError):
                binary_matrix(pa.table({"feat_0": values}), ["feat_0"])
        with self.assertRaises(ValueError):
            binary_matrix(pa.table({"other": [1]}), ["feat_0"])

    def test_canonical_mapping_is_complete_and_config_specific(self):
        contract = load_contract()
        presence = load_contract("lamda.malware_presence@1")
        self.assertEqual(contract["columns"], [f"feat_{i}" for i in range(4561)])
        self.assertEqual(contract["config_name"], "Baseline")
        self.assertEqual(presence["columns"], contract["columns"])
        self.assertEqual(presence["dictionary_sha256"], contract["dictionary_sha256"])
        self.assertTrue(all(ref.endswith("@1") for ref in contract["features"]))
        # Exercise the complete production-width projection, not only the fixture.
        row = pa.table({name: pa.array([i % 2], type=pa.int64())
                        for i, name in enumerate(contract["columns"])})
        matrix = binary_matrix(row, contract["columns"])
        self.assertEqual(matrix.shape, (1, 4561))
        self.assertEqual(matrix.nnz, 2280)

    def test_dictionary_digest_ignores_crlf_checkout_bytes(self):
        source = Path(__file__).resolve().parents[1] / "data" / "lamda_feature_descriptions.json"
        lf = source.read_bytes().replace(b"\r\n", b"\n")
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            lf_path, crlf_path = root / "lf.json", root / "crlf.json"
            lf_path.write_bytes(lf)
            crlf_path.write_bytes(lf.replace(b"\n", b"\r\n"))
            expected = load_contract()["dictionary_sha256"]
            self.assertEqual(dictionary_digest(lf_path), expected)
            self.assertEqual(dictionary_digest(crlf_path), expected)
            self.assertNotEqual(hashlib.sha256(crlf_path.read_bytes()).hexdigest(), expected)

    def test_splits_and_thresholds(self):
        policy = SplitPolicy()
        apk = "a" * 64
        self.assertEqual(policy.assign(apk, "test", "2025-01"), "test")
        self.assertEqual(policy.assign(apk, "train", "2021-01"), policy.assign(apk, "train", "2025-01"))
        temporal = SplitPolicy(train_through="2022-12", validation_through="2023-12")
        for date, expected in (("2022-12", "train"), ("2023-01", "validation"), ("2023-12", "validation"), ("2024-01", "test")):
            self.assertEqual(temporal.assign(apk, "test", date), expected)
        for args in ({"train_through": "2022-12"}, {"train_through": "2023-12", "validation_through": "2022-12"}, {"seed": -1}):
            with self.assertRaises(ValueError):
                SplitPolicy(**args)
        with self.assertRaises(ValueError):
            policy.assign(apk, "other", "2022-01")
        with self.assertRaises(ValueError):
            policy.assign(apk, "train", "2022-13")
        y, p = np.array([0, 1, 0, 1]), np.array([0.1, 0.4, 0.6, 0.9])
        threshold = choose_threshold(y, p)
        self.assertEqual(f1_score(y, p >= threshold), max(f1_score(y, p >= t) for t in p))
        self.assertIsNone(metrics([0, 0], [0.1, 0.2], 0.5)["roc_auc"])


# Exercise snapshot-aware reads and training artifacts against real temporary Iceberg tables.
class IcebergMLTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.contract = load_contract()
        # Small explicit test-only feature set; production always resolves all 4561.
        cls.contract = {**cls.contract, "columns": ["feat_0", "feat_1", "feat_2"],
                        "features": cls.contract["features"][:3], "shape": [3]}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.tracking = TrackingConfig(uri="sqlite:///:memory:", experiment="fixture-" + uuid4().hex)
        MlflowClient(tracking_uri=self.tracking.uri).create_experiment(
            self.tracking.experiment, artifact_location=(self.root / "mlflow-artifacts").as_uri())
        self.catalog = load_catalog("test", **local_catalog_config(self.root))
        self.catalog.create_namespace("raw_lamda")
        data = fixture()
        self.table = self.catalog.create_table("raw_lamda.lamda_samples", schema=data.schema)
        self.table.append(data)

    def tearDown(self):
        self.catalog.close()
        self.tmp.cleanup()

    # Wrap the current fixture table with the same snapshot-pinning contract used in production.
    def source(self, snapshot=None):
        return IcebergInput(self.table, self.contract, snapshot)

    def test_snapshot_pinning_filter_and_projection(self):
        source = self.source()
        self.table.append(fixture(20, offset=400))
        batches = list(source.batches(31))
        self.assertEqual(sum(len(b) for b in batches), 400)
        self.assertTrue(all(len(b) <= 31 for b in batches))
        self.assertNotIn("vt_count", batches[0].column_names)
        self.assertEqual(source.manifest["table_uuid"], str(self.table.metadata.table_uuid))
        self.assertEqual(len(source.manifest["fields"]), 9)
        self.assertEqual(sum(len(b) for b in self.source(source.snapshot_id).batches(80)), 400)
        mixed = fixture(5, offset=500).set_column(4, "config_name", pa.array(["var_thresh_0.01"] * 5))
        self.table.append(mixed)
        self.assertEqual(sum(len(b) for b in self.source().batches(100)), 420)
        with self.assertRaises(ValueError):
            self.source(12345)

    def test_iceberg_delete_semantics(self):
        self.table.delete("label = 1")
        batches = list(self.source().batches(31))
        self.assertEqual(sum(len(b) for b in batches), 200)
        self.assertTrue(all(set(b["label"].to_pylist()) == {0} for b in batches))

    def test_overlap_fails_before_fit_even_with_changed_case(self):
        duplicate = fixture(1).set_column(0, "hash", pa.array([fixture(1)["hash"][0].as_py().upper()]))
        duplicate = duplicate.set_column(3, "split_name", pa.array(["train"]))
        self.table.append(duplicate)
        with patch("scripts.lamda.models.sgd.SGDClassifier.partial_fit") as fit:
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(ValueError, "Repeated APK"):
                train(self.source(), self.root / "bad", epochs=1, tracking=self.tracking)
            fit.assert_not_called()
        self.assertEqual(json.loads((self.root / "bad/status.json").read_text())["status"], "failed")
        self.assertFalse((self.root / "bad/model.joblib").exists())

    def test_split_requires_both_classes(self):
        self.table.delete("label = 1")
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(ValueError, "both classes"):
            stage(self.source(), self.root / "cache", SplitPolicy(), 100)

    def test_full_training_artifact_inference_and_no_test_fitting(self):
        output = self.root / "model"
        with contextlib.redirect_stdout(io.StringIO()):
            report = train(self.source(), output, batch_size=80, epochs=2, tracking=self.tracking)
        artifact = joblib.load(output / "model.joblib")
        manifest = json.loads((output / "manifest.json").read_text())
        training_count = sum(manifest["class_counts"]["train"])
        # t_ is the number of SGD updates plus one, ruling out validation/test fits.
        self.assertEqual(artifact["model"].estimator.t_, 2 * training_count + 1)
        self.assertEqual(sum(map(sum, manifest["class_counts"].values())), 400)
        self.assertEqual(report["test"]["rows"], 80)
        self.assertGreater(report["test"]["average_precision"], 0.9)
        inputs = fixture(10).drop(["label", "family", "vt_count"])
        scored = predict(artifact, inputs)
        reordered = predict(artifact, inputs.select(list(reversed(inputs.column_names))))
        self.assertEqual(scored.to_pydict(), reordered.to_pydict())
        self.assertEqual(len(scored), 10)
        # Exercise the actual batch CLI and its unlabeled Parquet contract.
        from scripts.predict_lamda_malware import main as predict_main
        input_path, prediction_path = self.root / "input.parquet", self.root / "predictions.parquet"
        pq.write_table(inputs, input_path)
        arguments = ["predict", "--model", str(output / "model.joblib"), "--input", str(input_path),
                     "--output", str(prediction_path), "--batch-size", "3"]
        with patch("sys.argv", arguments), contextlib.redirect_stdout(io.StringIO()):
            predict_main()
        self.assertEqual(pq.read_table(prediction_path).to_pydict(), scored.to_pydict())
        with patch("sys.argv", arguments), self.assertRaises(FileExistsError):
            predict_main()
        with self.assertRaises(ValueError):
            predict(artifact, inputs.set_column(inputs.column_names.index("config_name"), "config_name", pa.array(["var_thresh_0.01"] * 10)))
        with self.assertRaises(ValueError):
            predict(artifact, inputs.drop(["feat_0"]))
        with self.assertRaises(FileExistsError):
            train(self.source(), output, epochs=1, tracking=self.tracking)
        # Replaying identical source/seed is deterministic in the same environment.
        with contextlib.redirect_stdout(io.StringIO()):
            replay = train(self.source(), self.root / "replay", batch_size=80, epochs=2, tracking=self.tracking)
        self.assertEqual(report, replay)


if __name__ == "__main__":
    unittest.main()
