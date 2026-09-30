"""Direct climate stream contracts, independent of Python data inspection."""

import ctypes as C
from datetime import datetime
import os
from pathlib import Path
import tempfile
import unittest

from easysewer.utils import probe_library_path
from test_climate_data_v2 import ghcnd_fixture, monthly_fixture
from test_native_v2_routing_io import SOURCE
from test_native_v2_standard_io import direct_library, execute, handles


def library(family):
    path = os.environ.get('EASYSEWER_CLIMATE_TEST_' + family.upper())
    path = path or probe_library_path('swmm5' if family == 'standard' else 'flexible_ponding')
    if not path:
        raise unittest.SkipTest('Native library unavailable')
    lib, _ = direct_library(path, revision_symbol='swmm_getEasySewerStandardFixes'
                            if family == 'standard' else 'swmm_getEasySewerNativeIOFixes')
    if not hasattr(lib, 'swmm_getEasySewerClimateIO') or lib.swmm_getEasySewerClimateIO() != 1:
        raise unittest.SkipTest('Climate I/O revision 1 is not installed')
    return lib


class ClimateIOChecks:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.weather = self.root / 'weather.dat'
        self.source = SOURCE + ('[TEMPERATURE]\nFILE "' + str(self.weather) + '"\nWINDSPEED FILE\n'
                               '[EVAPORATION]\nFILE 1 1 1 1 1 1 1 1 1 1 1 1\n')
        self.addCleanup(self.lib.swmm_close)

    def test_runner_accepts_complete_long_record_only_with_qualified_backend(self):
        from dataclasses import replace
        from easysewer.io.inp import InpDocument
        from easysewer.model import Model, FileReference
        from easysewer.runtime import Runner, RunConfig, StandardBackend, FlexiblePondingBackend
        cls = StandardBackend if self.family == 'standard' else FlexiblePondingBackend
        backend = cls(library=str(self.lib._name))
        self.weather.write_bytes(b'A' * 160 + b' 2020 1 1 60 40 .2 5')
        source = self.source.replace('FLOW_UNITS CFS',
            'FLOW_UNITS CFS\nFLOW_ROUTING DYNWAVE\nALLOW_PONDING YES')
        model = Model.from_document(InpDocument.from_text(source), strict=True)
        destination = self.root / 'published'
        # This comparison covers the entire destination, so opt out of the
        # default retained private workspace used to diagnose failed runs.
        config = RunConfig(backend=backend.key, keep_failed_artifacts=False,
            output_directory=FileReference(path=str(destination), direction='output'))
        result = Runner(backends={backend.key: backend}).run(model, config)
        self.assertTrue(result.succeeded, (result.failure, result.diagnostics))
        self.assertTrue(result.native_completed)
        published = {p.relative_to(destination): p.read_bytes()
                     for p in destination.rglob('*') if p.is_file()}

        class WithoutClimateProof(cls):
            def execution_info(self, info):
                known = super().execution_info(info)
                return replace(known, capabilities=tuple(c for c in known.capabilities
                    if not c.startswith('easysewer:climate-io:')))

        unknown = WithoutClimateProof(library=str(self.lib._name))
        failed = Runner(backends={unknown.key: unknown}).run(model, replace(config, overwrite=True))
        self.assertEqual(failed.status, 'rejected')
        self.assertFalse(failed.native_completed)
        self.assertIsNone(failed.retained_directory)
        self.assertEqual(published, {p.relative_to(destination): p.read_bytes()
                                    for p in destination.rglob('*') if p.is_file()})

    def test_four_formats_complete_final_record_needs_no_newline(self):
        for raw in (b'A 2020 1 1 60 40 .2 5\n', ghcnd_fixture(),
                    monthly_fixture('TD3200', months=(1,)), monthly_fixture('DLY0204', months=(1,))):
            for ending in (b'\n', b'\r\n', b''):
                data = raw.rstrip(b'\r\n') + ending
                with self.subTest(format=data[:30], ending=ending):
                    self.weather.write_bytes(data)
                    self.assertEqual(execute(self.lib, self.root, self.source), (0, 0, 0, 0, 0))
                    self.assertEqual(self.weather.read_bytes(), data)

    def test_long_station_and_maximum_line_are_bounded_without_truncation(self):
        tail = b' 2020 1 1 60 40 .2 5'
        for length in (79, 80, 160, 1022 - len(tail)):
            with self.subTest(length=length):
                data = b'A' * length + tail
                self.weather.write_bytes(data)
                self.assertEqual(execute(self.lib, self.root, self.source), (0, 0, 0, 0, 0))
        self.weather.write_bytes(b'A' * (1023 - len(tail)) + tail)
        self.assertEqual(execute(self.lib, self.root, self.source)[0], 338)

    def test_full_file_validation_rejects_later_corruption_and_recovers(self):
        valid = b'A 2020 1 1 60 40 .2 5\n'
        invalid = (b'', b'\n' + valid, b'\xef\xbb\xbf' + valid,
                   valid + b'A 2020 2 30 60 40 .2 5\n',
                   valid + b'A 2020 2 1 NaN 40 .2 5\n',
                   valid + b'A 2020 2 1 1e309 40 .2 5\n',
                   valid + b'A 2020 2 1 60\x00 40 .2 5\n',
                   valid + b'A 2020 2 1 60\x1a 40 .2 5\n',
                   valid + b'A 2020 2 1 60\r 40 .2 5\n',
                   valid + b'A 2019 12 1 60 40 .2 5\n',
                   monthly_fixture('TD3200')[:-20], monthly_fixture('DLY0204')[:-20])
        count = handles()
        for raw in invalid:
            with self.subTest(raw=raw[:50]):
                self.weather.write_bytes(raw)
                out = self.root / 'model.out'
                out.write_bytes(b'previous publication')
                codes = execute(self.lib, self.root, self.source)
                self.assertEqual(codes[0], 338)
                self.assertEqual(out.read_bytes(), b'previous publication')
                self.weather.write_bytes(valid)
                self.assertEqual(execute(self.lib, self.root, self.source), (0, 0, 0, 0, 0))
        self.assertLessEqual(handles(), count + 1)

    def test_missing_start_month_is_distinct_from_empty_month(self):
        self.weather.write_bytes(b'A 2020 2 1 60 40 .2 5')
        self.assertEqual(execute(self.lib, self.root, self.source)[0], 339)
        self.weather.write_bytes(b'DLY12345600TMAX  2020019999000')
        self.assertEqual(execute(self.lib, self.root, self.source), (0, 0, 0, 0, 0))

    def test_same_project_restart_restores_climate_stream_and_output(self):
        from test_native_v2_climate import DRY_CATCHMENT
        self.weather.write_bytes(b'A 2020 1 1 60 40 .2 5\nA 2020 2 1 70 50 .4 6')
        inp, rpt, out = (self.root / ('model' + suffix) for suffix in ('.inp', '.rpt', '.out'))
        # Routing-only keeps the climate stream open; runoff_end closes it.
        # Repeated start must reset the former and reopen the latter.
        for runoff in ('', DRY_CATCHMENT):
            with self.subTest(runoff=bool(runoff)):
                inp.write_text(self.source + runoff)
                self.assertEqual(self.lib.swmm_open(os.fsencode(inp), os.fsencode(rpt), os.fsencode(out)), 0)
                outputs = []
                for _ in range(3):
                    self.assertEqual(self.lib.swmm_start(1), 0)
                    elapsed = C.c_double()
                    for _ in range(20000):
                        self.assertEqual(self.lib.swmm_step(C.byref(elapsed)), 0)
                        if not elapsed.value:
                            break
                    else:
                        self.fail('Simulation did not finish')
                    self.assertEqual(self.lib.swmm_end(), 0)
                    outputs.append(out.read_bytes())
                self.assertEqual(outputs, [outputs[0]] * 3)
                self.assertEqual(self.lib.swmm_close(), 0)

    def test_series_evaporation_holds_latest_rate_and_applies_adjustment_once(self):
        from easysewer.io.output import OutputReader
        from test_native_v2_climate import DRY_CATCHMENT
        for units in ('CFS', 'CMS'):
            with self.subTest(units=units):
                source = (SOURCE.replace('FLOW_UNITS CFS', 'FLOW_UNITS ' + units)
                          .replace('END_DATE 01/01/2020', 'END_DATE 01/03/2020')
                          .replace('REPORT_STEP 0:01', 'REPORT_STEP 0:30')
                          .replace('ROUTING_STEP 1', 'ROUTING_STEP 300'))
                source += (DRY_CATCHMENT + '[EVAPORATION]\nTIMESERIES E\n[TIMESERIES]\n'
                           'E 01/01/2020 0:00 .1 6:00 .2 12:00 .3\n'
                           'E 01/02/2020 6:00 .4 12:00 .5\n[ADJUSTMENTS]\n'
                           'EVAPORATION ' + ' '.join(['.05'] * 12) + '\n')
                self.assertEqual(execute(self.lib, self.root, source), (0, 0, 0, 0, 0))
                with OutputReader(self.root / 'model.out') as reader:
                    series = reader.series(None, 'swmm:potential_evaporation')
                # Inspect interior points, independently of report/event boundary
                # ordering. The last rate must hold after the series ends.
                for hour, rate in ((3, .15), (9, .25), (15, .35), (27, .35), (33, .45), (40, .55)):
                    values = [value for stamp, value in zip(series.times, series.values)
                              if abs((stamp-datetime(2020, 1, 1)).total_seconds()/3600-hour) < .001]
                    self.assertEqual(len(values), 1)
                    self.assertAlmostEqual(values[0], rate, delta=1e-6)

    def test_temperature_and_evaporation_have_independent_shared_series_cursors(self):
        from easysewer.io.output import OutputReader
        from test_native_v2_climate import DRY_CATCHMENT
        entries = ('01/01/2020 0:00 .1\n01/01/2020 6:00 .2\n01/01/2020 12:00 .3\n'
                   '01/02/2020 6:00 .4\n01/02/2020 12:00 .5\n')
        data = self.root / 'series.dat'
        data.write_text(entries)
        results = []
        for external in (False, True):
            for shared in (False, True):
                with self.subTest(external=external, shared=shared):
                    series = ('E FILE "' + str(data) + '"\n') if external else ''.join(
                        'E ' + line + '\n' for line in entries.splitlines())
                    if not shared:
                        series += ''.join('T ' + line[2:] + '\n' for line in series.splitlines())
                    source = (SOURCE.replace('END_DATE 01/01/2020', 'END_DATE 01/03/2020')
                              .replace('REPORT_STEP 0:01', 'REPORT_STEP 0:30')
                              .replace('ROUTING_STEP 1', 'ROUTING_STEP 300'))
                    source += (DRY_CATCHMENT + '[TEMPERATURE]\nTIMESERIES ' + ('E' if shared else 'T')
                               + '\n[EVAPORATION]\nTIMESERIES E\n[TIMESERIES]\n' + series)
                    before = handles()
                    self.assertEqual(execute(self.lib, self.root, source), (0, 0, 0, 0, 0))
                    self.assertLessEqual(handles(), before + 1)
                    with OutputReader(self.root / 'model.out') as reader:
                        results.append(tuple(reader.series(None, key).values for key in
                                             ('swmm:temperature', 'swmm:potential_evaporation')))
        self.assertEqual(results, [results[0]] * 4)

    def test_calendar_limits_and_file_start_mapping_overflow(self):
        for start, end, record in (
            ('01/01/0001', '01/02/0001', b'A 1 1 1 60 40 .2 5'),
            ('12/30/9999', '12/31/9999', b'A 9999 12 30 60 40 .2 5'),
            ('02/28/2020', '03/01/2020', b'A 2020 2 28 60 40 .2 5\nA 2020 2 29 70 50 .3 6\nA 2020 3 1 80 60 .4 7'),
        ):
            with self.subTest(start=start):
                source = (self.source.replace('START_DATE 01/01/2020', 'START_DATE ' + start)
                          .replace('END_DATE 01/01/2020', 'END_DATE ' + end)
                          .replace('ROUTING_STEP 1', 'ROUTING_STEP 3600')
                          .replace('REPORT_STEP 0:01', 'REPORT_STEP 1:00'))
                self.weather.write_bytes(record)
                self.assertEqual(execute(self.lib, self.root, source), (0, 0, 0, 0, 0))
        # The simulation calendar is valid, but the independently mapped file
        # calendar would exceed year 9999. Native must stop before indexing it.
        self.weather.write_bytes(b'A 9999 12 31 60 40 .2 5')
        source = (self.source.replace('END_DATE 01/01/2020', 'END_DATE 01/03/2020')
                  .replace('ROUTING_STEP 1', 'ROUTING_STEP 3600')
                  .replace('REPORT_STEP 0:01', 'REPORT_STEP 1:00')
                  .replace(str(self.weather) + '"', str(self.weather) + '" 12/31/9999'))
        codes = execute(self.lib, self.root, source)
        self.assertEqual(codes[:3], (0, 0, 338))


class StandardClimateIOTests(ClimateIOChecks, unittest.TestCase):
    family = 'standard'
    @classmethod
    def setUpClass(cls):
        cls.lib = library('standard')


class CustomClimateIOTests(ClimateIOChecks, unittest.TestCase):
    family = 'custom'
    @classmethod
    def setUpClass(cls):
        cls.lib = library('custom')


if __name__ == '__main__':
    unittest.main()
