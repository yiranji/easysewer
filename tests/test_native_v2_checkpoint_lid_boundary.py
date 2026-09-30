"""Existing checkpoint boundary on the checked-LID baseline; no LID restore yet."""
import ctypes as c
import os
from pathlib import Path
import tempfile
import unittest

from test_native_v2_checkpoint_snow import Engine
from test_native_v2_lid_report_io import fixture


@unittest.skipUnless(os.environ.get('EASYSEWER_CHECKPOINT_STANDARD') and
                     os.environ.get('EASYSEWER_CHECKPOINT_CUSTOM'),
                     'Requires internal checkpoint test libraries')
class NativeCheckpointLidBoundaryTests(unittest.TestCase):
    def test_successful_steps_keep_existing_bundle_capturable_with_detail_reports(self):
        for family in ('standard', 'custom'):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                e = Engine(os.environ['EASYSEWER_CHECKPOINT_' + family.upper()], root,
                           fixture(detail=str(root / 'detail.txt')))
                try:
                    self.assertEqual(e.lib.swmm_getEasySewerLidReportIO(), 1)
                    for _ in range(1000):
                        raw = e.dump()
                        self.assertEqual(e.restore(raw), 0)
                        self.assertTrue(e.committed)
                        self.assertEqual(e.cleanup, 0)
                        self.assertEqual(e.dump(), raw)
                        if not e.step()[0]:
                            break
                    else:
                        self.fail('LID fixture did not finish')
                finally:
                    e.close()
                self.assertIn(b'LID Unit:', (root / 'detail.txt').read_bytes())
                self.assertGreater(e.paths[2].stat().st_size, 0)

    def reject_failed_open_or_start(self, target, expected_open, expected_start):
        for family in ('standard', 'custom'):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                lib = c.CDLL(os.environ['EASYSEWER_CHECKPOINT_' + family.upper()])
                lib.swmm_open.argtypes = [c.c_char_p] * 3
                lib.swmm_start.argtypes = [c.c_int]
                save = lib.es_test_snow_save
                save.argtypes = [c.c_void_p, c.c_size_t, c.POINTER(c.c_size_t), c.c_int]
                save.restype = c.c_int
                inp, rpt, out = [root / ('run' + ext) for ext in ('.inp', '.rpt', '.out')]
                inp.write_text(fixture(detail=target(root)), encoding='utf-8')
                try:
                    self.assertEqual(lib.swmm_open(*(os.fsencode(p) for p in (inp, rpt, out))), expected_open)
                    if expected_start is not None:
                        self.assertEqual(lib.swmm_start(1), expected_start)
                    size = c.c_size_t()
                    self.assertEqual(save(None, 0, c.byref(size), 1), 5)  # ES_CK_STATE
                    self.assertEqual(size.value, 0)
                finally:
                    lib.swmm_close()
                self.assertEqual(lib.swmm_close(), 0)
                # A failed project must not prevent capturing a fresh valid owner.
                e = Engine(os.environ['EASYSEWER_CHECKPOINT_' + family.upper()], root,
                           fixture(detail=str(root / 'recovered.txt')))
                try:
                    self.assertTrue(e.dump())
                    self.assertGreater(e.step()[0], 0)
                finally:
                    e.close()

    def test_lid_open_failure_rejects_capture_and_next_owner_recovers(self):
        self.reject_failed_open_or_start(lambda root: str(root / 'absent' / 'report.txt'), 200, None)

    @unittest.skipIf(os.name == 'nt', 'POSIX full device')
    def test_lid_write_failure_rejects_capture_and_next_owner_recovers(self):
        self.reject_failed_open_or_start(lambda root: '/dev/full', 0, 306)
