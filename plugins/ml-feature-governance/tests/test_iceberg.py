import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('featurectl', Path(__file__).resolve().parents[1] / 'scripts/featurectl.py')
ctl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ctl)

class IcebergTests(unittest.TestCase):
    def check(self, value, field=True):
        errors = []
        ctl.validate_iceberg_reference(value, 'test', errors, field)
        return errors

    def test_table_scoped_field_and_rename(self):
        binding = {'format': 'iceberg', 'table': 'prod.ml.features', 'iceberg_field_id': 3, 'column': 'old'}
        self.assertEqual(self.check(binding), [])
        binding['column'] = 'renamed'
        self.assertEqual(self.check(binding), [])
        binding.pop('table')
        self.assertTrue(self.check(binding))

    def test_invalid_ids(self):
        for value in [True, 0, -1, '3', None, 1.5]:
            with self.subTest(value=value):
                self.assertTrue(self.check({'format': 'iceberg', 'table': 't', 'iceberg_field_id': value}))

    def test_snapshot_pin(self):
        self.assertTrue(self.check({'format': 'iceberg', 'table': 't'}, False))
        self.assertEqual(self.check({'format': 'iceberg', 'table': 't', 'iceberg_snapshot_id': 918273645}, False), [])

    def test_documentation_and_metadata(self):
        b = {'format': 'iceberg', 'table': 't', 'iceberg_field_id': 3, 'doc': 'Rolling login count.'}
        self.assertEqual(self.check(b), [])
        for doc in [{'ml': 'x'}, '{"normalization":"standard_scaler"}', '']:
            self.assertTrue(self.check(dict(b, doc=doc)))
        self.assertTrue(self.check(dict(b, governance={'pii': False})))

    def test_other_storage_is_optional(self):
        self.assertEqual(self.check({'format': 'parquet'}), [])
        self.assertTrue(self.check({'iceberg_field_id': 3}))

    def test_inline_normalization_rejected(self):
        f = {'id': 'device.raw', 'version': 1, 'semantic_type': 'continuous', 'source': 'events', 'column': 'bytes', 'output': {'type': 'int64', 'nullable': True}}
        errors=[]
        ctl.validate_feature(Path('feature.yaml'), f, errors)
        self.assertEqual(errors, [])
        f['ml']={'normalization': 'standard_scaler'}
        ctl.validate_feature(Path('feature.yaml'), f, errors)
        self.assertTrue(any('separate versioned fitted-transform' in e for e in errors))

if __name__ == '__main__':
    unittest.main()
