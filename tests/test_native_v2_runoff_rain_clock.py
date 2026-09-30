"""Rain-history controls observed through public link settings, not C internals."""

import ctypes
from dataclasses import replace
from datetime import datetime, timedelta
import math
import os
from pathlib import Path
import struct
import tempfile
import unittest

from easysewer import get_native_capabilities
from easysewer.model import FileReference, Ref
from easysewer.model.hydrology import FileRainfall
from easysewer.model.resources import FileTimeSeries, SeriesPoint
from easysewer.utils import probe_library_path
from test_hydrology_v2 import hydrology_model
from test_native_v2_standard_io import direct_library
from test_native_v2_runoff_physics import out_rows
from test_native_v2_files import selected


def integral(schedule, start, end):
    return sum(max(0., min(end, b)-max(start, a))*v/3600.
               for (a, v), (b, _) in zip(schedule, schedule[1:]))


def rate_at(schedule, seconds):
    return next((v for (a, v), (b, _) in zip(schedule, schedule[1:]) if a <= seconds < b), 0.)


def rain_model(root, schedule, *, duration, units='CFS', source='inline', form='INTENSITY',
               co_gage=False, start=datetime(2020, 1, 30), route_step=60, rule_step=0):
    model = selected(hydrology_model(form=form)); model.reinterpret_units(units)
    end = start + timedelta(seconds=duration)
    model.update_options(start_date=start.date(), start_time=start.time(), end_date=end.date(), end_time=end.time(),
                         report_start_date=start.date(), report_start_time=start.time(),
                         routing_step=timedelta(seconds=route_step), rule_step=timedelta(seconds=rule_step),
                         report_step=timedelta(seconds=max(60, math.ceil(route_step))))
    interval = schedule[1][0]-schedule[0][0]
    model.raingages.update('R', interval=timedelta(seconds=interval))
    values = []; total = 0.
    for seconds, value in schedule:
        if form != 'INTENSITY': value *= interval/3600.
        if form == 'CUMULATIVE': total += value; value = total
        values.append((seconds, value))
    model.timeseries.update('Rain', points=tuple(SeriesPoint(time=timedelta(seconds=s), value=v) for s, v in values))
    if source == 'timeseries-file':
        path = root/'rain-series.txt'
        path.write_text(''.join(f'{s/3600:.17g} {v:.17g}\n' for s, v in values), encoding='ascii')
        model.timeseries.replace('Rain', FileTimeSeries(id='Rain', file=FileReference(path=str(path))))
    elif source == 'rain-file':
        path = root/'rain.txt'; lines = []
        for seconds, value in values:
            stamp = start+timedelta(seconds=seconds)
            lines.append(f'Station {stamp:%Y %m %d %H %M} {value:.17g}\n')
        path.write_text(''.join(lines), encoding='ascii')
        model.raingages.update('R', source=FileRainfall(file=FileReference(path=str(path)),
                                    station='Station', units='IN' if units == 'CFS' else 'MM'))
    if co_gage:
        model.raingages.add(replace(model.raingages['R'], id='R2'))
        model.subcatchments.add(replace(model.subcatchments['S'], id='S2',
                                      rain_gage=Ref(collection='swmm:raingages', key='R2')))
    for window in range(49):
        model.nodes.add(replace(model.nodes['O'], id=f'O{window}'))
        model.links.add(replace(model.links['P'], id=f'Probe{window}',
                               outlet=Ref(collection='swmm:nodes', key=f'O{window}')))
    return model


def controlled_source(model, *, gage='R'):
    source = model.to_document().text+'[CURVES]\nRainProbe CONTROL 0 0 1000 1\n[CONTROLS]\n'
    for window in range(49):
        attribute = f'{window}-HR_DEPTH' if window else 'INTENSITY'
        # Native symbol lookup accepts prefixes. Equal-width names prevent
        # V1/E1 from accidentally owning the V10/E10 measurement.
        source += (f'VARIABLE V{window:02} = GAGE {gage} {attribute}\nEXPRESSION E{window:02} = V{window:02}\n'
                   f'RULE R{window}\nIF E{window:02} >= 0\nTHEN OUTLET Probe{window} SETTING = CURVE RainProbe\n')
    return source


def cache_bytes(model, steps):
    count = len(model.subcatchments); unit = ('CFS','GPM','MGD','CMS','LPS','MLD').index(model.units.flow_units)
    # The stored rain column is deliberately unrelated to the source.
    row = struct.pack('<8f', 999., 0., 0., 0., 0., 0., 0., 0.)
    return b'SWMM5-RUNOFF'+struct.pack('<4i', count, 0, unit, len(steps))+b''.join(
        struct.pack('<f', dt)+row*count for dt in steps)


