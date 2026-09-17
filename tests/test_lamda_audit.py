"""Adversarial contracts, full scans, publication, and raw-to-tensor golden cases."""
from copy import deepcopy
import contextlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

import numpy as np
import pyarrow as pa
from scipy import sparse
import yaml
import joblib
from mlflow import MlflowClient

from scripts.lamda.audit import AuditError, Issues, Profile, require_certified, run_audit
from scripts.lamda.contracts import ContractError, Plan, validate_tensor
from scripts.lamda.data import IcebergInput, SplitPolicy, load_contract, stage
from scripts.lamda.observations import publish_iceberg, publish_local
from scripts.lamda.pipeline import environment_manifest, predict, train
from scripts.lamda.tracking import TrackingConfig
from scripts.load_lamda_local_iceberg import open_local_catalog
from tests.test_lamda_ml import fixture


# Create a minimal versioned binary feature contract with source locations for diagnostic
# assertions.
def contract(n=3):
    definitions = {}
    for i in range(n):
        definitions[f"test.f{i}@1"] = {"id": f"test.f{i}", "version": 1, "column": f"feat_{i}",
            "source": "test", "semantic_type": "binary", "output": {"type": "int64", "nullable": True},
            "model_representation": {"kind": "scalar", "dtype": "float32"}, "missing": {"strategy": "error"},
            "validation": {"min": 0, "max": 1}, "location": f"features/test.yaml:{10+i}"}
    return {"id": "test.set@1", "features": list(definitions), "definitions": definitions,
            "columns": [f"feat_{i}" for i in range(n)], "shape": [n], "dtype": "float32",
            "dataset_id": "IQSeC-Lab/LAMDA", "config_name": "Baseline"}


# Derive a one-input transform contract without mutating the underlying raw feature definition.
def transformed(op, *, logical="int64", params=None, raw_nullable=False):
    value = contract(1)
    raw = value["definitions"]["test.f0@1"]
    raw.update(semantic_type="continuous", validation={"min": 0}, missing={"strategy": "propagate" if raw_nullable else "error"})
    output = deepcopy(raw)
    output.pop("column")
    output.pop("source")
    output.update(id="test.output", inputs=["test.f0@1"],
                  transform={"kind": "builtin", "op": op, "params": params or {}},
                  output={"type": logical, "nullable": False}, missing={"strategy": "error"})
    if op == "positive_presence":
        output.update(semantic_type="binary", validation={"min": 0, "max": 1})
    value["definitions"]["test.output@1"] = output
    value["features"] = ["test.output@1"]
    return value


# Mimic snapshot and schema interfaces so contract failures can be isolated from connector
# behavior.
class MemorySource:
    def __init__(self, data=None, definition=None):
        self.data, self.contract = data if data is not None else fixture(), definition or contract()
        self.snapshot_id = 42
        def physical(field):
            if pa.types.is_integer(field.type):
                return "long"
            if pa.types.is_string(field.type):
                return "string"
            return str(field.type)
        self.fields = {f.name: SimpleNamespace(field_type=physical(f), required=not f.nullable) for f in self.data.schema}
        self.audit_scan = SimpleNamespace(projection=lambda: SimpleNamespace(
            column_names=self.data.column_names, find_field=lambda name: self.fields[name]))
        self.manifest = {"table": "test.raw.samples", "table_uuid": "test-table", "snapshot_id": 42,
                         "schema_id": 0, "dataset_id": self.contract["dataset_id"], "config_name": self.contract["config_name"],
                         "fields": [{"column": name, "field_id": i+1, "physical_type": str(f.field_type),
                                     "required": f.required} for i, (name, f) in enumerate(self.fields.items())]}

    # Yield deterministic slices to exercise bounded scans and late-occurring violations.
    def audit_batches(self, batch_size):
        for offset in range(0, len(self.data), batch_size):
            yield self.data.slice(offset, batch_size)


# Substitute one Arrow column while preserving the surrounding fixture schema and row data.
def replace(data, name, values, dtype=None):
    array = pa.array(values, type=dtype)
    return data.set_column(data.schema.get_field_index(name), name, array)


