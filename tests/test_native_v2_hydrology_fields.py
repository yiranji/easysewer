"""Hydrology field facts written back to input and compared with both engines."""
import hashlib
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from easysewer import get_native_capabilities
from test_hydrology_fields_v2 import fixture, load, queries, CATCHMENT, INFILTRATION, UNITS
import test_native_v2_node_fields as nodes
from test_native_v2_regulator_fields import FAMILIES, library

EVIDENCE = []


@unittest.skipUnless(get_native_capabilities()['swmm_solver'] and get_native_capabilities()['flexible_ponding'],
                     'Standard/custom native solvers unavailable')
class NativeHydrologyFieldTests(unittest.TestCase):
    observe = nodes.NativeNodeFieldTests.observe

    def test_effective_parameters_preserve_full_native_results(self):
        cases = [(m, u, r, f, 30) for m in INFILTRATION for u in UNITS
                 for r in ('OUTLET', 'PERVIOUS', 'IMPERVIOUS') for f in ('INTENSITY', 'VOLUME', 'CUMULATIVE')]
        cases += [('HORTON', u, 'PERVIOUS', 'INTENSITY', impervious) for u in UNITS for impervious in (0, 100, 120)]
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            for family, name, symbol in FAMILIES:
                lib, path = library(name, symbol)
                rows = []
                for method, units, routing, form, impervious in cases:
                    with self.subTest(family=family, method=method, units=units, routing=routing, form=form, impervious=impervious):
                        source = fixture(method, units, routing, form, impervious)
                        model = load(source)
                        values = {field: model.inspect_field(CATCHMENT, field).semantics.effective for field in ('impervious_percent', 'subareas', 'infiltration')}
                        self.assertTrue(all(value.status == 'known' for value in values.values()))
                        model.subcatchments.update('S', **{field: value.value for field, value in values.items()})
                        expected = self.observe(lib, base, source, full=True)
                        actual = self.observe(lib, base, model.to_document(normalize=True).text, full=True)
                        self.assertEqual(actual, expected)
                        rows.append(dict(method=method, units=units, routing=routing, form=form, impervious=impervious,
                                         steps=len(actual['history']), out_sha256=actual['out_sha256']))
                EVIDENCE.append(dict(kind='hydrology-field-native-results', family=family, cases=len(rows), rows=rows,
                                     library_sha256=hashlib.sha256(Path(path).read_bytes()).hexdigest()))

    def test_five_methods_survive_archive_and_relocated_checkpoint(self):
        from easysewer.runtime import RunResult
        import test_native_v2_runner_checkpoint as checkpoint
        helper = checkpoint.NativeRunnerCheckpointTests()
        with patch.dict(os.environ, EASYSEWER_CHECKPOINT_PACKAGED='1'):
            for family in ('standard', 'custom'):
                for method in INFILTRATION:
                    with self.subTest(family=family, method=method), tempfile.TemporaryDirectory() as directory:
                        root = Path(directory)
                        model = load(fixture(method))
                        before = queries(model)
                        original, saved = helper.original(root, family, model=model)
                        helper.success(original); self.assertTrue(saved)
                        self.assertEqual(queries(original.snapshot.model()), before)
                        original.save(root/'expected')
                        expected = RunResult.load(root/'expected')
                        self.assertEqual(queries(expected.snapshot.model()), before)
                        shutil.rmtree(root/'first')
                        (root/'original.hsf').unlink()
                        (root/'saved').rename(root/'moved')
                        actual = checkpoint.runner(family).resume(root/'moved'/saved[0].directory.name, checkpoint.resume_config(root/'resumed'))
                        helper.equivalent(expected, actual)
                        self.assertEqual(queries(actual.snapshot.model()), before)
                        EVIDENCE.append(dict(kind='hydrology-fields-checkpoint', family=family, method=method, queries=len(before),
                                             out_sha256=actual.output.sha256, original_workspace_removed=True))


if __name__ == '__main__':
    unittest.main()
