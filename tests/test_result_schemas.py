"""Public structural schemas accept actual codecs and reject malformed records.

Requires jsonschema for this development check; the runtime has no new dependency.
"""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from easysewer.runtime import RunResult
from easysewer.runtime._result_codec import Codec
from easysewer.runtime.archive import _Blobs
from test_result_archive_v2 import failure_result

try:
    from jsonschema import Draft202012Validator
except ImportError:
    Draft202012Validator = None

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipIf(Draft202012Validator is None, 'Development schema validator is unavailable')
class ResultSchemaTests(unittest.TestCase):
    def validator(self, version):
        schema = json.loads((ROOT/'docs'/f'result-archive-{version}.schema.json').read_bytes())
        Draft202012Validator.check_schema(schema)
        return Draft202012Validator(schema)

    def archive(self, root, version):
        root.mkdir(); (root/'blobs').mkdir()
        blobs = _Blobs(root/'blobs', 8*1024**3)
        result = Codec(blobs, result_version=version).encode(failure_result())
        envelope = dict(kind='easysewer:run-result', schema_version=version, result=result,
                        blobs=[dict(sha256=k, size=v) for k, v in sorted(blobs.inventory.items())])
        (root/'result.json').write_text(json.dumps(envelope), encoding='utf-8')
        return envelope

    def test_all_ten_codecs_and_real_loader_agree_on_valid_archives(self):
        with tempfile.TemporaryDirectory() as tmp:
            for minor in range(10):
                version = f'1.{minor}'; root = Path(tmp)/version
                with self.subTest(version=version):
                    envelope = self.archive(root, version)
                    self.validator(version).validate(envelope)
                    self.assertEqual(RunResult.load(root), failure_result())

    def test_malformed_envelopes_fail_schema_and_loader(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)/'archive'; original = self.archive(root, '1.9')
            validator = self.validator('1.9')
            variants = []
            def mutate(label, apply):
                data = deepcopy(original); apply(data); variants.append((label, data))
            mutate('unknown envelope key', lambda d: d.update(extra=True))
            mutate('unknown version', lambda d: d.update(schema_version='2.0'))
            mutate('missing status', lambda d: d['result']['fields'].pop('status'))
            mutate('unknown result field', lambda d: d['result']['fields'].update(extra=1))
            mutate('unknown status', lambda d: d['result']['fields'].update(status='success'))
            mutate('integer completion flag', lambda d: d['result']['fields'].update(native_completed=1))
            mutate('wrong diagnostic type', lambda d: d['result']['fields'].update(diagnostics=None))
            mutate('raw artifact array', lambda d: d['result']['fields'].update(artifacts=[]))
            mutate('wrong report type', lambda d: d['result']['fields'].update(failure_report='text'))
            mutate('bad inventory hash', lambda d: d['blobs'][0].update(sha256='g'*64))
            mutate('negative size', lambda d: d['blobs'][0].update(size=-1))
            mutate('boolean size', lambda d: d['blobs'][0].update(size=True))
            mutate('duplicate inventory', lambda d: d['blobs'].append(deepcopy(d['blobs'][0])))
            for label, data in variants:
                with self.subTest(label=label):
                    self.assertFalse(validator.is_valid(data))
                    (root/'result.json').write_text(json.dumps(data), encoding='utf-8')
                    with self.assertRaises((ValueError, TypeError)):
                        RunResult.load(root)

    def test_versioned_fields_cannot_be_silently_downgraded(self):
        with tempfile.TemporaryDirectory() as tmp:
            original = self.archive(Path(tmp)/'archive', '1.9')
            for minor in (0, 1, 2, 3, 4, 5, 6, 7):
                data = deepcopy(original); data['schema_version'] = f'1.{minor}'
                with self.subTest(version=data['schema_version']):
                    self.assertFalse(self.validator(data['schema_version']).is_valid(data))

    def test_schema_pass_does_not_replace_integrity_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)/'archive'; data = self.archive(root, '1.9')
            self.validator('1.9').validate(data)
            blob = root/'blobs'/data['blobs'][0]['sha256']
            blob.write_bytes(b'changed bytes')
            self.validator('1.9').validate(data)
            with self.assertRaises(ValueError):
                RunResult.load(root)


if __name__ == '__main__':
    unittest.main()
