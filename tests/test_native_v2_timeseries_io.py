"""Direct time-series files, calendar anchoring and consumer independence."""

import os
from pathlib import Path
import tempfile
import unittest

from easysewer.utils import probe_library_path
from test_native_v2_routing_io import SOURCE
from test_native_v2_standard_io import direct_library, execute, handles


def library(family):
    path = os.environ.get('EASYSEWER_SERIES_TEST_' + family.upper())
    path = path or probe_library_path('swmm5' if family == 'standard' else 'flexible_ponding')
    if not path:
        raise unittest.SkipTest('Native library unavailable')
    lib, _ = direct_library(path, revision_symbol='swmm_getEasySewerStandardFixes'
                            if family == 'standard' else 'swmm_getEasySewerNativeIOFixes')
    if not hasattr(lib, 'swmm_getEasySewerTimeSeriesIO') or lib.swmm_getEasySewerTimeSeriesIO() != 1:
        raise unittest.SkipTest('Time-series I/O revision 1 unavailable')
    return lib


class TimeSeriesChecks:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = self.root / 'series.dat'
        self.source = SOURCE + ('[TIMESERIES]\nS FILE "' + str(self.data) + '"\n'
                                '[INFLOWS]\nJ FLOW S FLOW 1 1\n')
        self.addCleanup(self.lib.swmm_close)

    def run_bytes(self, source):
        self.assertEqual(execute(self.lib, self.root, source), (0, 0, 0, 0, 0))
        return (self.root / 'model.out').read_bytes()

    def test_runner_accepts_complete_long_record_only_with_qualified_backend(self):
        from dataclasses import replace
        from easysewer.io.inp import InpDocument
        from easysewer.model import Model, FileReference
        from easysewer.runtime import Runner, RunConfig, StandardBackend, FlexiblePondingBackend
        cls = StandardBackend if self.family == 'standard' else FlexiblePondingBackend
        backend = cls(library=str(self.lib._name))
        self.data.write_bytes(b';' + b'A' * 160 + b'\n0 ' + b'0' * 160 +
                              b'.1\n01/01/2020 0:05 .3\n0:10 .2')
        source = self.source.replace('FLOW_UNITS CFS',
            'FLOW_UNITS CFS\nFLOW_ROUTING DYNWAVE\nALLOW_PONDING YES')
        model = Model.from_document(InpDocument.from_text(source), strict=True)
        destination = self.root / 'published'
        config = RunConfig(backend=backend.key, keep_failed_artifacts=False,
            output_directory=FileReference(path=str(destination), direction='output'))
        result = Runner(backends={backend.key: backend}).run(model, config)
        self.assertTrue(result.succeeded, (result.failure, result.diagnostics))
        self.assertTrue(result.native_completed)
        published = {p.relative_to(destination): p.read_bytes()
                     for p in destination.rglob('*') if p.is_file()}

        class WithoutSeriesProof(cls):
            def execution_info(self, info):
                known = super().execution_info(info)
                return replace(known, capabilities=tuple(c for c in known.capabilities
                    if not c.startswith('easysewer:timeseries-io:')))

        unknown = WithoutSeriesProof(library=str(self.lib._name))
        failed = Runner(backends={unknown.key: unknown}).run(model, replace(config, overwrite=True))
        self.assertEqual(failed.status, 'rejected')
        self.assertFalse(failed.native_completed)
        self.assertIsNone(failed.retained_directory)
        self.assertEqual(published, {p.relative_to(destination): p.read_bytes()
                                    for p in destination.rglob('*') if p.is_file()})

    def test_complete_final_rows_comments_and_long_fields(self):
        # The Windows runtime retains a few handles on its first solve. Measure
        # subsequent solves, as for the existing native lifecycle checks.
        self.data.write_bytes(b'0 0\n1 0')
        self.run_bytes(self.source)
        before = handles()
        cases = [b'0 0\n1 0', b'01/01/2020 0:00 0\n1:00 0',
                 b'Jan-1-2020 0:00:00.9 0\n1:00:00 0',
                 b';' + b'A' * 1021 + b'\n0 0\n1 0',
                 b'0 ' + b'0' * 900 + b'\n1 0',
                 b'0' * 900 + b' 0\n1 0',
                 b'\n\t; comment\n01/01/2020\t0\t0\tignored\n\n1 0']
        for raw in cases:
            for ending in (b'', b'\n', b'\r\n'):
                with self.subTest(prefix=raw[:30], ending=ending):
                    self.data.write_bytes(raw + ending)
                    self.run_bytes(self.source)
                    self.assertEqual(self.data.read_bytes(), raw + ending)
        self.assertLessEqual(handles(), before + 1)

    def test_late_damage_is_rejected_before_output_and_recovers(self):
        valid = b'0 0\n1 0\n'
        invalid = [b'', b'; comments only', b'\xef\xbb\xbf' + valid,
                   valid + b'01/01/2020 2 NaN', valid + b'01/01/2020 2 1e309',
                   valid + b'02/30/2020 2 0', valid + b'01/01/2147483648 2 0',
                   valid + b'01/01/20_20 2 0', valid + '01/01/２０２０ 2 0'.encode(),
                   valid + b'01/01/2020 2147483648:00 0',
                   valid + b'01/01/2020 2:60 0', valid + b'01/01/2020 2:00:60 0',
                   valid + b'01/01/2020 1e300 0', valid + b'01/01/2020 2junk 0',
                   valid + b'01/01/2020 2 0suffix', valid + b'01/01/2020 2 0\x00extra',
                   valid + b'01/01/2020 2 0\x1aextra', valid + b'01/01/2020 2 0\rextra',
                   valid + b';' + b'A' * 1022]
        before = handles()
        for raw in invalid:
            with self.subTest(tail=raw[-40:]):
                self.data.write_bytes(raw)
                out = self.root / 'model.out'
                out.write_bytes(b'previous result')
                codes = execute(self.lib, self.root, self.source)
                self.assertEqual(codes[0], 363)
                self.assertEqual(out.read_bytes(), b'previous result')
                self.assertEqual(self.lib.swmm_close(), 0)
                self.assertEqual(self.data.read_bytes(), raw)
                self.data.write_bytes(valid)
                self.run_bytes(self.source)
        self.assertLessEqual(handles(), before + 1)

    def test_order_failure_is_distinct_and_missing_file_can_be_retried(self):
        self.assertEqual(execute(self.lib, self.root, self.source)[0], 361)
        for raw in (b'0 0\n0 1', b'1 0\n0 1'):
            self.data.write_bytes(raw)
            self.assertEqual(execute(self.lib, self.root, self.source)[0], 173)
        self.data.write_bytes(b'0 0\n1 1')
        self.run_bytes(self.source)

    def test_relative_prefix_and_calendar_continuation_match_explicit_dates(self):
        source = self.source.replace('START_DATE 01/01/2020',
            'START_DATE 01/01/2020\nSTART_TIME 12:00').replace('END_TIME 0:10', 'END_TIME 12:10')
        rows = (b'0 0\n01/01/2020 12:05 1\n12:10 0',
                b'01/01/2020 12:00 0\n12:05 1\n12:10 0')
        results = []
        for raw in rows:
            self.data.write_bytes(raw)
            results.append(self.run_bytes(source))
        inline = source.replace('S FILE "' + str(self.data) + '"',
                                'S 01/01/2020 12:00 0 12:05 1 12:10 0')
        results.append(self.run_bytes(inline))
        self.assertEqual(results, [results[0]] * 3)

    def test_inflow_and_outfall_shared_lookup_match_separate_series(self):
        rows = '01/01/2020 0:00 .1\n0:05 .9\n0:10 .1\n'
        self.data.write_text(rows)
        results = []
        for external in (False, True):
            for shared in (False, True):
                with self.subTest(external=external, shared=shared):
                    series = 'S FILE "' + str(self.data) + '"\n' if external else ''.join(
                        'S ' + row + '\n' for row in rows.splitlines())
                    if not shared:
                        series += ''.join('T ' + row[2:] + '\n' for row in series.splitlines())
                    source = SOURCE.replace('O 0 FREE', 'O 0 TIMESERIES ' + ('S' if shared else 'T'))
                    source += '[TIMESERIES]\n' + series + '[INFLOWS]\nJ FLOW S FLOW 1 1\n'
                    results.append(self.run_bytes(source))
        self.assertEqual(results, [results[0]] * 4)

    def test_rainfall_external_and_inline_match_with_one_or_many_points(self):
        for rows in ('0 .5\n', '0 .5\n0:05 0\n0:08 .1\n'):
            with self.subTest(rows=rows):
                source = (SOURCE + '[RAINGAGES]\nR INTENSITY 0:01 1 TIMESERIES Rain\n'
                    '[SUBCATCHMENTS]\nS R J 2 25 200 1 0\n[SUBAREAS]\n'
                    'S .01 .1 .05 .05 25 OUTLET\n[INFILTRATION]\nS 3 .5 4 7 0\n[TIMESERIES]\n')
                self.data.write_text(rows)
                external = self.run_bytes(source + 'Rain FILE "' + str(self.data) + '"\n')
                inline = self.run_bytes(source + ''.join('Rain ' + row + '\n' for row in rows.splitlines()))
                self.assertEqual(external, inline)

    def test_restart_rewinds_owned_streams_without_losing_the_anchor(self):
        self.data.write_bytes(b'0 0\n01/01/2020 0:05 1\n0:10 0')
        inp, rpt, out = (self.root / ('model' + suffix) for suffix in ('.inp', '.rpt', '.out'))
        inp.write_text(self.source)
        before = handles()
        self.assertEqual(self.lib.swmm_open(*(os.fsencode(p) for p in (inp, rpt, out))), 0)
        import ctypes
        outputs = []
        for _ in range(3):
            self.assertEqual(self.lib.swmm_start(1), 0)
            elapsed = ctypes.c_double()
            for step in range(20000):
                self.assertEqual(self.lib.swmm_step(ctypes.byref(elapsed)), 0)
                if not elapsed.value:
                    break
            else:
                self.fail('Simulation did not finish')
            self.assertEqual(self.lib.swmm_end(), 0)
            outputs.append(out.read_bytes())
        self.assertEqual(self.lib.swmm_close(), 0)
        self.assertEqual(outputs, [outputs[0]] * 3)
        self.assertLessEqual(handles(), before + 1)


class StandardTimeSeriesTests(TimeSeriesChecks, unittest.TestCase):
    family = 'standard'

    @classmethod
    def setUpClass(cls):
        cls.lib = library('standard')


class CustomTimeSeriesTests(TimeSeriesChecks, unittest.TestCase):
    family = 'custom'

    @classmethod
    def setUpClass(cls):
        cls.lib = library('custom')


if __name__ == '__main__':
    unittest.main()
