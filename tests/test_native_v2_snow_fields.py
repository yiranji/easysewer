"""Snow defaults/caps and monthly assignments against both packaged engines."""
import hashlib
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from easysewer import get_native_capabilities
from test_snow_fields_v2 import fixture, load, queries, SNOW, UNITS
import test_native_v2_node_fields as nodes
from test_native_v2_regulator_fields import FAMILIES, library

EVIDENCE = []


@unittest.skipUnless(get_native_capabilities()['swmm_solver'] and get_native_capabilities()['flexible_ponding'],
                     'Standard/custom native solvers unavailable')
class NativeSnowFieldTests(unittest.TestCase):
    observe = nodes.NativeNodeFieldTests.observe

    def test_effective_snow_defaults_and_caps_preserve_complete_results(self):
        cases = [(u, missing, mode, month) for u in UNITS for missing in (0, 1, 2, 4)
                 for mode in ('active', 'none', 'inactive') for month in (1, 2)]
        cases += [(u, 7, 'active', month) for u in UNITS for month in (1, 2)]
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            for family, name, symbol in FAMILIES:
                lib, path = library(name, symbol)
                rows = []
                for units, missing, mode, month in cases:
                    with self.subTest(family=family, units=units, missing=missing, mode=mode, month=month):
                        source = fixture(units, missing, mode, month)
                        model = load(source)
                        values = {name: model.inspect_field(SNOW, name).semantics.effective for name in ('plowable', 'impervious', 'pervious', 'removal')}
                        self.assertTrue(all(value.status == 'known' for value in values.values()))
                        model.snowpacks.update('Snow', **{name: value.value for name, value in values.items()})
                        expected = self.observe(lib, base, source, full=True)
                        actual = self.observe(lib, base, model.to_document(normalize=True).text, full=True)
                        self.assertEqual(actual, expected)
                        rows.append(dict(units=units, missing=missing, mode=mode, month=month, steps=len(actual['history']), out_sha256=actual['out_sha256']))
                EVIDENCE.append(dict(kind='snow-field-native-results', family=family, cases=len(rows), rows=rows,
                                     library_sha256=hashlib.sha256(Path(path).read_bytes()).hexdigest()))

    def test_snow_and_adjustment_facts_survive_relocated_checkpoint(self):
        from easysewer.runtime import RunResult
        import test_native_v2_runner_checkpoint as checkpoint
        helper = checkpoint.NativeRunnerCheckpointTests()
        with patch.dict(os.environ, EASYSEWER_CHECKPOINT_PACKAGED='1'):
            for family in ('standard', 'custom'):
                for missing, mode, month in ((0, 'active', 1), (7, 'active', 2), (0, 'inactive', 2)):
                    with self.subTest(family=family, missing=missing, mode=mode), tempfile.TemporaryDirectory() as directory:
                        root = Path(directory)
                        model = load(fixture(missing=missing, mode=mode, month=month))
                        before = queries(model)
                        original, saved = helper.original(root, family, model=model)
                        helper.success(original); self.assertTrue(saved)
                        self.assertEqual(queries(original.snapshot.model()), before)
                        original.save(root/'expected'); expected = RunResult.load(root/'expected')
                        self.assertEqual(queries(expected.snapshot.model()), before)
                        shutil.rmtree(root/'first'); (root/'original.hsf').unlink(); (root/'saved').rename(root/'moved')
                        actual = checkpoint.runner(family).resume(root/'moved'/saved[0].directory.name, checkpoint.resume_config(root/'resumed'))
                        helper.equivalent(expected, actual)
                        self.assertEqual(queries(actual.snapshot.model()), before)
                        EVIDENCE.append(dict(kind='snow-fields-checkpoint', family=family, missing=missing, mode=mode, month=month,
                                             queries=len(before), out_sha256=actual.output.sha256, original_workspace_removed=True))


if __name__ == '__main__':
    unittest.main()
