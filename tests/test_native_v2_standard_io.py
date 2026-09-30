"""Direct C I/O regressions; deliberately bypass the Python input inspectors."""

import ctypes
from dataclasses import replace
from datetime import date, time, timedelta
import hashlib
import json
import os
from pathlib import Path
import stat
import struct
import tempfile
import unittest

from easysewer import get_native_capabilities
from easysewer.io.hotstart import HotstartData, HotstartLayout
from easysewer.model import Ref
from easysewer.model import quality as q
from easysewer.utils import probe_library_path
from test_hydrology_v2 import hydrology_model
from test_options_v2 import network


def direct_library(path=None, *, revision_symbol='swmm_getEasySewerStandardFixes'):
    # Allows an uninstalled build to pass the same qualification tests first.
    path = path or os.environ.get('EASYSEWER_STANDARD_TEST_LIBRARY') or probe_library_path('swmm5')
    lib = ctypes.CDLL(path)
    lib.swmm_open.argtypes = [ctypes.c_char_p] * 3
    lib.swmm_start.argtypes = [ctypes.c_int]
    lib.swmm_step.argtypes = [ctypes.POINTER(ctypes.c_double)]
    for name in ('swmm_end', 'swmm_close', revision_symbol):
        getattr(lib, name).argtypes = []
    return lib, Path(path)


def execute(lib, root, source, *, finish=True, scratch_output=False):
    """Use one loaded library repeatedly, so process exit cannot hide leaks."""
    inp, rpt, out = (root / ('model' + suffix) for suffix in ('.inp', '.rpt', '.out'))
    inp.write_text(source, encoding='utf-8')
    codes = []
    cwd = Path.cwd()
    try:
        os.chdir(root)
        codes.append(lib.swmm_open(os.fsencode(inp), os.fsencode(rpt), b'' if scratch_output else os.fsencode(out)))
        if not codes[-1]:
            codes.append(lib.swmm_start(1))
            if not codes[-1] and finish:
                elapsed = ctypes.c_double()
                for _ in range(20000):
                    error = lib.swmm_step(ctypes.byref(elapsed))
                    if error or not elapsed.value:
                        codes.append(error)
                        break
                else:
                    raise AssertionError('Native simulation did not finish')
            codes.append(lib.swmm_end())
    finally:
        codes.append(lib.swmm_close())
        os.chdir(cwd)
    return tuple(codes)


def handles():
    if os.name == 'nt':
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.GetCurrentProcess.restype = ctypes.c_void_p
        kernel.GetProcessHandleCount.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        result = ctypes.c_ulong()
        if not kernel.GetProcessHandleCount(kernel.GetCurrentProcess(), ctypes.byref(result)):
            raise ctypes.WinError(ctypes.get_last_error())
        return result.value
    return len(tuple(Path('/proc/self/fd').iterdir()))


def dry_quality_model(pollutants, units):
    model = hydrology_model()
    model.reinterpret_units(units)
    model.update_options(end_date=date(2020, 1, 30), end_time=time(0, 10),
                         routing_step=timedelta(seconds=60), dry_days=100)
    model.subcatchments.add(replace(model.subcatchments['S'], id='S2', area=3))
    for p in range(pollutants):
        model.pollutants.add(q.Pollutant(id=f'Q{p}', units=('MG/L', 'UG/L', '#/L')[p % 3],
            rainfall_concentration=0, groundwater_concentration=0, rdii_concentration=0, decay_rate=0))
    for k in range(2):
        land = Ref(collection='swmm:landuses', key=f'L{k}')
        model.landuses.add(q.LandUse(id=land.key, sweep_interval=0))
        for sid in ('S', 'S2'):
            model.coverages.add(q.Coverage(subcatchment=Ref(collection='swmm:subcatchments', key=sid),
                landuse=land, percent=25 if k == 0 else 75))
        for p in range(pollutants):
            model.buildup.add(q.Buildup(landuse=land, pollutant=Ref(collection='swmm:pollutants', key=f'Q{p}'),
                function=q.PowerBuildup(maximum=10 + 10*k + p, coefficient=100, exponent=1), normalizer='AREA'))
    return model


