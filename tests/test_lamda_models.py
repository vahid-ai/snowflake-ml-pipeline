"""Real MLflow and CPU Lightning integration; synthetic scores are not LAMDA results."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

import joblib
from mlflow import MlflowClient
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from pyiceberg.catalog import load_catalog
from scipy import sparse

from test_lamda_ml import fixture
from scripts.lamda.data import IcebergInput, SplitPolicy, load_contract, stage
from scripts.lamda.models import create_adapter
from scripts.lamda.models.base import TrainingData
from scripts.lamda.pipeline import predict, train
from scripts.lamda.tracking import TrackingConfig
from scripts.load_lamda_local_iceberg import local_catalog_config

try:
    import torch
    from scripts.lamda.models.lightning import MalwareModule, SparseBatches
    HAS_LIGHTNING = True
except ImportError:
    HAS_LIGHTNING = False


class ModelPipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        contract = load_contract()
        cls.contract = {**contract, "columns": ["feat_0", "feat_1", "feat_2"],
                        "features": contract["features"][:3], "shape": [3]}
        if HAS_LIGHTNING:
            cls.previous_threads = torch.get_num_threads()
            torch.set_num_threads(1)

    @classmethod
    def tearDownClass(cls):
        if HAS_LIGHTNING:
            torch.set_num_threads(cls.previous_threads)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.tracking = TrackingConfig(uri="sqlite:///:memory:", experiment="models-" + uuid4().hex)
        self.client = MlflowClient(tracking_uri=self.tracking.uri)
        self.experiment_id = self.client.create_experiment(
            self.tracking.experiment, artifact_location=(self.root / "artifacts").as_uri())
        self.catalog = load_catalog("lamda_local", **local_catalog_config(self.root))
        self.catalog.create_namespace("raw_lamda")
        self.table = self.catalog.create_table("raw_lamda.lamda_samples", schema=fixture().schema)
        self.table.append(fixture())

    def tearDown(self):
        self.catalog.close()
        self.tmp.cleanup()

    def source(self):
        return IcebergInput(self.table, self.contract)

    def run_model(self, name, output=None, **kwargs):
        output = output or self.root / name
        with contextlib.redirect_stdout(io.StringIO()):
            report = train(self.source(), output, model=name, tracking=self.tracking,
                           epochs=2, batch_size=83, **kwargs)
        artifact = joblib.load(output / "model.joblib")
        parent = self.client.get_run(artifact["tracking"]["run_id"])
        self.assertEqual(parent.info.status, "FINISHED")
        self.assertEqual(parent.data.tags["model.type"], name)
        self.assertEqual(parent.data.params["snapshot_id"], str(self.source().snapshot_id))
        self.assertAlmostEqual(parent.data.metrics["test.average_precision"], report["test"]["average_precision"])
        children = self.client.search_runs([self.experiment_id],
            filter_string=f"tags.`mlflow.parentRunId` = '{parent.info.run_id}'")
        self.assertEqual(len(children), len(report["selection"]))
        for child in children:
            self.assertEqual(child.info.status, "FINISHED")
            history = self.client.get_metric_history(child.info.run_id, "train.loss")
            self.assertEqual([m.step for m in history], [0, 1])
            self.assertTrue(all(np.isfinite(m.value) for m in history))
        paths = {x.path for x in self.client.list_artifacts(parent.info.run_id)}
        self.assertTrue({"model.joblib", "metrics.json", "manifest.json", "input_contract.json"} <= paths)
        self.assertNotIn("cache", paths)
        self.assertNotIn("test_predictions.parquet", paths)
        return report, artifact, children

    def test_sgd_tracking_and_legacy_inference(self):
        report, artifact, children = self.run_model("sgd")
        self.assertEqual(len(children), 2)
        legacy = {**artifact, "format_version": 1, "model": artifact["model"].estimator}
        unlabeled = fixture(15).drop(["label", "family", "vt_count"])
        self.assertEqual(predict(artifact, unlabeled).to_pydict(), predict(legacy, unlabeled).to_pydict())

    @unittest.skipUnless(HAS_LIGHTNING, "install --extra lightning for neural integration")
    def test_neural_models_fit_scope_tracking_reload_and_cli(self):
        options = dict(hidden_dims=[8, 4], latent_dim=2, neural_batch_size=17, learning_rate=0.01)
        for name in ("mlp", "autoencoder"):
            with self.subTest(model=name):
                observed = []
                original = MalwareModule.training_step
                def observe(module, batch, batch_idx):
                    observed.extend(batch[1].detach().cpu().tolist())
                    return original(module, batch, batch_idx)
                with patch.object(MalwareModule, "training_step", observe):
                    report, artifact, children = self.run_model(name, model_options=options)
                manifest = json.loads((self.root / name / "manifest.json").read_text())
                counts = manifest["class_counts"]["train"]
                expected = counts[0] if name == "autoencoder" else sum(counts)
                self.assertEqual(len(observed), 2 * expected)
                if name == "autoencoder":
                    self.assertEqual(set(observed), {0.0})
                    self.assertEqual(artifact["score_kind"], "anomaly")
                    self.assertIsNone(report["test"]["log_loss"])
                    self.assertNotIn("test_at_0_5", report)
                    column = "anomaly_score"
                else:
                    self.assertEqual(sum(observed), 2 * counts[1])
                    column = "malware_probability"
                child = children[0]
                history = self.client.get_metric_history(child.info.run_id, "train.rows")
                self.assertEqual([m.value for m in history], [expected, expected])
                self.assertIn("model.ckpt", {x.path for x in self.client.list_artifacts(child.info.run_id)})
                unlabeled = fixture(23).drop(["label", "family", "vt_count"])
                scored = predict(artifact, unlabeled)
                self.assertTrue(np.isfinite(scored[column].to_numpy()).all())
                # Portable payload and native Lightning checkpoint produce the same scores.
                checkpoint = MalwareModule.load_from_checkpoint(
                    self.root / name / "candidates/0/model.ckpt", map_location="cpu", weights_only=False)
                checkpoint.eval()
                from scripts.lamda.data import binary_matrix
                x = torch.from_numpy(binary_matrix(unlabeled, self.contract["columns"]).toarray())
                with torch.inference_mode():
                    logits = checkpoint(x)
                    expected_scores = (torch.nn.functional.binary_cross_entropy_with_logits(
                        logits, x, reduction="none").mean(1) if name == "autoencoder" else logits.squeeze(-1).sigmoid())
                np.testing.assert_allclose(scored[column].to_numpy(), expected_scores.numpy(), rtol=1e-6, atol=1e-7)
                from scripts.predict_lamda_malware import main
                input_path, result_path = self.root / f"{name}.parquet", self.root / f"{name}-predictions.parquet"
                pq.write_table(unlabeled, input_path)
                with patch("sys.argv", ["predict", "--model", str(self.root / name / "model.joblib"),
                        "--input", str(input_path), "--output", str(result_path), "--batch-size", "7"]), \
                        contextlib.redirect_stdout(io.StringIO()):
                    main()
                np.testing.assert_allclose(pq.read_table(result_path)[column].to_numpy(), scored[column].to_numpy(), rtol=1e-6)
                with self.assertRaisesRegex(ValueError, "matching config_name"):
                    predict(artifact, unlabeled.set_column(unlabeled.column_names.index("config_name"),
                                                          "config_name", pa.array(["wrong"] * len(unlabeled))))
                # Identical source, seed and settings replay on CPU.
                replay, _, _ = self.run_model(name, self.root / f"{name}-replay", model_options=options)
                self.assertEqual(report, replay)

    @unittest.skipUnless(HAS_LIGHTNING, "install --extra lightning for neural integration")
    def test_full_width_sparse_to_dense_golden_and_benign_filter(self):
        folder = self.root / "golden"
        folder.mkdir()
        values = np.zeros((5, 4561), dtype=np.float32)
        for i in range(5):
            values[i, [i, 4560]] = 1
        sparse.save_npz(folder / "shard.npz", sparse.csr_matrix(values))
        pq.write_table(pa.table({"label": [0, 1, 0, 1, 0]}), folder / "shard.parquet")
        data = TrainingData(folder, ("shard",), (3, 2), 4561)
        for kind in ("mlp", "autoencoder"):
            network = MalwareModule(kind, 4561, [8, 4], 2, 0.0, 0.001)
            with torch.inference_mode():
                output = network(torch.from_numpy(values[:2]))
            self.assertEqual(tuple(output.shape), (2, 1 if kind == "mlp" else 4561))
            self.assertTrue(torch.isfinite(output).all())
        for benign in (False, True):
            seen = []
            for x, y in SparseBatches(data, 2, benign_only=benign):
                self.assertLessEqual(len(x), 2)
                self.assertEqual(x.dtype, torch.float32)
                for row, label in zip(x.numpy(), y.numpy()):
                    index = np.flatnonzero(row[:5])[0]
                    np.testing.assert_array_equal(row, values[index])
                    if benign:
                        self.assertEqual(label, 0)
                    seen.append(index)
            self.assertEqual(sorted(seen), [0, 2, 4] if benign else list(range(5)))

    @unittest.skipUnless(HAS_LIGHTNING, "install --extra lightning for neural integration")
    def test_training_cli_selects_models_and_preserves_tracking(self):
        from scripts.train_lamda_malware import main
        for kind in ("mlp", "autoencoder"):
            output = self.root / f"cli-{kind}"
            arguments = ["train", "--local-root", str(self.root), "--model", kind,
                         "--output", str(output), "--epochs", "1", "--hidden-dims", "8", "4",
                         "--neural-batch-size", "17", "--mlflow-tracking-uri", self.tracking.uri,
                         "--mlflow-experiment", self.tracking.experiment]
            if kind == "autoencoder":
                arguments += ["--latent-dim", "2"]
            with patch("sys.argv", arguments), patch("scripts.train_lamda_malware.load_contract", return_value=self.contract), \
                    contextlib.redirect_stdout(io.StringIO()):
                main()
            artifact = joblib.load(output / "model.joblib")
            self.assertEqual(artifact["model_type"], kind)
            self.assertEqual(self.client.get_run(artifact["tracking"]["run_id"]).info.status, "FINISHED")

    def test_adapter_failure_marks_parent_and_child_failed_without_exception_text(self):
        class BrokenAdapter:
            name, backend = "broken", "test"
            def candidates(self):
                return [{"example": 1}]
            def fit(self, data, parameters, **kwargs):
                self.assertion = all(name.startswith("train/") or name.startswith("train\\") for name in data.shards)
                if not self.assertion:
                    raise AssertionError("Adapter received evaluation shards")
                raise RuntimeError("secret-url-must-not-be-logged")
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(RuntimeError, "secret-url"):
            train(self.source(), self.root / "failed", adapter=BrokenAdapter(), tracking=self.tracking)
        runs = self.client.search_runs([self.experiment_id])
        self.assertEqual(len(runs), 2)
        self.assertEqual({r.info.status for r in runs}, {"FAILED"})
        for run in runs:
            self.assertEqual(run.data.tags["failure.type"], "RuntimeError")
            self.assertNotIn("secret-url", json.dumps(run.data.tags))
        self.assertEqual(json.loads((self.root / "failed/status.json").read_text())["status"], "failed")

    def test_bad_source_creates_failed_run_before_fitting(self):
        self.table.append(fixture(1))
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(ValueError, "Repeated APK"):
            train(self.source(), self.root / "bad-source", tracking=self.tracking)
        runs = self.client.search_runs([self.experiment_id])
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0].info.status, "FAILED")

    def test_tracking_unavailable_stops_before_data_staging(self):
        with patch("scripts.lamda.pipeline.TrackingSession", side_effect=RuntimeError("tracking unavailable")), \
                patch("scripts.lamda.pipeline.stage") as staging, self.assertRaises(RuntimeError):
            train(self.source(), self.root / "no-tracking", tracking=self.tracking)
        staging.assert_not_called()

    def test_invalid_model_configuration_rejected(self):
        with self.assertRaises(ValueError):
            create_adapter("unknown")
        with self.assertRaises(ValueError):
            create_adapter("sgd", alphas=[-1])
        if HAS_LIGHTNING:
            for values in ({"hidden_dims": [0]}, {"learning_rate": float("nan")},
                           {"dropout": 1}, {"neural_batch_size": 0}):
                with self.assertRaises(ValueError):
                    create_adapter("mlp", **values)


if __name__ == "__main__":
    unittest.main()
