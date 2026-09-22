"""Offline integration tests for the Loghub Android dlt pipeline."""

import gc
import io
from pathlib import Path
import tempfile
import unittest

from scripts.load_loghub_android_local_iceberg import (
    android_rows,
    open_local_catalog,
    parse_android_line,
    run_pipeline,
)


SAMPLE = (
    b"03-17 16:13:38.811  1702  2395 D WindowManager: printFreezingDisplayLogs\n"
    b"03-17 16:13:38.819  1702  2395 I am_proc_start: [0,1234,1000]\n"
    b"unparsed but retained\n"
)


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def fixture_opener(request):
    if request.full_url != "https://example.test/Android_2k.log":
        raise AssertionError(request.full_url)
    return _Response(SAMPLE)


class LoghubAndroidTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "android local"

    def tearDown(self):
        gc.collect()
        self.tmp.cleanup()

    def test_parser_retains_raw_lines_and_nullable_parse_failures(self):
        row = parse_android_line("unparsed but retained", 7, "https://example.test/log")
        self.assertEqual(row["line_number"], 7)
        self.assertEqual(row["raw_line"], "unparsed but retained")
        self.assertIsNone(row["pid"])
        rows = list(android_rows("https://example.test/Android_2k.log", 2, opener=fixture_opener))
        self.assertEqual([row["pid"] for row in rows], [1702, 1702])
        self.assertEqual(rows[0]["component"], "WindowManager")

    def test_pipeline_loads_and_replaces_local_iceberg_table(self):
        kwargs = {
            "source_url": "https://example.test/Android_2k.log",
            "local_root": self.root,
            "opener": fixture_opener,
        }
        first = run_pipeline(**kwargs)
        self.assertEqual(first["rows"], 3)
        second = run_pipeline(**kwargs, limit=2)
        self.assertEqual(second["rows"], 2)
        catalog = open_local_catalog(self.root)
        try:
            data = catalog.load_table("raw_loghub.android_logs").scan().to_arrow()
        finally:
            catalog.close()
        self.assertEqual(data.num_rows, 2)
        self.assertEqual(data["line_number"].to_pylist(), [1, 2])
        self.assertTrue(list((self.root / "warehouse").rglob("*.parquet")))
        self.assertEqual(len(list((self.root / "runs").glob("*.json"))), 2)

    def test_rejects_invalid_options_before_loading(self):
        with self.assertRaises(ValueError):
            run_pipeline(local_root=self.root, limit=0, opener=fixture_opener)
        with self.assertRaises(ValueError):
            run_pipeline(local_root=self.root, dataset_name="Bad-Name", opener=fixture_opener)
        with self.assertRaises(ValueError):
            run_pipeline(local_root=self.root, source_url="http://example.test/log", opener=fixture_opener)


if __name__ == "__main__":
    unittest.main()
