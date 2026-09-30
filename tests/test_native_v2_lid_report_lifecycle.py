"""Partial LID initialization and native owner guards, using fault builds."""
import ctypes as C
import os
from pathlib import Path
import tempfile
import unittest

from test_native_v2_lid_report_io import cycle_fixture, fixture, load, message, run
from test_native_v2_standard_io import handles

EVIDENCE = []


class LidReportLifecycleTests(unittest.TestCase):
    def test_all_model_allocations_and_report_opens_release_partial_projects(self):
        with tempfile.TemporaryDirectory() as directory:
            for family in ('standard', 'custom'):
                lib = load(family, faults=True)
                root = Path(directory) / family
                source = cycle_fixture()
                lib.es_test_lid_fault(0, 0)
                self.assertFalse(any(run(lib, root / 'baseline', source)[0]))
                counts = {k: lib.es_test_lid_calls(k) for k in (7, 8)}
                self.assertEqual(counts, {7: 8, 8: 2})
                before = handles()
                outcomes = []
                for kind, count in counts.items():
                    for hit in range(1, count + 1):
                        with self.subTest(family=family, kind=kind, hit=hit):
                            lib.es_test_lid_fault(kind, hit)
                            codes, (error, _) = run(lib, root / f'{kind}-{hit}', source)
                            self.assertEqual(lib.es_test_lid_fault_fired(), 1)
                            expected = 101 if kind == 7 and hit <= 3 else 200
                            self.assertEqual(codes, [expected, 0])
                            self.assertEqual(error, expected)
                            self.assertEqual(lib.swmm_close(), 0)
                            lib.es_test_lid_fault(0, 0)
                            self.assertFalse(any(run(lib, root / 'recovery', source)[0]))
                            self.assertEqual(handles(), before)
                            outcomes.append(dict(kind=kind, hit=hit, codes=codes))
                EVIDENCE.append(dict(family=family, kind='partial-allocation-open', outcomes=outcomes))

    def test_real_open_failure_and_later_input_errors_cleanup_previous_report(self):
        with tempfile.TemporaryDirectory() as directory:
            for family in ('standard', 'custom'):
                lib = load(family)
                root = Path(directory) / family
                root.mkdir()
                bad_path = root / 'absent-parent' / 'detail.txt'
                source = cycle_fixture()
                cases = [fixture(detail=str(root)), fixture(detail=str(bad_path)),
                         source.replace('"lid second.txt"', '"' + str(bad_path) + '"'),
                         source + '[LID_USAGE]\nS MISSING 1 10 1 0 0 0\n']
                self.assertFalse(any(run(lib, root / 'baseline', fixture())[0]))
                before = handles()
                for i, text in enumerate(cases):
                    codes, (error, _) = run(lib, root / str(i), text)
                    self.assertEqual(codes, [200, 0])
                    self.assertEqual(error, 200)
                    self.assertEqual(lib.swmm_close(), 0)
                    self.assertFalse(any(run(lib, root / 'recovery', fixture())[0]))
                    self.assertEqual(handles(), before)
                EVIDENCE.append(dict(family=family, kind='real-partial-open', cases=len(cases)))

    def test_run_and_reopen_do_not_replace_live_report_owners(self):
        with tempfile.TemporaryDirectory() as directory:
            for family in ('standard', 'custom'):
                lib = load(family, faults=True)
                lib.swmm_run.argtypes = [C.c_char_p] * 3
                root = Path(directory) / family
                root.mkdir()
                inp, rpt, out = [root / ('model' + s) for s in ('.inp', '.rpt', '.out')]
                source = fixture(detail=str(root / 'detail.txt'))
                inp.write_text(source, encoding='utf-8')
                before = handles()
                for action in ('open', 'run'):
                    lib.es_test_lid_fault(0, 0)
                    self.assertEqual(lib.swmm_open(os.fsencode(inp), os.fsencode(rpt), os.fsencode(out)), 0)
                    self.assertEqual(lib.swmm_start(1), 0)
                    content = (root / 'detail.txt').read_bytes()
                    self.assertEqual(getattr(lib, 'swmm_' + action)(
                        os.fsencode(inp), os.fsencode(root / 'other.rpt'), os.fsencode(root / 'other.out')), 503)
                    self.assertFalse((root / 'other.rpt').exists())
                    self.assertEqual((root / 'detail.txt').read_bytes(), content)
                    self.assertEqual(lib.swmm_close(), 0)
                    self.assertEqual(lib.swmm_close(), 0)
                    self.assertEqual(handles(), before)
                lib.es_test_lid_fault(1, 1)
                self.assertEqual(lib.swmm_run(os.fsencode(inp), os.fsencode(rpt), os.fsencode(out)), 306)
                self.assertEqual(message(lib)[0], 306)
                self.assertEqual(lib.swmm_close(), 0)
                self.assertEqual(handles(), before)
                lib.es_test_lid_fault(0, 0)
                self.assertFalse(any(run(lib, root / 'recovery', fixture())[0]))
                EVIDENCE.append(dict(family=family, kind='owner-guards-and-run', cases=3))


if __name__ == '__main__':
    unittest.main()