# Verify feature operations, domain checks, type conversions, lineage, and final tensor
# requirements.
class ContractTests(unittest.TestCase):
    # Run a feature plan and return both the candidate matrix and aggregated issues for
    # assertions.
    def execute(self, definition, values, dtype=None, model="sgd"):
        issues = Issues()
        matrix = Plan(definition).execute(pa.table({"feat_0": pa.array(values, type=dtype)}), issues, model=model)
        return matrix, issues

    def test_binary_two_three_four_five_negative_and_null(self):
        for bad in [2, 3, 4, 5, -1, -2, None]:
            with self.subTest(value=bad):
                matrix, issues = self.execute(contract(1), [0, 1, bad])
                self.assertIsNone(matrix)
                self.assertEqual(issues.records()[0]["code"], "UNHANDLED_NULL" if bad is None else "BINARY_DOMAIN")
                self.assertEqual(issues.records()[0]["count"], 1)
                self.assertEqual(issues.records()[0]["location"], "features/test.yaml:10")

    def test_float_and_string_do_not_implicitly_become_binary(self):
        for values in ([0., 1.], ["0", "1"], [False, True]):
            with self.subTest(values=values):
                matrix, issues = self.execute(contract(1), values)
                self.assertIsNone(matrix)
                self.assertEqual(issues.records()[0]["code"], "TYPE_MISMATCH")

    def test_positive_presence_golden_and_original_contract_stays_strict(self):
        definition = transformed("positive_presence")
        matrix, issues = self.execute(definition, [0, 1, 2, 3, 5, 2**62])
        self.assertEqual(issues.error_count, 0)
        np.testing.assert_array_equal(matrix.toarray(), [[0], [1], [1], [1], [1], [1]])
        self.assertEqual(matrix.dtype, np.float32)
        for bad in (None, -1):
            self.assertIsNone(self.execute(definition, [0, bad])[0])
        self.assertIsNone(self.execute(contract(1), [0, 2])[0])

    def test_explicit_null_fill_and_post_transform_contract(self):
        definition = transformed("fill_null", params={"value": 0}, raw_nullable=True)
        matrix, issues = self.execute(definition, [0, None, 2], pa.int64())
        self.assertEqual(issues.error_count, 0)
        np.testing.assert_array_equal(matrix.toarray().ravel(), [0, 0, 2])
        definition["definitions"]["test.output@1"]["validation"]["max"] = 1
        self.assertIsNone(self.execute(definition, [None, 2], pa.int64())[0])
        definition["definitions"]["test.output@1"]["transform"]["params"]["value"] = -1
        self.assertIn("RANGE_MIN", [i["code"] for i in self.execute(definition, [None], pa.int64())[1].records()])

    def test_clip_log_cast_and_identity_golden(self):
        for op, params, logical, expected in [
            ("clip", {"min": 0, "max": 1}, "int64", [0, 1, 1]),
            ("log1p_nonnegative", {}, "float32", np.log1p([0, 1, 2])),
            ("cast", {}, "float32", [0, 1, 2]), ("identity", {}, "int64", [0, 1, 2])]:
            with self.subTest(op=op):
                matrix, issues = self.execute(transformed(op, params=params, logical=logical), [0, 1, 2])
                self.assertEqual(issues.error_count, 0)
                np.testing.assert_allclose(matrix.toarray().ravel(), expected, rtol=1e-6)

    def test_nonfinite_and_float32_overflow(self):
        definition = contract(1)
        node = definition["definitions"]["test.f0@1"]
        node.update(semantic_type="continuous", output={"type": "float64", "nullable": False}, validation={})
        for bad, code in [(float("nan"), "NON_FINITE"), (float("inf"), "NON_FINITE"), (-float("inf"), "NON_FINITE"), (1e300, "TENSOR_NON_FINITE")]:
            with self.subTest(value=bad):
                matrix, issues = self.execute(definition, [0., bad])
                self.assertIsNone(matrix)
                self.assertIn(code, [i["code"] for i in issues.records()])

    def test_integer_narrowing_overflow_and_unknown_category(self):
        definition = contract(1)
        node = definition["definitions"]["test.f0@1"]
        node.update(semantic_type="categorical", output={"type": "int8", "nullable": False}, validation={"allowed_values": [0, 1, 2]})
        self.assertIn("TYPE_OVERFLOW", [i["code"] for i in self.execute(definition, [256])[1].records()])
        self.assertIn("CATEGORY_DOMAIN", [i["code"] for i in self.execute(definition, [3])[1].records()])
        self.assertIsNotNone(self.execute(definition, [2])[0])

    def test_autoencoder_rejects_unbounded_counts_before_fit(self):
        definition = transformed("identity")
        self.assertIsNotNone(self.execute(definition, [0, 2], model="mlp")[0])
        self.assertIn("MODEL_DOMAIN", [i["code"] for i in self.execute(definition, [0, 2], model="autoencoder")[1].records()])

    def test_dag_and_layout_errors(self):
        mutations = [
            lambda c: c.update(shape=[2]), lambda c: c.update(dtype="float64"),
            lambda c: c.update(features=["missing@1"]),
            lambda c: c.update(features=["test.f0@1", "test.f0@1"], shape=[2]),
            lambda c: c.update(columns=["wrong"]),
            lambda c: c["definitions"]["test.f0@1"].update(output={"type": "fixed_size_list<float32, 2>", "nullable": False}),
            lambda c: c["definitions"]["test.f0@1"].update(model_representation={"kind": "tensor", "dtype": "float32"}),
            lambda c: c["definitions"]["test.f0@1"].update(missing={"strategy": "guess"})]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                definition = contract(1)
                mutate(definition)
                with self.assertRaises(ContractError):
                    Plan(definition)
        for transform in ({"kind": "fitted", "op": "standard_scaler"}, {"kind": "builtin", "op": "guess"},
                          {"kind": "builtin", "op": "clip", "params": {"min": 2, "max": 1}}, {"kind": "builtin", "op": "fill_null"}):
            definition = transformed("identity")
            definition["definitions"]["test.output@1"]["transform"] = transform
            with self.assertRaises(ContractError):
                Plan(definition)
        definition = transformed("identity")
        definition["definitions"]["test.output@1"]["inputs"] = ["test.output@1"]
        with self.assertRaisesRegex(ContractError, "cycle"):
            Plan(definition)

    def test_tensor_shape_dtype_format_and_finite(self):
        cases = [(sparse.csr_matrix(np.zeros((2, 2), dtype=np.float32)), "TENSOR_SHAPE"),
                 (sparse.csr_matrix(np.zeros((2, 1), dtype=np.float64)), "TENSOR_DTYPE"),
                 (sparse.csc_matrix(np.zeros((2, 1), dtype=np.float32)), "TENSOR_FORMAT"),
                 (sparse.csr_matrix(np.array([[np.nan], [0]], dtype=np.float32)), "TENSOR_NON_FINITE")]
        for matrix, code in cases:
            issues = Issues()
            validate_tensor(matrix, contract(1), issues, 2)
            self.assertIn(code, [i["code"] for i in issues.records()])

    def test_integer_to_model_precision_loss_and_duplicate_columns(self):
        matrix, issues = self.execute(transformed("identity"), [2**24 + 1])
        self.assertIsNone(matrix)
        self.assertIn("TENSOR_PRECISION", {i["code"] for i in issues.records()})
        issues = Issues()
        batch = pa.Table.from_arrays([pa.array([0]), pa.array([1])], names=["feat_0", "feat_0"])
        self.assertIsNone(Plan(contract(1)).execute(batch, issues))
        self.assertEqual(issues.records()[0]["code"], "DUPLICATE_COLUMN")

    def test_physical_column_order_does_not_change_feature_order(self):
        issues = Issues()
        matrix = Plan(contract(3)).execute(pa.table({"feat_2": [1, 0], "feat_0": [0, 1], "label": [1, 1], "feat_1": [1, 1]}), issues)
        np.testing.assert_array_equal(matrix.toarray(), [[0, 1, 1], [1, 1, 0]])
        self.assertEqual([row["index"] for row in Plan(contract(3)).lineage()], [0, 1, 2])

    def test_multiple_independent_feature_errors_are_aggregated(self):
        issues = Issues()
        Plan(contract(3)).execute(pa.table({"feat_0": [0, 2], "feat_1": [None, 1], "feat_2": [3, 4]}), issues)
        self.assertEqual({i["feature"] for i in issues.records()}, {"test.f0@1", "test.f1@1", "test.f2@1"})
        self.assertEqual(issues.error_count, 4)

    def test_target_leakage_and_cross_source_features_are_rejected(self):
        for name in ("label", "hash", "vt_count", "family", "split_name", "_dlt_id"):
            definition = contract(1)
            definition["columns"] = [name]
            definition["definitions"]["test.f0@1"]["column"] = name
            with self.subTest(name=name), self.assertRaises(ContractError):
                Plan(definition)
        definition = contract(1)
        definition["source"] = "wrong"
        with self.assertRaises(ContractError):
            Plan(definition)

    def test_model_domain_errors_aggregate_across_features(self):
        definition = contract(3)
        for node in definition["definitions"].values():
            node.update(semantic_type="continuous", validation={"min": 0})
        issues = Issues()
        self.assertIsNone(Plan(definition).execute(pa.table({f"feat_{i}": [i+2] for i in range(3)}), issues, model="autoencoder"))
        self.assertEqual(len([i for i in issues.records() if i["code"] == "MODEL_DOMAIN"]), 3)


