import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

PLUGIN = Path(__file__).resolve().parents[1]
VALIDATOR = PLUGIN / "scripts" / "featurectl.py"
TEMPLATE = PLUGIN / "templates" / "project" / "feature-platform"

class FeatureCtlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = Path(self.tmp.name)
        shutil.copytree(TEMPLATE, self.project / "feature-platform")

    def tearDown(self):
        self.tmp.cleanup()

    def run_validate(self):
        return subprocess.run(
            [sys.executable, str(VALIDATOR), "validate", "--project-dir", str(self.project)],
            text=True, capture_output=True
        )

    def test_template_is_valid(self):
        p = self.run_validate()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    def test_iceberg_experiment_pin_is_checked_in_project(self):
        path = self.project / "feature-platform" / "experiments" / "ablation.yaml"
        doc = yaml.safe_load(path.read_text())
        doc["experiments"][0]["dataset"] = {"format": "iceberg", "table": "prod.ml.features"}
        path.write_text(yaml.safe_dump(doc))
        result = self.run_validate()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("iceberg_snapshot_id", result.stdout)
        doc["experiments"][0]["dataset"]["iceberg_snapshot_id"] = 918273645
        path.write_text(yaml.safe_dump(doc))
        self.assertEqual(self.run_validate().returncode, 0)

    def test_embedded_fitted_state_is_rejected(self):
        path = self.project / "feature-platform" / "features" / "network.yaml"
        text = path.read_text()
        text = text.replace("artifact_type: scaler_parameters", "artifact_type: scaler_parameters\n        mean: 123.0")
        path.write_text(text)
        p = self.run_validate()
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("embeds fitted values", p.stdout)

    def test_unversioned_feature_set_ref_is_rejected(self):
        path = self.project / "feature-platform" / "feature_sets" / "baseline.yaml"
        text = path.read_text().replace("device.bytes_sent_5m@1", "device.bytes_sent_5m", 1)
        path.write_text(text)
        p = self.run_validate()
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("unversioned feature ref", p.stdout)

    def test_raw_feature_source_type_mismatch_is_rejected(self):
        path = self.project / "feature-platform" / "features" / "network.yaml"
        text = path.read_text().replace("type: uint16", "type: int32", 1)
        path.write_text(text)
        p = self.run_validate()
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("differs from source column type", p.stdout)

    def test_reference_backend_must_cover_used_types(self):
        path = self.project / "feature-platform" / "capabilities" / "backends.yaml"
        data = yaml.safe_load(path.read_text())
        del data["backends"]["python_arrow"]["type_mappings"]["int64"]
        path.write_text(yaml.safe_dump(data, sort_keys=False))
        p = self.run_validate()
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("reference backend", p.stdout)
        self.assertIn("int64", p.stdout)

    def test_breaking_change_requires_migration_and_approval(self):
        changes = self.project / "feature-platform" / "changes"
        changes.mkdir(exist_ok=True)
        path = changes / "breaking.yaml"
        path.write_text(
            yaml.safe_dump(
                {
                    "spec_version": "1.0",
                    "changes": [
                        {
                            "id": "device.break_port_contract",
                            "version": 1,
                            "status": "proposed",
                            "change_type": "breaking_semantic",
                            "intent": "Change the port encoding contract.",
                            "compatibility": "breaking",
                            "affected_features": ["device.destination_port_hashed@1"],
                            "verification": {
                                "required": ["contract", "dag", "leakage", "portability", "golden_differential"]
                            },
                            "approval": {"required": False},
                        }
                    ],
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        p = self.run_validate()
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("requires migration.strategy", p.stdout)
        self.assertIn("approval.required: true", p.stdout)
        self.assertIn("affected_consumers inventory", p.stdout)

    def test_valid_additive_change_contract_passes(self):
        changes = self.project / "feature-platform" / "changes"
        changes.mkdir(exist_ok=True)
        path = changes / "additive.yaml"
        path.write_text(
            yaml.safe_dump(
                {
                    "spec_version": "1.0",
                    "changes": [
                        {
                            "id": "device.add_window",
                            "version": 1,
                            "status": "proposed",
                            "change_type": "additive_semantic",
                            "intent": "Add a second trailing window.",
                            "compatibility": "backward_compatible",
                            "affected_features": ["device.bytes_sent_5m@1"],
                            "proposed_features": ["device.bytes_sent_30m@1"],
                            "verification": {
                                "required": ["contract", "dag", "leakage", "portability", "golden_differential"]
                            },
                            "approval": {"required": False},
                        }
                    ],
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        p = self.run_validate()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

if __name__ == "__main__":
    unittest.main()
