"""Hydraulic EVENTS are compared with unmodified native input, not a GUI oracle."""

from datetime import timedelta
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

from easysewer import get_native_capabilities
from easysewer.utils import probe_library_path
from test_events_v2 import load, period
from test_native_v2_standard_io import direct_library, execute
from test_options_v2 import network
from test_scenario_v2 import portable


EVIDENCE = []
CHECKPOINT_EVIDENCE = []


@unittest.skipUnless(get_native_capabilities()['swmm_solver'] and get_native_capabilities()['flexible_ponding'],
    'Standard/custom native solvers unavailable')
class NativeEventsTests(unittest.TestCase):
    def test_typed_schedule_survives_moved_checkpoint_and_fresh_parent(self):
        from easysewer.runtime import RunResult
        import test_native_v2_runner_checkpoint as checkpoints

        child = '''import sys
from pathlib import Path
package, tests, family, checkpoint, output, archive = sys.argv[1:]
sys.path[:0] = [package, tests]
import easysewer
assert Path(easysewer.__file__).is_relative_to(Path(package))
from test_native_v2_runner_checkpoint import runner, resume_config
value = runner(family).resume(checkpoint, resume_config(Path(output)))
assert value.succeeded, (value.failure, value.diagnostics)
value.save(archive)
'''
        import easysewer
        package = str(Path(easysewer.__file__).resolve().parent.parent)
        tests = str(Path(__file__).resolve().parent)
        helper = checkpoints.NativeRunnerCheckpointTests()
        from unittest.mock import patch
        with patch.dict(os.environ, EASYSEWER_CHECKPOINT_PACKAGED='1'):
            for family in ('standard', 'custom'):
                with self.subTest(family=family), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    model = network()
                    model.update_events(periods=(period(60, 180), period(300, 540)))
                    model.update_options(rule_step=timedelta(seconds=17))
                    original, saved = helper.original(root, family, model=model)
                    helper.success(original)
                    self.assertTrue(saved)
                    original.save(root / 'expected')
                    expected = RunResult.load(root / 'expected')
                    shutil.rmtree(root / 'first')
                    (root / 'original.hsf').unlink()
                    (root / 'saved').rename(root / 'moved')
                    process = subprocess.run([sys.executable, '-I', '-B', '-c', child,
                        package, tests, family, str(root / 'moved' / saved[0].directory.name),
                        str(root / 'resumed'), str(root / 'result')],
                        capture_output=True, text=True, timeout=90,
                        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                    self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
                    actual = RunResult.load(root / 'result')
                    helper.equivalent(expected, actual)
                    self.assertEqual(len(actual.continuations), 1)
                    CHECKPOINT_EVIDENCE.append(dict(family=family,
                        output_sha256=actual.output.sha256, checkpoints=len(saved),
                        fresh_parent=True, original_workspace_removed=True))

    def solve(self, lib, root, source):
        root.mkdir()
        self.assertTrue(all(code == 0 for code in execute(lib, root, source)))
        report = (root / 'model.rpt').read_bytes()
        report = re.sub(rb'(?m)^[ \t]*(?:Analysis (?:begun|ended)|Total elapsed time)[^\r\n]*', b'', report)
        return (root / 'model.out').read_bytes(), report

    def test_source_roundtrip_matches_all_output_bytes_and_normalized_reports(self):
        cases = {
            'single': '01/01/2020 00:01 01/01/2020 00:03',
            'unsorted-overlap': '01/01/2020 00:04 01/01/2020 00:06\n01/01/2020 00:01 01/01/2020 00:05',
            'nested': '01/01/2020 00:01 01/01/2020 00:08\n01/01/2020 00:02 01/01/2020 00:03',
            'ties': '01/01/2020 00:01 01/01/2020 00:08\n01/01/2020 00:01 01/01/2020 00:03\n01/01/2020 00:01 01/01/2020 00:08',
            'terminal': '01/01/2020 00:01 01/01/2020 00:02',
            'outside': '12/31/2019 22:00 12/31/2019 23:00\n01/02/2020 01:00 01/02/2020 02:00',
            'next-day': '12/31/2019 24:01 12/31/2019 24:03',
            'fractional': '01/01/2020 .0085 01/01/2020 .0335',
            'truncated-clock': '01/01/2020 00:01:00.5 01/01/2020 00:03:00.8 ignored',
        }
        model = network()
        model.update_options(report_step=timedelta(seconds=15), allow_ponding=True)
        base = model.to_document().text + '[DWF]\nJ FLOW 1.0\n[REPORT]\nNODES ALL\nLINKS ALL\n'
        for family, name, symbol in (
            ('standard', 'swmm5', 'swmm_getEasySewerStandardFixes'),
            ('custom', 'flexible_ponding', 'swmm_getEasySewerNativeIOFixes'),
        ):
            lib, library = direct_library(probe_library_path(name), revision_symbol=symbol)
            for case, rows in cases.items():
                with self.subTest(family=family, case=case), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    original = base + '[EVENTS]\n' + rows + '\n'
                    parsed = load(original)
                    rebuilt = portable(parsed).to_document().text
                    expected = self.solve(lib, root / 'original', original)
                    actual = self.solve(lib, root / 'rebuilt', rebuilt)
                    self.assertEqual(actual, expected)
                    EVIDENCE.append(dict(family=family, case=case,
                        library_sha256=hashlib.sha256(library.read_bytes()).hexdigest(),
                        out_sha256=hashlib.sha256(actual[0]).hexdigest(),
                        report_sha256=hashlib.sha256(actual[1]).hexdigest()))
            with self.subTest(family=family, case='edited'), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                unrestricted = self.solve(lib, root / 'unrestricted', base)
                edited = load(base)
                edited.update_events(periods=(period(60, 180),))
                restricted = self.solve(lib, root / 'restricted', edited.to_document().text)
                self.assertNotEqual(unrestricted[0], restricted[0])
                edited.update_events(periods=())
                self.assertEqual(self.solve(lib, root / 'cleared', edited.to_document().text), unrestricted)


if __name__ == '__main__':
    unittest.main()