class NativeIOChecks:
    """Shared C boundary checks, executed separately against each engine family."""
    def test_multiple_catchments_landuses_and_eight_pollutants_have_independent_mass_oracle(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for units in ('CFS', 'CMS'):
                for count in (0, 1, 2, 8):
                    with self.subTest(units=units, pollutants=count):
                        model = dry_quality_model(count, units)
                        state = root / 'state.hsf'
                        source = model.to_document().text
                        self.assertFalse(any(execute(self.lib, root, source + f'[FILES]\nSAVE HOTSTART "{state}"\n')))
                        raw = state.read_bytes()
                        layout = HotstartLayout.from_model(model)
                        self.assertEqual(len(raw), layout.byte_length())
                        data = HotstartData.from_bytes(raw, layout=layout)
                        self.assertEqual(data.to_bytes(), raw)
                        stride = 8 * (10 + 2*count + (2*(count+1) if count else 0))
                        self.assertEqual(len(raw), 39 + 2*stride + 4*(8 + 3*count))
                        for s, area in enumerate((2, 3)):
                            for k, land in enumerate(data.subcatchments[s].landuses):
                                # No rainfall, washoff or sweeping; antecedent dry
                                # time saturates each POW curve at its maximum.
                                expected = tuple(area * (.25 if k == 0 else .75) * (10+10*k+p) for p in range(count))
                                for actual, value in zip(land.buildup, expected):
                                    self.assertAlmostEqual(actual, value, delta=1e-10)
                                offset = 39 + s*stride + 8*(10+2*count+k*(count+1))
                                self.assertEqual(struct.unpack_from('<'+'d'*(count+1), raw, offset), (*land.buildup, land.last_swept))
                        self.assertFalse(any(execute(self.lib, root, source + f'[FILES]\nUSE HOTSTART "{state}"\n')))

    def test_invalid_headers_short_payloads_nonfinite_and_trailing_bytes_fail_in_c(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); state = root / 'state.hsf'
            model = hydrology_model()
            source = model.to_document().text
            self.assertFalse(any(execute(self.lib, root, source + f'[FILES]\nSAVE HOTSTART "{state}"\n', finish=False)))
            raw = state.read_bytes()
            bad_count = bytearray(raw); struct.pack_into('<i', bad_count, 15, 999)
            cases = [(raw[:n], 333) for n in (0, 1, 14, 15, 18, 38)]
            cases += [(b'bad'+raw[3:], 333), (bytes(bad_count), 333), (raw+b'x', 333)]
            cases += [(raw[:n], 335) for n in (39, 40, 118, len(raw)-1)]
            for fmt, offset in (('<d', 39), ('<f', 119)):
                for value in (float('nan'), float('inf'), -float('inf')):
                    damaged = bytearray(raw); struct.pack_into(fmt, damaged, offset, value)
                    cases.append((bytes(damaged), 335))
            for index, (damaged, expected) in enumerate(cases):
                with self.subTest(case=index):
                    state.write_bytes(damaged)
                    codes = execute(self.lib, root, source + f'[FILES]\nUSE HOTSTART "{state}"\n', finish=False)
                    self.assertEqual(codes[0], 0)
                    self.assertEqual(codes[1], expected)
                    self.assertEqual(codes[2], expected)
                    self.assertIn(f'ERROR {expected}', (root / 'model.rpt').read_text())
                    state.unlink()  # catches leaked Windows FILE handles too

    def test_readonly_hotstart_and_repeated_failed_start_release_handles(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); state = root / 'state.hsf'
            source = network().to_document().text
            self.assertFalse(any(execute(self.lib, root, source + f'[FILES]\nSAVE HOTSTART "{state}"\n')))
            raw = state.read_bytes()
            state.chmod(stat.S_IREAD | stat.S_IRGRP | stat.S_IROTH)
            try:
                self.assertFalse(any(execute(self.lib, root, source + f'[FILES]\nUSE HOTSTART "{state}"\n')))
                self.assertEqual(state.read_bytes(), raw)
            finally:
                state.chmod(stat.S_IREAD | stat.S_IWRITE)
            source += f'[FILES]\nUSE HOTSTART "{state}"\n'
            state.write_bytes(raw[:-1]); execute(self.lib, root, source, finish=False)
            before = handles()
            for _ in range(32):
                codes = execute(self.lib, root, source, finish=False)
                self.assertEqual(codes[1:3], (335, 335))
            self.assertEqual(handles(), before)
            state.unlink()

    def test_repeated_scratch_rain_runoff_and_output_do_not_leak_descriptors(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = hydrology_model().to_document().text
            self.assertFalse(any(execute(self.lib, root, source, finish=False, scratch_output=True)))
            before = handles()
            for _ in range(32):
                self.assertFalse(any(execute(self.lib, root, source, finish=False, scratch_output=True)))
            self.assertEqual(handles(), before)
            self.assertEqual({p.name for p in root.iterdir()}, {'model.inp', 'model.rpt'})

    def test_repeated_close_after_failed_open_or_running_project_is_safe(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for source in ('[OPTIONS]\nFLOW_UNITS BAD\n', network().to_document().text):
                inp, rpt, out = (root / ('model'+suffix) for suffix in ('.inp', '.rpt', '.out'))
                inp.write_text(source)
                code = self.lib.swmm_open(os.fsencode(inp), os.fsencode(rpt), os.fsencode(out))
                if not code:
                    self.assertEqual(self.lib.swmm_start(1), 0)
                self.assertEqual(self.lib.swmm_close(), 0)
                closed = rpt.read_bytes()
                self.assertEqual(self.lib.swmm_close(), 0)
                self.assertEqual(rpt.read_bytes(), closed)
            self.assertFalse(any(execute(self.lib, root, network().to_document().text)))

    def test_climate_file_success_and_failure_close_in_same_process(self):
        from test_climate_v2 import climate_model
        from test_native_v2_climate import user_weather, DRY_CATCHMENT
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); weather = root / 'weather.dat'
            user_weather(weather)
            base = climate_model().to_document().text
            good = base + f'[TEMPERATURE]\nFILE "{weather}"\n'
            bad = base + f'[TEMPERATURE]\nFILE "{weather}" 11/01/2019\n'
            execute(self.lib, root, good, finish=False)
            before = handles()
            for _ in range(24):
                self.assertFalse(any(execute(self.lib, root, good, finish=False)))
                self.assertFalse(any(execute(self.lib, root, good+DRY_CATCHMENT, finish=False)))
                self.assertGreater(execute(self.lib, root, bad, finish=False)[0], 0)
            self.assertEqual(handles(), before)
            weather.unlink()

    @unittest.skipUnless(os.name == 'posix', 'POSIX realpath overflow regression')
    def test_relative_input_past_legacy_path_capacity_opens_without_leaks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); folder = root
            while len(str(folder)) < 275:
                folder = folder / ('nested-'+'x'*20)
            folder.mkdir(parents=True)
            (folder/'model.inp').write_text(network().to_document().text)
            cwd = Path.cwd()
            try:
                os.chdir(folder)
                before = handles()
                for _ in range(16):
                    self.assertEqual(self.lib.swmm_open(b'model.inp', b'model.rpt', b'model.out'), 0)
                    self.assertEqual(self.lib.swmm_close(), 0)
                self.assertEqual(handles(), before)
                self.assertNotIn('ERROR', (folder/'model.rpt').read_text())
            finally:
                os.chdir(cwd)


@unittest.skipUnless(get_native_capabilities()['swmm_solver'], 'Native solver unavailable')
class NativeStandardIOTests(NativeIOChecks, unittest.TestCase):
    def setUp(self):
        self.lib, self.library_path = direct_library()
        self.assertEqual(self.lib.swmm_getEasySewerStandardFixes(), 16)

    def test_packaged_build_record_matches_binary_and_fixed_official_source(self):
        if os.environ.get('EASYSEWER_STANDARD_TEST_LIBRARY'):
            record_path = self.library_path.with_suffix(self.library_path.suffix+'.json')
        else:
            record_path = self.library_path.with_name('swmm5.build.json')
        record = json.loads(record_path.read_text(encoding='utf-8'))
        self.assertEqual(record['sha256'], hashlib.sha256(self.library_path.read_bytes()).hexdigest())
        self.assertEqual(record['standard_fixes'], 16)
        self.assertEqual(record['horton_outfall']['revision'], 1)
        self.assertEqual(record['horton_outfall'], record['source']['horton_outfall'])
        self.assertEqual(record['horton_outfall']['horton_state'], 1)
        self.assertEqual(record['horton_outfall']['outfall_gate'], 1)
        self.assertEqual(self.lib.swmm_getEasySewerHortonState(), 1)
        self.assertEqual(self.lib.swmm_getEasySewerOutfallGate(), 1)
        self.assertEqual(record['horton_outfall']['recipe_sha256'], record['source']['recipe']['horton_outfall.py'])
        self.assertEqual(record['horton_capacity']['revision'], 1)
        self.assertEqual(record['horton_capacity'], record['source']['horton_capacity'])
        self.assertEqual(self.lib.swmm_getEasySewerHortonCapacity(), 1)
        self.assertEqual(record['horton_capacity']['recipe_sha256'],record['source']['recipe']['horton.py'])
        self.assertEqual(record['path_io']['revision'], 1)
        self.assertEqual(record['path_io'], record['source']['path_io'])
        self.assertEqual(self.lib.swmm_getEasySewerPathIO(), 1)
        self.assertIn('scratch.c', record['path_io']['recipes'])
        self.assertEqual(record['checkpoint']['abi'], 2)
        self.assertEqual(record['checkpoint'], record['source']['checkpoint'])
        self.assertEqual(self.lib.swmm_checkpointVersion(), 2)
        self.assertEqual(record['source']['base']['commit'], '7952ca837988b1c32f791812eccc9fd64547e093')
        self.assertEqual(record['source']['patch'], 'easysewer:standard:5.2.4:16')
        self.assertEqual(record['runoff_physics'], 1)
        self.assertEqual(record['runoff_rain_clock'], 1)
        self.assertEqual(record['source']['runoff_rain_clock'], 1)
        self.assertEqual(record['rdii_io'], 1)
        self.assertEqual(record['source']['rdii_io'], 1)
        self.assertEqual(self.lib.swmm_getEasySewerRdiiIO(), 1)
        self.assertIn('rdii_io.c', record['source']['recipe'])
        self.assertEqual(record['routing_io'], 1)
        self.assertEqual(record['source']['routing_io'], 1)
        self.assertEqual(self.lib.swmm_getEasySewerRoutingIO(), 1)
        self.assertIn('routing_io.c', record['source']['recipe'])
        self.assertEqual(record['solver_output_io'], 1)
        self.assertEqual(record['source']['solver_output_io'], 1)
        self.assertEqual(self.lib.swmm_getEasySewerSolverOutputIO(), 1)
        self.assertIn('output_io.c', record['source']['recipe'])
        self.assertEqual(record['climate_io'], 1)
        self.assertEqual(record['source']['climate_io'], 1)
        self.assertEqual(self.lib.swmm_getEasySewerClimateIO(), 1)
        self.assertIn('climate_io.c', record['source']['recipe'])
        self.assertEqual(record['timeseries_io'], 1)
        self.assertEqual(record['report_io'], 1)
        self.assertEqual(record['lid_report_io'], 1)
        self.assertEqual(record['source']['timeseries_io'], 1)
        self.assertEqual(record['source']['report_io'], 1)
        self.assertEqual(record['source']['lid_report_io'], 1)
        self.assertEqual(self.lib.swmm_getEasySewerTimeSeriesIO(), 1)
        self.assertEqual(self.lib.swmm_getEasySewerReportIO(), 1)
        self.assertEqual(self.lib.swmm_getEasySewerLidReportIO(), 1)
        self.assertIn('series_io.c', record['source']['recipe'])
        self.assertIn('report_io.c', record['source']['recipe'])
        self.assertIn('runoff_rain.c', record['source']['recipe'])
        self.assertIn('-fno-fast-math', record['command'])
        self.assertIn('-ffp-contract=off', record['command'])


if __name__ == '__main__':
    unittest.main()
