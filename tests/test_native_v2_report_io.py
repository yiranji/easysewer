"""Native RPT boundaries, lifecycle and visible bytes, without Python preflight."""

import ctypes as C
import os
from pathlib import Path
import tempfile
import unittest

from easysewer.utils import probe_library_path
from test_native_v2_routing_io import SOURCE
from test_native_v2_standard_io import direct_library, handles


REPORT_SOURCE = SOURCE + '''[REPORT]
INPUT YES
CONTROLS YES
NODES ALL
LINKS ALL
[CONTROLS]
RULE Switch
IF SIMULATION TIME >= 0:02
THEN CONDUIT C STATUS = CLOSED
ELSE CONDUIT C STATUS = OPEN
'''


def library(family):
    path = os.environ.get('EASYSEWER_REPORT_TEST_' + family.upper())
    path = path or probe_library_path('swmm5' if family == 'standard' else 'flexible_ponding')
    if not path:
        raise unittest.SkipTest('Native library unavailable')
    lib, _ = direct_library(path, revision_symbol='swmm_getEasySewerStandardFixes'
                            if family == 'standard' else 'swmm_getEasySewerNativeIOFixes')
    if not hasattr(lib, 'swmm_getEasySewerReportIO') or lib.swmm_getEasySewerReportIO() != 1:
        raise unittest.SkipTest('Report I/O revision 1 unavailable')
    lib.swmm_writeLine.argtypes = [C.c_char_p]
    lib.swmm_writeLine.restype = None
    lib.swmm_getError.argtypes = [C.c_char_p, C.c_int]
    lib.swmm_run.argtypes = [C.c_char_p] * 3
    return lib


