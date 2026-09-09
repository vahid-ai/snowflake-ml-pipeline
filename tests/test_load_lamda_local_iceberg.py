"""Offline integration tests using real PyIceberg catalogs and dlt loads."""

import contextlib
import gc
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import dlt
import pyarrow as pa
from pyiceberg.catalog import load_catalog
from pyiceberg.expressions import EqualTo
from pyiceberg.io.pyarrow import ArrowScan

from scripts.load_lamda_local_iceberg import (
    local_catalog_config,
    open_local_catalog,
    run_pipeline,
    select_tables,
    snapshot_chunks,
)


class LocalIcebergTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source_root = self.root / "source"
        self.source_root.mkdir()
        self.source = load_catalog("test_source", **local_catalog_config(self.source_root))
        self.source.create_namespace("raw_lamda")
        self.local_root = self.root / "local with spaces"
        self.data = pa.table({
            "row_number": pa.array(range(7), type=pa.int64()),
            "config_name": ["baseline"] * 7,
            "feat_0": pa.array([1, 0, None, 1, 0, 1, 1], type=pa.int64()),
            "_dlt_load_id": ["remote-load"] * 7,
        })
        self.samples = self.create_table("lamda_samples", self.data)
        self.create_table("lamda_files", pa.table({"source_file": ["train.parquet"]}))

    def tearDown(self):
        self.source.close()
        # dlt clients can retain SQLAlchemy pools in reference cycles. Release
        # their idle SQLite handles before removing the temporary Windows tree.
        gc.collect()
        self.tmp.cleanup()

    def create_table(self, name, data):
        table = self.source.create_table(("raw_lamda", name), schema=data.schema)
        if data.num_rows:
            table.append(data)
        return table

    def run_copy(self, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()):
            return run_pipeline(source_catalog=self.source, local_root=self.local_root, **kwargs)

    def read_local(self, name="lamda_samples"):
        catalog = open_local_catalog(self.local_root)
        try:
            return catalog.load_table(("raw_lamda", name)).scan().to_arrow()
        finally:
            catalog.close()

    def test_chunks_pin_snapshot_and_preserve_nulls(self):
        chunks = snapshot_chunks(self.samples, rows_per_run=2)
        first = next(chunks)
        self.samples.append(self.data.slice(0, 1))
        copied = [first, *chunks]
        self.assertEqual([t.num_rows for t in copied], [2, 2, 2, 1])
        result = pa.concat_tables(copied)
        self.assertNotIn("_dlt_load_id", result.column_names)
        self.assertEqual(result["feat_0"].to_pylist(), [1, 0, None, 1, 0, 1, 1])

    def test_refresh_reopens_offline_and_replaces_without_duplicates(self):
        # A prior R2 writer in the same process must not redirect local writes.
        dlt.config["iceberg_catalog.iceberg_catalog_type"] = "rest"
        dlt.config["iceberg_catalog.iceberg_catalog_config"] = {"type": "rest", "uri": "https://invalid.example"}
        first = self.run_copy(rows_per_run=3)
        self.assertTrue(list((self.local_root / "warehouse").rglob("*.parquet")))
        self.assertFalse((self.root / "local%20with%20spaces").exists())
        self.assertEqual({t["table"]: t["rows"] for t in first["tables"]}, {"lamda_files": 1, "lamda_samples": 7})
        self.assertEqual(sorted(self.read_local()["row_number"].to_pylist()), list(range(7)))
        self.samples.delete(EqualTo("row_number", 3))
        self.run_copy(rows_per_run=2)
        self.assertEqual(sorted(self.read_local()["row_number"].to_pylist()), [0, 1, 2, 4, 5, 6])
        self.assertEqual(self.source.load_table("raw_lamda.lamda_samples").scan().count(), 6)
        code = (
            "from pathlib import Path; from scripts.load_lamda_local_iceberg import open_local_catalog; "
            "import sys; c=open_local_catalog(Path(sys.argv[1])); "
            "assert c.load_table('raw_lamda.lamda_samples').scan().count()==6; c.close()"
        )
        env = {k: v for k, v in os.environ.items() if not k.startswith(("R2_", "CLOUDFLARE_"))}
        result = subprocess.run([sys.executable, "-c", code, str(self.local_root)], env=env,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        for report_path in (self.local_root / "runs").glob("*.json"):
            report = json.loads(report_path.read_text())
            self.assertIn("source_snapshot_id", report["tables"][0])

    def test_source_files_are_read_one_at_a_time_with_global_limit(self):
        self.samples.append(self.data)
        original = ArrowScan.to_record_batches
        task_counts = []

        def observe(scan, tasks):
            tasks = list(tasks)
            task_counts.append(len(tasks))
            return original(scan, tasks)

        with patch.object(ArrowScan, "to_record_batches", observe):
            chunks = list(snapshot_chunks(self.samples, rows_per_run=3, limit=10))
        self.assertEqual(task_counts, [1, 1])
        self.assertEqual([chunk.num_rows for chunk in chunks], [3, 3, 3, 1])

    def test_empty_table_created_and_nonempty_table_cleared(self):
        self.create_table("lamda_empty", self.data.slice(0, 0))
        self.run_copy(tables=["lamda_empty", "lamda_samples"], rows_per_run=3)
        self.assertEqual(self.read_local("lamda_empty").num_rows, 0)
        self.samples.delete()
        self.run_copy(tables=["lamda_samples"])
        self.assertEqual(self.read_local().num_rows, 0)
        self.assertIn("feat_0", self.read_local().column_names)

    def test_selection_limits_and_missing_table_preflight(self):
        self.create_table("lamda_feature_categories", pa.table({"category": ["permissions"]}))
        self.create_table("_dlt_pipeline_state", pa.table({"state": ["internal"]}))
        self.assertEqual(len(select_tables(self.source, "raw_lamda", None)), 3)
        report = self.run_copy(tables=["lamda_samples", "lamda_samples"], limit_per_table=4, rows_per_run=3)
        self.assertEqual(len(report["tables"]), 1)
        self.assertEqual(self.read_local().num_rows, 4)
        with self.assertRaises(Exception):
            self.run_copy(tables=["lamda_samples", "lamda_missing"])
        self.assertEqual(self.read_local().num_rows, 4)
        with self.assertRaises(ValueError):
            self.run_copy(rows_per_run=0)
        with self.assertRaises(ValueError):
            self.run_copy(tables=["../lamda_samples"])

    def test_restart_after_partial_refresh(self):
        from scripts import load_lamda_local_iceberg as module
        original = module.snapshot_chunks

        def fail_after_first(*args):
            chunks = original(*args)
            yield next(chunks)
            raise RuntimeError("interrupted read")

        with patch.object(module, "snapshot_chunks", side_effect=fail_after_first):
            with self.assertRaisesRegex(RuntimeError, "interrupted read"):
                self.run_copy(tables=["lamda_samples"], rows_per_run=2)
        self.assertFalse((self.local_root / "runs").exists())
        self.run_copy(tables=["lamda_samples"], rows_per_run=3)
        self.assertEqual(sorted(self.read_local()["row_number"].to_pylist()), list(range(7)))


if __name__ == "__main__":
    unittest.main()
