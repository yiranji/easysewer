"""Resource facts across both native engines and portable checkpoint recovery."""
from datetime import date, time
import hashlib
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from easysewer import get_native_capabilities
from easysewer.model.resources import CURVE_KINDS
from test_resource_fields_v2 import load, queries, P, T, UNITS
from test_options_v2 import network
import test_native_v2_node_fields as nodes
from test_native_v2_regulator_fields import FAMILIES, library

EVIDENCE = []


def fixture(kind='series', units='CFS', day=1):
    model = network()
    model.reinterpret_units(units)
    model.update_options(start_date=date(2020, 1, day), end_date=date(2020, 1, day),
                         start_time=time(12), end_time=time(12, 10))
    text = model.to_document().text
    if kind == 'series':
        text = text.replace('O 9 FREE', 'O 9 TIMESERIES T')
        text += f'[TIMESERIES]\nT 0 9.5 .04 9.8\nT 01/{day:02d}/2020 12:05 9.6 12:10 9.7\n'
    else:
        text += f'[DWF]\nJ FLOW .3 P\n[PATTERNS]\nP {kind} .8\n'
    text += '[CURVES]\n' + ''.join(f'C{i} {k} 0 0 1 1 2 0\n' for i, k in enumerate(CURVE_KINDS))
    if kind == 'series':
        text += '[PATTERNS]\nP DAILY 1\n'
    return text


@unittest.skipUnless(get_native_capabilities()['swmm_solver'] and get_native_capabilities()['flexible_ponding'],
                     'Standard/custom native solvers unavailable')
class NativeResourceFieldTests(unittest.TestCase):
    observe = nodes.NativeNodeFieldTests.observe

    def test_defaults_calendar_and_grouped_sources_preserve_full_native_results(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            for family, name, symbol in FAMILIES:
                lib, path = library(name, symbol)
                rows = []
                for units in UNITS:
                    for kind in ('MONTHLY', 'DAILY', 'HOURLY', 'WEEKEND', 'series'):
                        for day in (1, 4):
                            with self.subTest(family=family, units=units, kind=kind, day=day):
                                source = fixture(kind, units, day)
                                model = load(source)
                                if kind == 'series':
                                    # Replace relative/carry syntax with the queried effective calendar points.
                                    model.timeseries.update('T', points=model.inspect_field(T, 'points').semantics.effective.value)
                                else:
                                    # Materialize the queried native padding into explicit input factors.
                                    model.patterns.update('P', factors=model.inspect_field(P, 'factors').semantics.effective.value)
                                expected = self.observe(lib, base, source, full=True)
                                actual = self.observe(lib, base, model.to_document(normalize=True).text, full=True)
                                self.assertEqual(actual, expected)
                                rows.append(dict(units=units, variant=kind, day=day, steps=len(actual['history']), out_sha256=actual['out_sha256']))
                EVIDENCE.append(dict(kind='resource-native-results', family=family, cases=len(rows), rows=rows,
                                     library_sha256=hashlib.sha256(Path(path).read_bytes()).hexdigest()))

    def test_fields_survive_archive_and_relocated_checkpoint(self):
        from easysewer.runtime import RunResult
        import test_native_v2_runner_checkpoint as checkpoint
        helper = checkpoint.NativeRunnerCheckpointTests()
        with patch.dict(os.environ, EASYSEWER_CHECKPOINT_PACKAGED='1'):
            for family in ('standard', 'custom'):
                with self.subTest(family=family), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    model = load(fixture())
                    model.update_options(allow_ponding=True)
                    before = queries(model)
                    original, saved = helper.original(root, family, model=model)
                    helper.success(original)
                    self.assertTrue(saved)
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
                    EVIDENCE.append(dict(kind='resource-fields-checkpoint', family=family, queries=len(before),
                                         out_sha256=actual.output.sha256, original_workspace_removed=True))


if __name__ == '__main__':
    unittest.main()