# Test certification, publication, storage semantics, and the boundary that prevents invalid
# data reaching fitting.
class AuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.serial = 0

    def tearDown(self):
        self.temp.cleanup()

    # Give each audit an isolated output path while suppressing expected diagnostic console
    # output.
    def run_audit(self, source=None, **kwargs):
        self.serial += 1
        directory = self.root / str(self.serial)
        with contextlib.redirect_stdout(io.StringIO()):
            report = run_audit(source or MemorySource(), directory, batch_size=53, **kwargs)
        return report, directory

    def test_full_scan_certifies_and_eda_includes_non_model_columns(self):
        report, directory = self.run_audit()
        self.assertTrue(report["certified"])
        self.assertTrue(report["complete_scan"])
        self.assertEqual(report["rows"], 400)
        profile = {p["column"]: p for p in report["profiles"]}
        self.assertEqual(profile["hash"]["distinct_non_null"], 400)
        self.assertEqual(profile["feat_0"]["mean"], 0.5)
        self.assertEqual(profile["feat_0"]["stddev"], 0.5)
        self.assertEqual(profile["hash"]["top_values"], [])
        self.assertIn("vt_count", profile)
        self.assertNotIn("vt_count", str(report["lineage"]))
        self.assertTrue((directory / "report.html").exists())
        self.assertFalse((directory / "working.sqlite").exists())

    def test_rare_two_at_end_is_detected_and_examples_bounded(self):
        data = fixture()
        values = data["feat_0"].to_pylist()
        values[0] = values[100] = values[399] = 2
        data = replace(data, "feat_0", values)
        data = data.append_column("source_file", pa.array(["test.parquet"] * len(data)))
        data = data.append_column("row_number", pa.array(range(len(data))))
        report, _ = self.run_audit(MemorySource(data), examples=2)
        self.assertFalse(report["certified"])
        self.assertTrue(report["complete_scan"])
        issue = next(i for i in report["issues"] if i["code"] == "BINARY_DOMAIN")
        self.assertEqual(issue["count"], 3)
        self.assertEqual(len(issue["examples"]), 2)
        self.assertEqual(issue["examples"][0]["value"], 2)
        self.assertEqual(issue["examples"][0]["source_file"], "test.parquet")
        raw = next(p for p in report["profiles"] if p["column"] == "feat_0")
        self.assertEqual(raw["distinct_non_null"], 3)
        self.assertEqual(raw["max"], 2)

    def test_sample_never_certifies_or_publishes_even_when_all_sample_rows_pass(self):
        with patch("scripts.lamda.observations.publish_local") as publish:
            report, _ = self.run_audit(max_rows=300, observations_root=self.root / "observed")
        self.assertFalse(report["certified"])
        self.assertFalse(report["complete_scan"])
        publish.assert_not_called()
        self.assertEqual(report["rows"], 300)
        # Explicit sample remains advisory even if its limit exceeds dataset size.
        self.assertFalse(self.run_audit(max_rows=1000)[0]["certified"])

    def test_missing_columns_and_bad_metadata_aggregate(self):
        data = fixture().drop(["feat_0", "feat_1"])
        data = replace(data, "label", np.zeros(len(data)), pa.float64())
        report, _ = self.run_audit(MemorySource(data))
        codes = {i["code"] for i in report["issues"]}
        self.assertTrue({"MISSING_COLUMN", "METADATA_TYPE", "LABEL_DOMAIN"} <= codes)
        self.assertFalse(report["certified"])

    def test_required_iceberg_null_is_error_even_if_feature_fill_exists(self):
        data = replace(fixture(), "feat_0", [None] + [0] * 399, pa.int64())
        source = MemorySource(data, transformed("fill_null", params={"value": 0}, raw_nullable=True))
        source.fields["feat_0"].required = True
        report, _ = self.run_audit(source)
        self.assertIn("ICEBERG_REQUIRED_NULL", {i["code"] for i in report["issues"]})

    def test_identity_duplicates_case_normalization_and_split_overlap(self):
        data = fixture()
        hashes = data["hash"].to_pylist()
        hashes[1] = hashes[0].upper()  # row0 test, row1 train/validation
        report, _ = self.run_audit(MemorySource(replace(data, "hash", hashes)))
        self.assertIn("SPLIT_OVERLAP", {i["code"] for i in report["issues"]})
        hashes[5] = hashes[0]  # both published test
        report, _ = self.run_audit(MemorySource(replace(data, "hash", hashes)))
        self.assertIn("DUPLICATE_APK", {i["code"] for i in report["issues"]})

    def test_invalid_metadata_variants(self):
        for name, value, code in [("hash", None, "APK_IDENTITY"), ("hash", "abc", "APK_IDENTITY"),
                                  ("label", 2, "LABEL_DOMAIN"), ("label", None, "LABEL_DOMAIN"),
                                  ("year_month", "2025-13", "SPLIT_INVALID"), ("year_month", None, "SPLIT_INVALID"),
                                  ("split_name", "validation", "SPLIT_INVALID"),
                                  ("config_name", "other", "DATASET_MISMATCH")]:
            with self.subTest(name=name, value=value):
                data = fixture()
                values = data[name].to_pylist()
                values[0] = value
                report, _ = self.run_audit(MemorySource(replace(data, name, values, data[name].type)))
                self.assertIn(code, {i["code"] for i in report["issues"]})

    def test_empty_missing_selection_and_single_class(self):
        for data, code in [(fixture().slice(0, 0), "EMPTY_DATASET"),
                           (fixture().drop(["dataset_id"]), "MISSING_COLUMN"),
                           (replace(fixture(), "label", [0] * 400), "SPLIT_CLASSES")]:
            report, _ = self.run_audit(MemorySource(data))
            self.assertIn(code, {i["code"] for i in report["issues"]})
            self.assertFalse(report["certified"])

    def test_interrupted_scan_keeps_report_but_cannot_publish(self):
        source = MemorySource()
        def interrupted(size):
            yield source.data.slice(0, 50)
            raise RuntimeError("secret-signed-url")
        source.audit_batches = interrupted
        with patch("scripts.lamda.observations.publish_local") as publish:
            report, directory = self.run_audit(source, observations_root=self.root / "observed")
        self.assertFalse(report["complete_scan"])
        publish.assert_not_called()
        self.assertIn("SCAN_FAILED", {i["code"] for i in report["issues"]})
        self.assertNotIn("secret-signed-url", (directory / "report.json").read_text())
        self.assertTrue(report["failure_trace"])
        self.assertTrue(all(set(frame) == {"file", "line", "function"} for frame in report["failure_trace"]))

    def test_certificate_is_bound_to_snapshot_contract_split_and_model(self):
        source = MemorySource()
        report, _ = self.run_audit(source)
        require_certified(report, source, SplitPolicy(), "sgd")
        for key, value in [("complete_scan", False), ("certified", False), ("contract_sha256", "other"),
                           ("source", {"snapshot_id": 43}), ("split", {}), ("model", "autoencoder")]:
            changed = {**report, key: value}
            with self.subTest(key=key), self.assertRaises(ValueError):
                require_certified(changed, source, SplitPolicy(), "sgd")

    def test_observed_yaml_updates_without_weakening_model_contract(self):
        root = self.root / "observed"
        first, _ = self.run_audit(observations_root=root)
        data = replace(fixture(), "feat_0", [2] + [0] * 399)
        source = MemorySource(data)
        original = deepcopy(source.contract)
        second, _ = self.run_audit(source, observations_root=root)
        self.assertEqual(source.contract, original)
        self.assertFalse(second["certified"])
        self.assertTrue(Path(first["observations"]["history"]).exists())
        latest = yaml.safe_load(Path(second["observations"]["latest"]).read_text())
        self.assertEqual(next(p for p in latest["profiles"] if p["column"] == "feat_0")["max"], 2)
        self.assertTrue(second["observations"]["drift"])
        # Idempotent retries preserve immutable audit histories.
        publish_local(second, root)
        tampered = deepcopy(second)
        tampered["rows"] += 1
        with self.assertRaisesRegex(ValueError, "Immutable"):
            publish_local(tampered, root)

    def test_publication_failure_blocks_fit_and_report_survives(self):
        with patch("scripts.lamda.observations.publish_local", side_effect=PermissionError("secret")):
            report, directory = self.run_audit(observations_root=self.root / "observed")
        self.assertFalse(report["certified"])
        self.assertIn("OBSERVATION_WRITE", {i["code"] for i in report["issues"]})
        self.assertTrue((directory / "report.json").exists())

    def test_report_escapes_html(self):
        source = MemorySource()
        source.contract["definitions"]["test.f0@1"]["location"] = '<script>alert("x")</script>'
        source.data = replace(source.data, "feat_0", [2] * 400)
        report, directory = self.run_audit(source)
        self.assertNotIn("<script>", (directory / "report.html").read_text())
        self.assertIn("&lt;script&gt;", (directory / "report.html").read_text())

    def test_spilled_exact_distinct_counts_and_null_profile(self):
        database = sqlite3.connect(":memory:")
        database.execute("CREATE TABLE distinct_values(col TEXT,value TEXT,n INTEGER,PRIMARY KEY(col,value))")
        profile = Profile("many", pa.int64(), database, limit=2)
        profile.add(pa.array([1, 2, 3, None]))
        profile.add(pa.array([1, 4, 5, None]))
        result = profile.result()
        self.assertEqual(result["distinct_non_null"], 5)
        self.assertEqual(result["null_count"], 2)
        self.assertEqual(result["top_values"][0], {"value": 1, "count": 2})
        np.testing.assert_allclose(result["mean"], np.mean([1, 2, 3, 1, 4, 5]))
        profile = Profile("empty", pa.int64(), database)
        profile.add(pa.array([None, None], type=pa.int64()))
        self.assertIsNone(profile.result()["mean"])
        self.assertEqual(profile.result()["distinct_non_null"], 0)
        database.close()

    def test_real_local_iceberg_atomic_observations_idempotence_and_source_unchanged(self):
        (self.root / "catalog").mkdir()
        catalog = open_local_catalog(self.root / "catalog")
        try:
            catalog.create_namespace("raw_lamda")
            table = catalog.create_table("raw_lamda.lamda_samples", schema=fixture().schema)
            table.append(fixture())
            source = IcebergInput(table, contract())
            snapshot = table.current_snapshot().snapshot_id
            report, _ = self.run_audit(source, publish_catalog=catalog)
            self.assertTrue(report["certified"], report["issues"])
            result = publish_iceberg(report, catalog)
            observations = catalog.load_table(result["table"]).scan().to_arrow()
            self.assertEqual(len(observations), len(report["profiles"]) + 1)
            self.assertEqual(observations["record_kind"].to_pylist().count("complete"), 1)
            table.refresh()
            self.assertEqual(table.current_snapshot().snapshot_id, snapshot)
            self.assertEqual(len(table.scan().to_arrow()), 400)
        finally:
            catalog.close()

    def test_bounded_reader_applies_positional_deletes_before_filtering(self):
        catalog = open_local_catalog(self.root)
        try:
            catalog.create_namespace("raw_lamda")
            data = fixture(40)
            configs = ["other" if i % 3 == 0 else "Baseline" for i in range(40)]
            data = replace(data, "config_name", configs)
            table = catalog.create_table("raw_lamda.lamda_samples", schema=data.schema)
            table.append(data)
            source = IcebergInput(table, contract())
            task = next(iter(source.audit_scan.plan_files()))
            removed = {4, 7, 12, 16, 39}
            deletes = {task.file.file_path: [pa.chunked_array([sorted(removed)], type=pa.int64())]}
            with patch("scripts.lamda.iceberg_reader._read_all_delete_files", return_value=deletes):
                actual_batches = list(source.audit_batches(7))
            self.assertTrue(all(len(batch) <= 7 for batch in actual_batches))
            actual = pa.concat_tables(actual_batches)
            expected = [v for i, v in enumerate(data["hash"].to_pylist()) if i not in removed and configs[i] == "Baseline"]
            self.assertEqual(actual["hash"].to_pylist(), expected)
        finally:
            catalog.close()

    def test_bounded_reader_pins_old_schema_after_column_rename(self):
        catalog = open_local_catalog(self.root)
        try:
            catalog.create_namespace("raw_lamda")
            table = catalog.create_table("raw_lamda.lamda_samples", schema=fixture().schema)
            table.append(fixture())
            old = table.current_snapshot().snapshot_id
            with table.update_schema() as update:
                update.rename_column("feat_2", "renamed_feature")
            data = fixture(40, 400)
            data = data.rename_columns(["renamed_feature" if name == "feat_2" else name for name in data.column_names])
            table.append(data)
            source = IcebergInput(table, contract(), old)
            actual = pa.concat_tables(list(source.audit_batches(31)))
            self.assertIn("feat_2", actual.column_names)
            self.assertNotIn("renamed_feature", actual.column_names)
            self.assertEqual(len(actual), 400)
            self.assertEqual(actual["feat_2"].to_pylist(), fixture()["feat_2"].to_pylist())
        finally:
            catalog.close()

    def test_failures_never_release_staged_training_data(self):
        source = MemorySource(replace(fixture(), "feat_0", [2] + [0] * 399))
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(AuditError):
            stage(source, self.root / "cache", SplitPolicy(), 53)
        manifest = json.loads((self.root / "cache/manifest.json").read_text())
        self.assertFalse(manifest["certified"])

    def test_environment_hashes_ignore_generated_history_but_track_real_inputs(self):
        for name in ("scripts/worker.py", "feature-platform/features/test.yaml", "pyproject.toml", "uv.lock"):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("original\n")
        with patch("scripts.lamda.pipeline.ROOT", self.root):
            original = environment_manifest()["source_sha256"]
            generated = self.root / "feature-platform/generated/raw_profiles/selection"
            generated.mkdir(parents=True)
            for name in ("latest.yaml", "snapshot-audit-1.yaml", "snapshot-audit-2.yaml"):
                (generated / name).write_text("audit: changing-history\n")
            self.assertEqual(environment_manifest()["source_sha256"], original)
            (generated / "latest.yaml").write_text("audit: a-new-run\n")
            self.assertEqual(environment_manifest()["source_sha256"], original)
            for name in ("scripts/worker.py", "feature-platform/features/test.yaml", "pyproject.toml", "uv.lock"):
                with self.subTest(input=name):
                    path = self.root / name
                    path.write_text("changed\n")
                    self.assertNotEqual(environment_manifest()["source_sha256"][str(Path(name))], original[str(Path(name))])
                    path.write_text("original\n")

    def test_pinned_filter_column_renames_preserve_partition_planning_and_projection(self):
        from pyiceberg.expressions import And, EqualTo
        from scripts.lamda.iceberg_reader import batches
        catalog = open_local_catalog(self.root)
        try:
            catalog.create_namespace("raw_lamda")
            table = catalog.create_table("raw_lamda.lamda_samples", schema=fixture().schema)
            with table.update_spec() as update:
                update.add_identity("config_name")
            other = replace(fixture(40, 400), "config_name", ["Other"] * 40)
            table.append(pa.concat_tables([fixture(), other]))
            old = table.current_snapshot().snapshot_id
            with table.update_schema() as update:
                update.rename_column("dataset_id", "renamed_dataset")
                update.rename_column("config_name", "renamed_config")
            data = fixture(40, 600)
            data = data.rename_columns([{"dataset_id": "renamed_dataset", "config_name": "renamed_config"}.get(name, name)
                                        for name in data.column_names])
            table.append(data)
            current_schema_id = table.metadata.current_schema_id
            current_snapshot_id = table.current_snapshot().snapshot_id
            source = IcebergInput(table, contract(), old)
            actual = pa.concat_tables(list(source.audit_batches(31)))
            self.assertEqual(actual["hash"].to_pylist(), fixture()["hash"].to_pylist())
            # Predicate columns need not be among the returned columns.
            narrow = table.scan(snapshot_id=old, selected_fields=("feat_0",),
                row_filter=And(EqualTo("dataset_id", "IQSeC-Lab/LAMDA"), EqualTo("config_name", "Baseline")))
            projected = pa.Table.from_batches(list(batches(table, narrow, 31)))
            self.assertEqual(projected.column_names, ["feat_0"])
            self.assertEqual(projected["feat_0"].to_pylist(), fixture()["feat_0"].to_pylist())
            self.assertEqual(table.metadata.current_schema_id, current_schema_id)
            self.assertEqual(narrow.table_metadata.current_schema_id, current_schema_id)
            self.assertEqual(table.current_snapshot().snapshot_id, current_snapshot_id)
            self.assertNotEqual(current_snapshot_id, old)
        finally:
            catalog.close()

    def tracking(self):
        config = TrackingConfig(uri="sqlite:///:memory:", experiment="audit-" + uuid4().hex)
        client = MlflowClient(tracking_uri=config.uri)
        experiment = client.create_experiment(config.experiment, artifact_location=(self.root / "artifacts").as_uri())
        return config, client, experiment

    def test_binary_two_blocks_all_adapters_before_fit_and_logs_failed_audit(self):
        tracking, client, experiment = self.tracking()
        source = MemorySource(replace(fixture(), "feat_0", [2] + [0] * 399))
        class NeverFit:
            name, backend = "test", "test"
            def candidates(self):
                return [{}]
            def fit(self, *args, **kwargs):
                raise AssertionError("Audit failed but fit was called")
        output = self.root / "failed-training"
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(AuditError):
            train(source, output, adapter=NeverFit(), tracking=tracking)
        runs = client.search_runs([experiment])
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0].info.status, "FAILED")
        paths = {a.path for a in client.list_artifacts(runs[0].info.run_id, "audit")}
        self.assertIn("audit/report.json", paths)
        self.assertFalse((output / "candidates").exists())

    def test_presence_transforms_train_and_serve_identically_for_all_models(self):
        import importlib.util
        models = ["sgd"] + (["mlp", "autoencoder"] if importlib.util.find_spec("lightning") else [])
        tracking, client, experiment = self.tracking()
        data = fixture()
        values = data["feat_0"].to_numpy() * 2
        source = MemorySource(replace(data, "feat_0", values), transformed("positive_presence"))
        for model in models:
            with self.subTest(model=model):
                output = self.root / model
                options = {} if model == "sgd" else {"hidden_dims": [4], "latent_dim": 2, "neural_batch_size": 32}
                with contextlib.redirect_stdout(io.StringIO()):
                    train(source, output, model=model, model_options=options, alphas=(.001,),
                          tracking=tracking, epochs=1, batch_size=97)
                audit = json.loads((output / "cache_audit/report.json").read_text())
                self.assertTrue(audit["certified"])
                raw = next(p for p in audit["profiles"] if p["column"] == "feat_0")
                self.assertEqual(raw["max"], 2)
                artifact = joblib.load(output / "model.joblib")
                raw_predictions = predict(artifact, source.data.slice(0, 32))
                binary_predictions = predict(artifact, data.slice(0, 32))
                self.assertEqual(raw_predictions.to_pydict(), binary_predictions.to_pydict())
                self.assertEqual(client.get_run(artifact["tracking"]["run_id"]).info.status, "FINISHED")
                self.assertEqual(audit["lineage"][0]["trace"], ["test.f0@1", "test.output@1"])
                self.assertEqual(artifact["contract"]["definitions"]["test.output@1"]["transform"]["op"], "positive_presence")

    def test_statistical_outlier_is_warning_but_declared_range_is_error(self):
        source = MemorySource(replace(fixture(), "feat_0", [1000] + [0] * 399), transformed("identity"))
        report, _ = self.run_audit(source)
        self.assertTrue(report["certified"])
        self.assertEqual(next(i for i in report["issues"] if i["code"] == "STATISTICAL_OUTLIER")["severity"], "warning")
        source.contract["definitions"]["test.f0@1"]["validation"]["max"] = 10
        report, _ = self.run_audit(source)
        self.assertFalse(report["certified"])
        self.assertIn("RANGE_MAX", {i["code"] for i in report["issues"]})

    def test_standalone_cli_exit_status_and_mlflow(self):
        from scripts.audit_lamda import main
        catalog = open_local_catalog(self.root)
        catalog.create_namespace("raw_lamda")
        table = catalog.create_table("raw_lamda.lamda_samples", schema=fixture().schema)
        table.append(fixture())
        catalog.close()
        client = MlflowClient(tracking_uri="sqlite:///:memory:")
        if client.get_experiment_by_name("lamda-malware") is None:
            client.create_experiment("lamda-malware", artifact_location=(self.root / "cli-artifacts").as_uri())
        for suffix, extra, expected in [("full", [], None), ("sample", ["--max-rows", "300"], 3)]:
            args = ["audit", "--local-root", str(self.root), "--output", str(self.root / suffix),
                    "--no-publish-observations", "--mlflow-tracking-uri", "sqlite:///:memory:", *extra]
            with patch("sys.argv", args), patch("scripts.audit_lamda.load_contract", return_value=contract()), contextlib.redirect_stdout(io.StringIO()):
                if expected is None:
                    main()
                else:
                    with self.assertRaises(SystemExit) as exc:
                        main()
                    self.assertEqual(exc.exception.code, expected)


if __name__ == "__main__":
    unittest.main()