def trace(lib, root, source, duration, *, api=None):
    inp = root/'model.inp'; inp.write_text(source, encoding='utf-8')
    lib.swmm_getIndex.argtypes = [ctypes.c_int, ctypes.c_char_p]
    lib.swmm_getValue.argtypes = [ctypes.c_int, ctypes.c_int]; lib.swmm_getValue.restype = ctypes.c_double
    lib.swmm_setValue.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_double]
    previous = 0.; rows = []; elapsed = ctypes.c_double()
    try:
        assert lib.swmm_open(os.fsencode(inp), os.fsencode(root/'model.rpt'), os.fsencode(root/'model.out')) == 0
        error = lib.swmm_start(1)
        assert error == 0, ('swmm_start', error)
        indices = tuple(lib.swmm_getIndex(3, f'Probe{n}'.encode()) for n in range(49))
        assert min(indices) >= 0
        for _ in range(25000):
            if api is not None: lib.swmm_setValue(100, 0, api(previous))
            error = lib.swmm_step(ctypes.byref(elapsed))
            assert error == 0, (error, (root/'model.rpt').read_text(errors='replace'))
            now = elapsed.value*86400 if elapsed.value else duration
            rows.append((previous, now, tuple(lib.swmm_getValue(407, i)*1000 for i in indices)))
            previous = now
            if elapsed.value == 0: break
        else: raise AssertionError('Clock fixture exceeded its routing step budget')
        assert lib.swmm_end() == 0
    finally: lib.swmm_close()
    return rows


@unittest.skipUnless(get_native_capabilities()['swmm_solver'] and
                    get_native_capabilities()['flexible_ponding'], 'Both native solvers required')