class ReportIOChecks:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.inp, self.rpt, self.out = (self.root / ('model' + ext) for ext in ('.inp', '.rpt', '.out'))
        self.inp.write_text(REPORT_SOURCE)
        self.addCleanup(self.lib.swmm_close)

    def open(self, report=None, output=None):
        return self.lib.swmm_open(os.fsencode(self.inp), os.fsencode(report or self.rpt),
                                  os.fsencode(output or self.out))

    def finish(self):
        elapsed = C.c_double()
        for _ in range(20000):
            self.assertEqual(self.lib.swmm_step(C.byref(elapsed)), 0)
            if not elapsed.value:
                break
        else:
            self.fail('Simulation did not finish')
        self.assertEqual(self.lib.swmm_end(), 0)

    def test_api_boundaries_flush_new_text_including_caller_and_control_lines(self):
        self.assertEqual(self.open(), 0)
        self.assertIn(b'STORM WATER MANAGEMENT MODEL', self.rpt.read_bytes())
        self.lib.swmm_writeLine(b'caller 100% text with %s kept literally')
        self.assertIn(b'caller 100% text with %s kept literally', self.rpt.read_bytes())
        self.assertEqual(self.lib.swmm_start(1), 0)
        self.assertIn(b'Flow Units', self.rpt.read_bytes())
        self.finish()
        self.assertIn(b'Flow Routing Continuity', self.rpt.read_bytes())
        self.assertIn(b'by Control Switch', self.rpt.read_bytes())
        self.assertEqual(self.lib.swmm_report(), 0)
        self.assertIn(b'<<< Node J >>>', self.rpt.read_bytes())
        self.assertEqual(self.lib.swmm_close(), 0)
        self.assertIn(b'Analysis ended on:', self.rpt.read_bytes())

    def test_invalid_input_retains_primary_code_and_flushed_context(self):
        self.inp.write_text(REPORT_SOURCE.replace('100 .013', 'BAD .013'))
        self.assertEqual(self.open(), 200)
        text = self.rpt.read_bytes()
        self.assertIn(b'ERROR 211', text)
        self.assertIn(b'BAD .013', text)
        message = C.create_string_buffer(1024)
        self.assertEqual(self.lib.swmm_getError(message, len(message)), 200)
        self.assertEqual(self.lib.swmm_close(), 0)
        self.assertEqual(self.lib.swmm_close(), 0)
        self.inp.write_text(REPORT_SOURCE)
        self.assertEqual(self.open(), 0)

    def test_unclosed_owner_rejects_reopen_without_truncating_either_report(self):
        self.assertEqual(self.open(), 0)
        first = self.rpt.read_bytes()
        second = self.root / 'other.rpt'
        second.write_bytes(b'caller-owned destination')
        self.assertEqual(self.open(report=second), 503)
        self.assertEqual(self.rpt.read_bytes(), first)
        self.assertEqual(second.read_bytes(), b'caller-owned destination')
        self.assertEqual(self.lib.swmm_close(), 0)
        self.assertEqual(self.open(report=second), 0)
        self.assertEqual(self.lib.swmm_close(), 0)

    def test_open_failure_is_distinct_and_recovery_releases_input(self):
        self.assertEqual(self.open(report=self.root / 'missing' / 'model.rpt'), 305)
        self.assertEqual(self.lib.swmm_close(), 0)
        self.inp.unlink()
        self.inp.write_text(REPORT_SOURCE)
        self.assertEqual(self.open(), 0)
        self.assertEqual(self.lib.swmm_close(), 0)

    def test_one_shot_run_does_not_reset_or_close_an_existing_owner(self):
        self.assertEqual(self.open(), 0)
        self.assertEqual(self.lib.swmm_start(1), 0)
        previous = self.rpt.read_bytes()
        other = self.root / 'other.rpt'
        other.write_bytes(b'caller-owned report')
        self.assertEqual(self.lib.swmm_run(os.fsencode(self.inp), os.fsencode(other),
                                           os.fsencode(self.out)), 503)
        self.assertEqual(self.rpt.read_bytes(), previous)
        self.assertEqual(other.read_bytes(), b'caller-owned report')
        self.assertEqual(self.open(report=other), 503)
        self.assertEqual(self.lib.swmm_close(), 0)
        self.assertEqual(self.open(report=other), 0)
        self.assertEqual(self.lib.swmm_close(), 0)

    def test_repeated_start_end_report_and_close_preserve_ownership(self):
        self.assertEqual(self.open(), 0)
        self.assertEqual(self.lib.swmm_start(1), 0)
        self.finish()
        before = handles()
        results = [self.out.read_bytes()]
        for _ in range(3):
            self.assertEqual(self.lib.swmm_start(1), 0)
            self.finish()
            self.assertEqual(self.lib.swmm_report(), 0)
            results.append(self.out.read_bytes())
        self.assertEqual(results, [results[0]] * 4)
        self.assertEqual(self.lib.swmm_close(), 0)
        self.assertEqual(self.lib.swmm_close(), 0)
        self.assertLessEqual(handles(), before)
        self.rpt.unlink()

    @unittest.skipUnless(os.name == 'posix' and Path('/dev/full').exists(), 'POSIX full-device regression')
    def test_full_device_fails_before_output_and_latches_primary_until_close(self):
        # Establish a previous owner explicitly; this must not depend on which
        # other test happened to run first in the shared library process.
        self.assertEqual(self.open(), 0)
        self.assertEqual(self.lib.swmm_close(), 0)
        self.out.write_bytes(b'caller-owned output')
        self.assertEqual(self.open(report=Path('/dev/full')), 306)
        message = C.create_string_buffer(1024)
        self.assertEqual(self.lib.swmm_getError(message, len(message)), 306)
        self.assertIn(b'error writing to report file', message.value)
        self.assertEqual(self.lib.swmm_close(), 306)
        self.assertEqual(self.lib.swmm_close(), 0)
        self.assertEqual(self.out.read_bytes(), b'caller-owned output')
        self.assertEqual(self.open(), 0)
        self.assertEqual(self.lib.swmm_close(), 0)
        self.assertEqual(self.lib.swmm_run(os.fsencode(self.inp), b'/dev/full', os.fsencode(self.out)), 306)
        self.assertEqual(self.lib.swmm_close(), 0)

    @unittest.skipUnless(os.name == 'posix' and Path('/dev/full').exists(), 'POSIX full-device regression')
    def test_early_report_failure_after_named_control_expression_releases_once(self):
        self.inp.write_text(REPORT_SOURCE.replace(
            'RULE Switch\nIF SIMULATION TIME >= 0:02',
            'VARIABLE DepthValue = NODE J DEPTH\n'
            'EXPRESSION TwiceDepth = 2 * DepthValue\n'
            'RULE Switch\nIF TwiceDepth >= 0'))
        before = handles()
        for _ in range(3):
            self.assertEqual(self.open(), 0)
            self.assertEqual(self.lib.swmm_start(1), 0)
            self.finish()
            self.assertEqual(self.lib.swmm_close(), 0)
            previous = self.out.read_bytes()
            self.assertEqual(self.open(report=Path('/dev/full')), 306)
            self.assertEqual(self.lib.swmm_close(), 306)
            self.assertEqual(self.lib.swmm_close(), 0)
            self.assertEqual(self.out.read_bytes(), previous)
        self.assertEqual(self.open(), 0)
        self.assertEqual(self.lib.swmm_close(), 0)
        self.assertLessEqual(handles(), before)


class StandardReportIOTests(ReportIOChecks, unittest.TestCase):
    family = 'standard'

    @classmethod
    def setUpClass(cls):
        cls.lib = library(cls.family)


class CustomReportIOTests(ReportIOChecks, unittest.TestCase):
    family = 'custom'

    @classmethod
    def setUpClass(cls):
        cls.lib = library(cls.family)
