"""Complete native results, calendar oracle, interface artifacts and recovery."""
import hashlib
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from easysewer import get_native_capabilities
from easysewer.model import Model
from test_context_fields_v2 import fixture, load, queries, UNITS
from test_native_v2_regulator_fields import FAMILIES, library
from test_native_v2_control_fields import observe
from test_native_v2_runner_checkpoint import reports

EVIDENCE = []


@unittest.skipUnless(get_native_capabilities()['swmm_solver'] and get_native_capabilities()['flexible_ponding'],
                     'Native solver libraries unavailable')
class NativeContextFieldTests(unittest.TestCase):
    def test_all_units_json_normalization_and_complete_results(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            for family, name, symbol in FAMILIES:
                lib, path = library(name, symbol); rows = []
                for units in UNITS:
                    for variant in ('report', 'events', 'clock', 'mixed'):
                        with self.subTest(family=family, units=units, variant=variant):
                            source = fixture(units, variant=variant); m = load(source)
                            before = queries(m)
                            m = Model.from_json_document(m.to_json_document(), strict=True)
                            self.assertEqual(queries(m), before)
                            expected = observe(self, lib, base, source)
                            actual = observe(self, lib, base, m.to_document(normalize=True).text)
                            self.assertEqual(actual, expected)
                            self.assertGreater(max(row[-1] for row in actual['history']), 0)
                            rows.append(dict(units=units, variant=variant, steps=len(actual['history']),
                                out_sha256=actual['out_sha256'], report_sha256=actual['report_sha256']))
                EVIDENCE.append(dict(kind='context-native-results', family=family, rows=rows,
                    library_sha256=hashlib.sha256(Path(path).read_bytes()).hexdigest()))

    def test_effective_calendar_matches_independent_literal_dates(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            for family, name, symbol in FAMILIES:
                lib, _ = library(name, symbol)
                clock = fixture(variant='clock')
                literal = clock + ('[OPTIONS]\nSTART_DATE 01/01/2020\nSTART_TIME 00:00\n'
                    'END_DATE 01/01/2020\nEND_TIME 00:40\nREPORT_START_DATE 01/01/2020\nREPORT_START_TIME 00:00\n')
                before = observe(self, lib, base, clock)
                first_report = reports((base/'model.rpt').read_bytes())
                actual = observe(self, lib, base, literal)
                second_report = reports((base/'model.rpt').read_bytes())
                self.assertEqual(actual['history'], before['history'])
                self.assertEqual(actual['out_sha256'], before['out_sha256'])
                # The fixed engine displays the configured date with a clock
                # modulo 24h; its execution calendar nevertheless rolls over.
                for label in (b'Starting Date ............ ', b'Ending Date .............. '):
                    self.assertEqual(first_report.count(label + b'12/31/2019'), 1)
                    self.assertEqual(second_report.count(label + b'01/01/2020'), 1)
                    first_report = first_report.replace(label + b'12/31/2019', label + b'01/01/2020')
                self.assertEqual(first_report, second_report)
                EVIDENCE.append(dict(kind='context-calendar-oracle', family=family,
                    out_sha256=before['out_sha256'], report_sha256=before['report_sha256'],
                    literal_report_sha256=actual['report_sha256'], configured_date_display_differences=2))

    def test_actual_interface_files_keep_results_and_output_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            for family, name, symbol in FAMILIES:
                lib, _ = library(name, symbol); rows = []
                for kind in ('HOTSTART', 'RUNOFF', 'OUTFLOWS'):
                    target = base/'interface.bin'
                    producer = fixture(variant='report') + f'[FILES]\nSAVE {kind} "{target}"\n'
                    expected = observe(self, lib, base, producer); raw = target.read_bytes()
                    m = load(producer, source=str(base/'model.inp')); before = queries(m)
                    m = Model.from_json_document(m.to_json_document(), strict=True)
                    self.assertEqual(queries(m), before)
                    self.assertEqual(observe(self, lib, base, m.to_document(normalize=True).text), expected)
                    self.assertEqual(target.read_bytes(), raw)
                    rows.append(dict(kind=kind, mode='SAVE', out_sha256=expected['out_sha256'],
                        file_sha256=hashlib.sha256(raw).hexdigest()))
                    if kind == 'OUTFLOWS': continue
                    consumer = fixture(variant='report') + f'[FILES]\nUSE {kind} "{target}"\n'
                    expected = observe(self, lib, base, consumer)
                    m = load(consumer, source=str(base/'model.inp')); before = queries(m)
                    m = Model.from_json_document(m.to_json_document(), strict=True)
                    self.assertEqual(queries(m), before)
                    self.assertEqual(observe(self, lib, base, m.to_document(normalize=True).text), expected)
                    self.assertEqual(target.read_bytes(), raw)
                    rows.append(dict(kind=kind, mode='USE', out_sha256=expected['out_sha256'],
                        file_sha256=hashlib.sha256(raw).hexdigest()))
                EVIDENCE.append(dict(kind='context-interface-oracles', family=family, rows=rows))

    def test_field_sources_survive_result_archive_and_moved_checkpoint(self):
        from easysewer.runtime import RunResult
        import test_native_v2_runner_checkpoint as checkpoint
        helper = checkpoint.NativeRunnerCheckpointTests()
        with patch.dict(os.environ, EASYSEWER_CHECKPOINT_PACKAGED='1'):
            for family in ('standard', 'custom'):
                with self.subTest(family=family), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory); m = load(fixture())
                    original, saved = helper.original(root, family, model=m); helper.success(original)
                    self.assertTrue(saved)
                    before = queries(original.snapshot.model())
                    original.save(root/'expected'); expected = RunResult.load(root/'expected')
                    self.assertEqual(queries(expected.snapshot.model()), before)
                    shutil.rmtree(root/'first'); (root/'original.hsf').unlink(); (root/'saved').rename(root/'moved')
                    actual = checkpoint.runner(family).resume(root/'moved'/saved[0].directory.name,
                        checkpoint.resume_config(root/'resumed'))
                    helper.equivalent(expected, actual); self.assertEqual(queries(actual.snapshot.model()), before)
                    EVIDENCE.append(dict(kind='context-fields-checkpoint', family=family,
                        queries=len(before), out_sha256=actual.output.sha256, original_workspace_removed=True))


if __name__ == '__main__': unittest.main()