class NativeRunoffRainClockTests(unittest.TestCase):
    def libraries(self):
        for family, name, variable in (
            ('standard', 'swmm5', 'EASYSEWER_STANDARD_TEST_LIBRARY'),
            ('custom', 'flexible_ponding', 'EASYSEWER_CUSTOM_TEST_LIBRARY')):
            lib, _ = direct_library(os.environ.get(variable) or probe_library_path(name),
                                    revision_symbol='swmm_getEasySewerRunoffRainClock')
            self.assertEqual(lib.swmm_getEasySewerRunoffRainClock(), 1)
            yield family, lib

    def assert_history(self, rows, schedule, *, rule_step=0, tolerance=2e-5):
        for previous, now, actual in rows:
            evaluation = previous if not rule_step else math.floor((previous+1e-7)/rule_step)*rule_step
            completed = math.floor((evaluation+1e-7)/3600)*3600
            self.assertAlmostEqual(actual[0], rate_at(schedule, round(evaluation, 7)), delta=tolerance,
                                   msg=str(('instantaneous', previous, now)))
            for window in range(1, 49):
                expected = integral(schedule, max(0, completed-window*3600), completed)
                self.assertAlmostEqual(actual[window], expected, delta=tolerance,
                                       msg=str((window, previous, now, evaluation)))

    def replay(self, lib, root, model, steps, duration, *, gage='R', api=None):
        cache = root/'runoff.bin'; cache.write_bytes(cache_bytes(model, steps))
        return trace(lib, root, controlled_source(model, gage=gage)+f'[FILES]\nUSE RUNOFF "{cache}"\n', duration, api=api)

    def test_all_48_windows_and_instantaneous_rain_are_independent_of_frame_boundaries(self):
        duration = 50*3600
        schedule = tuple((i*1800, (1., 3., 0., 2., 4.)[i % 5]) for i in range(100)) + ((duration, 0.),)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for family, lib in self.libraries():
                model = rain_model(root, schedule, duration=duration)
                for steps in ((duration,), (17.5, 4000.25, duration-4017.75), (3e38,)):
                    with self.subTest(family=family, steps=steps):
                        rows = self.replay(lib, root, model, steps, duration)
                        self.assert_history(rows, schedule)
                        observed = out_rows(root/'model.out')
                        self.assertEqual(len(observed), duration//60)
                        for i, row in enumerate(observed):
                            self.assertAlmostEqual(row[0], rate_at(schedule, (i+1)*60), delta=2e-6)

    def test_inline_external_series_and_rain_files_cover_all_rain_forms_and_units(self):
        duration = 7200; schedule = tuple((i*900, (1., 3., 0., 2.)[i % 4]) for i in range(9))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for family, lib in self.libraries():
                for units in ('CFS', 'CMS'):
                    for source in ('inline', 'timeseries-file', 'rain-file'):
                        for form in ('INTENSITY', 'VOLUME', 'CUMULATIVE'):
                            with self.subTest(family=family, units=units, source=source, form=form):
                                model = rain_model(root, schedule, duration=duration, units=units, source=source, form=form)
                                self.assert_history(self.replay(lib, root, model, (3e38,), duration), schedule)

    def test_cogages_ignore_flags_and_monthly_zero_to_nonzero_adjustment(self):
        duration = 7200; schedule = tuple((i*900, 1.) for i in range(9))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for family, lib in self.libraries():
                model = rain_model(root, schedule, duration=duration, co_gage=True,
                                   start=datetime(2020, 1, 31, 23, 30))
                cache = root/'runoff.bin'; cache.write_bytes(cache_bytes(model, (3e38,)))
                source = controlled_source(model, gage='R2')
                source += '[ADJUSTMENTS]\nRAINFALL 0 2 1 1 1 1 1 1 1 1 1 1\n'
                with self.subTest(family=family, mode='monthly'):
                    rows = trace(lib, root, source+f'[FILES]\nUSE RUNOFF "{cache}"\n', duration)
                    self.assert_history(rows, ((0., 0.), (1800., 2.), (duration, 0.)))
                model.update_options(ignore_rainfall=True)
                with self.subTest(family=family, mode='ignore'):
                    self.assert_history(self.replay(lib, root, model, (3e38,), duration, gage='R2'),
                                        ((0., 0.), (duration, 0.)))

    def test_api_changes_follow_consumer_steps_and_propagate_to_cogages(self):
        duration = 7200; source_rain = tuple((i*900, 9.) for i in range(9))
        actual_rain = ((0., 1.), (1800., 3.), (3600., 0.), (5400., 2.), (7200., 0.))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for family, lib in self.libraries():
                model = rain_model(root, source_rain, duration=duration, co_gage=True)
                with self.subTest(family=family):
                    rows = self.replay(lib, root, model, (3e38,), duration, gage='R2',
                                       api=lambda t: rate_at(actual_rain, round(t, 7)))
                    self.assert_history(rows, actual_rain)

    def test_fractional_routing_rule_steps_and_nonhour_model_origin(self):
        duration = 3602; schedule = tuple((i*900, (1., 3., 0., 2.)[i % 4]) for i in range(6))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for family, lib in self.libraries():
                for rule_step in (0, 17, 300):
                    with self.subTest(family=family, rule_step=rule_step):
                        model = rain_model(root, schedule, duration=duration, start=datetime(2020, 1, 30, 3, 17),
                                           route_step=.5 if not rule_step else 31, rule_step=rule_step)
                        rows = self.replay(lib, root, model, (.125, 100.5, 3e38), duration)
                        self.assert_history(rows, schedule, rule_step=rule_step)

    def test_more_than_48_hours_can_advance_without_an_hour_by_hour_loop(self):
        step = 53*3600; duration = 54*3600
        schedule = ((0., 2.), (step, 2.), (2*step, 0.))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for family, lib in self.libraries():
                with self.subTest(family=family):
                    model = rain_model(root, schedule, duration=duration, route_step=step,
                                       start=datetime(2020, 1, 5))
                    model.update_options(wet_step=timedelta(seconds=step), dry_step=timedelta(seconds=step))
                    rows = self.replay(lib, root, model, (3e38,), duration)
                    self.assertEqual(len(rows), 2)
                    self.assert_history(rows, schedule)

    def test_exhausted_cache_does_not_publish_rainfall_at_failed_routing_time(self):
        duration = 120; schedule = ((0., 1.), (30., 3.), (60., 5.), (90., 2.), (120., 0.))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for family, lib in self.libraries():
                with self.subTest(family=family):
                    model = rain_model(root, schedule, duration=duration, route_step=30)
                    # Property 100 exposes reportRainfall, so publish the first
                    # successful step before checking the failed step's value.
                    model.update_options(report_step=timedelta(seconds=30))
                    cache = root/'runoff.bin'; cache.write_bytes(cache_bytes(model, (30.,)))
                    inp = root/'model.inp'; inp.write_text(controlled_source(model)+f'[FILES]\nUSE RUNOFF "{cache}"\n')
                    lib.swmm_getValue.argtypes = [ctypes.c_int, ctypes.c_int]
                    lib.swmm_getValue.restype = ctypes.c_double
                    elapsed = ctypes.c_double()
                    try:
                        self.assertEqual(lib.swmm_open(os.fsencode(inp), os.fsencode(root/'model.rpt'), os.fsencode(root/'model.out')), 0)
                        self.assertEqual(lib.swmm_start(1), 0)
                        self.assertEqual(lib.swmm_step(ctypes.byref(elapsed)), 0)
                        self.assertEqual(lib.swmm_getValue(100, 0), 3.)
                        self.assertEqual(lib.swmm_step(ctypes.byref(elapsed)), 327)
                        self.assertEqual(lib.swmm_getValue(100, 0), 3.)
                    finally: lib.swmm_close()
