import contextlib
import gc
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pyarrow as pa
from pyiceberg.catalog import load_catalog

from scripts.load_lamda_local_iceberg import local_catalog_config, open_local_catalog, run_pipeline
from scripts.resume_lamda_local_iceberg import key_inventory, resume_samples


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        source_root = root / "source"
        source_root.mkdir()
        self.root = root / "local"
        self.root.mkdir()
        self.source = load_catalog("source", **local_catalog_config(source_root))
        self.local = open_local_catalog(self.root)
        for catalog in (self.source, self.local):
            catalog.create_namespace("raw_lamda")
        self.data = pa.table({"source_file": ["a"] * 7 + ["b"] * 4,
                              "row_number": list(range(7)) + list(range(4)),
                              "feat_0": [0, 1, None, 1, 0, 1, 0, 1, 1, 0, None]})
        self.data = self.data.select(["row_number", "source_file", "feat_0"])
        self.source_table = self.source.create_table("raw_lamda.lamda_samples", schema=self.data.schema)
        self.source_table.append(self.data)
        self.snapshot = self.source_table.current_snapshot().snapshot_id
        with contextlib.redirect_stdout(io.StringIO()):
            run_pipeline(source_catalog=self.source, local_root=self.root,
                         tables=["lamda_samples"], limit_per_table=1)
        self.target = self.local.load_table("raw_lamda.lamda_samples")
        self.target.overwrite(self.data.take(pa.array([0, 1, 3, 6, 8, 10])))

    def tearDown(self):
        self.source.close()
        self.local.close()
        gc.collect()
        self.tmp.cleanup()

    def recover(self, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()):
            return resume_samples(self.snapshot, source_catalog=self.source,
                                  local_root=self.root, rows_per_run=2, **kwargs)

    def test_recovers_holes_and_rerun_does_not_duplicate(self):
        report = self.recover()
        self.assertEqual(report["tables"][0]["recovered_rows"], 5)
        self.target.refresh()
        actual = self.target.scan().to_arrow().sort_by([("source_file", "ascending"), ("row_number", "ascending")])
        self.assertEqual(actual.select(self.data.column_names).to_pylist(), self.data.to_pylist())
        self.assertEqual(self.recover()["tables"][0]["recovered_rows"], 0)

    def test_changed_snapshot_and_invalid_local_keys_refused(self):
        self.source_table.append(self.data.slice(0, 1))
        with self.assertRaisesRegex(ValueError, "snapshot changed"):
            self.recover()
        self.assertEqual(self.target.scan().count(), 6)

    def test_duplicate_and_foreign_local_keys_refused(self):
        self.target.append(self.data.slice(0, 1))
        with self.assertRaisesRegex(ValueError, "duplicate"):
            self.recover()
        self.target.overwrite(pa.table({"source_file": ["absent"], "row_number": [0], "feat_0": [1]}))
        with self.assertRaisesRegex(ValueError, "absent from"):
            self.recover()

    def test_recovery_can_restart_after_committed_chunk(self):
        from scripts import resume_lamda_local_iceberg as module
        original = module.snapshot_chunks

        def interrupted(*args, **kwargs):
            chunks = original(*args, **kwargs)
            yield next(chunks)
            raise RuntimeError("interrupted")

        with patch.object(module, "snapshot_chunks", interrupted):
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                self.recover()
        self.target.refresh()
        self.assertEqual(self.target.scan().count(), 8)
        self.recover()
        self.target.refresh()
        self.assertEqual(key_inventory(self.target), key_inventory(self.source_table))


if __name__ == "__main__":
    unittest.main()
