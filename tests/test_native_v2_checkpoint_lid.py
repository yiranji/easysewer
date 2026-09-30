"""LID numeric/report-cache owner tests, not whole-process/output continuation."""
import ctypes as c
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from itertools import product
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import sys
import tempfile
import unittest

from easysewer.io.inp import InpDocument
from easysewer.model import FileReference, Model
from easysewer.model.lid import DisabledLidUsage
from easysewer.model.resources import Curve, CurvePoint, SeriesPoint
from test_lid_v2 import lid_model, usage
from test_quality_v2 import ref
from test_native_v2_checkpoint_snow import Engine as SnowEngine, OPEN
from test_native_v2_lid_report_io import cycle_fixture
from test_native_v2_standard_io import handles

EVIDENCE = []


class Engine(SnowEngine):
    def __init__(self, *args):
        super().__init__(*args)
        for name, args, result in (
            ('es_test_lid_save', [c.c_void_p, c.c_size_t, c.POINTER(c.c_size_t), c.c_int], c.c_int),
            ('es_test_lid_restore', [c.c_void_p, c.c_size_t, c.c_int, OPEN, c.c_int,
                                     c.POINTER(c.c_int), c.POINTER(c.c_int)], c.c_int),
            ('es_test_lid_change', [c.c_int, c.c_int], None),
            ('es_test_lidproc_poison', [], None),
            ('es_test_lid_observe', [c.c_int, c.POINTER(c.c_double), c.c_void_p, c.POINTER(c.c_int)], c.c_int),
            ('es_test_lid_report_guard', [c.c_int], None),
        ):
            f = getattr(self.lib, name); f.argtypes, f.restype = args, result

    def dump(self, only=False):
        size = c.c_size_t()
        self.check(self.lib.es_test_lid_save(None, 0, c.byref(size), only))
        data = c.create_string_buffer(size.value)
        self.check(self.lib.es_test_lid_save(data, size.value, c.byref(size), only))
        return data.raw

    def restore(self, raw, fail_at=-1, only=False):
        cleanup, committed = c.c_int(), c.c_int()
        error = self.lib.es_test_lid_restore(c.create_string_buffer(bytes(raw)), len(raw),
            fail_at, self.provider, only, c.byref(cleanup), c.byref(committed))
        self.cleanup, self.committed = cleanup.value, bool(committed.value)
        self.calls, self.hit = self.lib.es_test_tables_calls(), self.lib.es_test_tables_hit()
        return error

    def observe(self):
        result = []
        for index in range(100):
            values = (c.c_double * 28)(); row = c.create_string_buffer(256); dry = c.c_int()
            count = self.lib.es_test_lid_observe(index, values, row, c.byref(dry))
            if not count:
                return result
            result.append([list(values[:count]), dry.value, row.value.decode('ascii')])
        raise AssertionError('Unexpected LID fixture size')


def fixture(root, kind='BC', *, units='CFS', variant='normal', report=True):
    root = Path(root)
    model = lid_model(kind)
    start = datetime(2020, 1, 30)
    duration = 120 if variant in ('regen', 'delay', 'hysteresis') else 30
    end = start + timedelta(minutes=duration)
    model.update_options(start_date=start.date(), start_time=time(),
        report_start_date=start.date(), report_start_time=time(),
        end_date=end.date(), end_time=end.time(), wet_step=timedelta(seconds=60),
        dry_step=timedelta(seconds=120), routing_step=timedelta(seconds=60),
        report_step=timedelta(seconds=60), variable_step=0)
    model.raingages.update('R', interval=timedelta(seconds=60))
    model.timeseries.update('Rain', points=tuple(SeriesPoint(time=timedelta(minutes=n), value=v)
        for n, v in ((0, 0), (2, 3), (7, 0), (15, 2), (20, 0), (duration, 0))))
    if variant == 'dry':
        model.timeseries.update('Rain', points=tuple(replace(p, value=0) for p in model.timeseries['Rain'].points))
    if variant == 'regen':
        ctrl = model.lid_controls['L']
        model.lid_controls.update('L', pavement=replace(ctrl.pavement, clogging_factor=.2,
            regeneration_days=.025, regeneration_fraction=.7))
    if variant in ('delay', 'hysteresis'):
        ctrl = model.lid_controls['L']
        model.lid_controls.update('L', drain=replace(ctrl.drain, coefficient=3, delay=.2,
            open_head=2, close_head=.5), storage=replace(ctrl.storage, covered=False))
    if variant == 'no-soil':
        model.lid_controls.update('L', soil=None)
    if variant == 'full':
        model.lid_usage.update('lid-usage-1', area=model.subcatchments['S'].area * 43560 / 2,
                               from_impervious=0, from_pervious=0)
    if variant in ('multiple', 'to-catchment', 'to-pervious', 'curve'):
        model.subcatchments.add(replace(model.subcatchments['S'], id='Other'))
        model.lid_controls.add(replace(model.lid_controls['L'], id='OtherDesign'))
        model.lid_usage.add(usage('second', area=70, initial_saturation=5))
        model.lid_usage.add(usage('third', subcatchment=ref('subcatchments', 'Other'),
                                 control=ref('lid_controls', 'OtherDesign'), number=1))
        if variant == 'to-catchment':
            model.lid_usage.update('lid-usage-1', drain_to=ref('subcatchments', 'Other'))
        if variant == 'to-pervious':
            model.lid_usage.update('lid-usage-1', to_pervious=True)
        if variant == 'curve':
            model.curves.add(Curve(id='DrainHead', kind='CONTROL',
                                  points=(CurvePoint(x=0, y=.1), CurvePoint(x=20, y=1))))
            model.lid_controls.update('L', drain=replace(model.lid_controls['L'].drain,
                                                         curve=ref('curves', 'DrainHead')))
    if variant == 'unused':
        model.lid_usage.remove('lid-usage-1')
    elif variant == 'disabled':
        model.lid_usage.remove('lid-usage-1')
        model.lid_usage.add(DisabledLidUsage(record_id='disabled', subcatchment=ref('subcatchments', 'S'),
            control=ref('lid_controls', 'L'), parameters=('bad', 'ignored', '0', '10', '1', 'unopened.txt')))
    elif variant == 'ignore':
        model.update_options(ignore_rainfall=True)
    for index, key in enumerate(model.lid_usage):
        if report and variant != 'no-report' and not isinstance(model.lid_usage[key], DisabledLidUsage):
            model.lid_usage.update(key, report_file=FileReference(path=str(root / f'detail-{index}.txt'), direction='output'))
    if units != 'CFS':
        model.convert_units(units)
    return model.to_document().text + '[REPORT]\nSUBCATCHMENTS ALL\nNODES ALL\nLINKS ALL\n'


def layout(raw):
    pos = raw.index(b'ESLID001') + 8
    start = pos - 8; bindings = []; floats = []; integers = []; rows = []
    def integer(fixed=True):
        nonlocal pos
        value = struct.unpack_from('<i', raw, pos)[0]
        (bindings if fixed else integers).append(pos); pos += 4
        return value
    def identity():
        nonlocal pos
        count = integer()
        if count: bindings.append(pos)
        pos += count
    def number(fixed=True):
        nonlocal pos
        value = struct.unpack_from('<d', raw, pos)[0]
        (bindings if fixed else floats).append(pos); pos += 8
        return value
    count = integer(); groups = integer(); pollutants = integer()
    integer(); integer(); integer()
    for _ in range(pollutants): identity()
    for _ in range(count):
        identity(); kind = integer()
        for _ in range(6): number()
        integer()
        for _ in range(18): number()  # pavement 7, soil 7, storage 4
        integer()
        coefficient = number()
        for _ in range(5): number()
        curve = integer()
        if coefficient > 0 and kind != 7 and curve >= 0: identity()
        for _ in range(4 + pollutants): number()
    for _ in range(groups):
        identity(); number(); number()
        if not integer(): continue
        number()
        for _ in range(3): number(False)
        for _ in range(integer()):
            integer(); integer()
            for _ in range(5): number()
            integer(); catchment = integer(); node = integer()
            if catchment >= 0: identity()
            if node >= 0: identity()
            conductivity = number()
            if conductivity > 0:
                for _ in range(3): number()
                for _ in range(4): number(False)
                integer(False)
            for _ in range(20): number(False)  # four layers, four fluxes, five histories, seven totals
            if integer():
                integer(False); offset = pos; length = integer(False)
                rows.append((offset, pos, length)); pos += length
    assert pos == len(raw), (pos, len(raw))
    return dict(start=start, bindings=bindings, floats=floats, integers=integers, rows=rows)


@unittest.skipUnless(os.environ.get('EASYSEWER_CHECKPOINT_STANDARD') and
                     os.environ.get('EASYSEWER_CHECKPOINT_CUSTOM'), 'Requires LID state test libraries')
class NativeCheckpointLidTests(unittest.TestCase):
    def engine(self, family, root, source):
        return Engine(os.environ['EASYSEWER_CHECKPOINT_' + family.upper()], root, source)

    def run_case(self, family, kind='BC', units='CFS', variant='normal', action='normal'):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            if variant == 'edges':
                source = cycle_fixture().replace('lid detail.txt', str(root / 'detail-0.txt')).replace(
                    'lid second.txt', str(root / 'detail-1.txt'))
            else:
                source = fixture(root, kind, units=units, variant=variant)
            e = self.engine(family, root, source); history = []; changed = False; elapsed = 0
            try:
                for _ in range(20000):
                    raw = e.dump(); layout(raw)
                    if action == 'restore':
                        e.lib.es_test_climate_bundle_poison(); e.lib.es_test_frames_poison(0)
                        e.lib.es_test_groundwater_poison(1); e.lib.es_test_groundwater_poison(2)
                        e.lib.es_test_snow_change(0, 1); e.lib.es_test_lid_change(0, 1)
                        e.lib.es_test_tables_drop(); e.lib.es_test_streams_drop()
                        self.assertEqual(e.restore(raw), 0); self.assertTrue(e.committed)
                        self.assertEqual(e.cleanup, 0); self.assertEqual(e.dump(), raw)
                    elif action == 'scratch':
                        e.lib.es_test_lid_change(8, 1); e.lib.es_test_lid_change(9, 1)
                        e.lib.es_test_lid_change(10, 1)
                        e.lib.es_test_lidproc_poison()
                        self.assertEqual(e.dump(), raw)
                    elif action.startswith('omit') and not changed:
                        group = int(action[-1])
                        ready = elapsed * 86400 >= (120 if variant == 'edges' else 600)
                        if ready:
                            e.lib.es_test_lid_change(group, 0); changed = True
                    step = e.step(); elapsed = step[0]
                    history.append((step, e.observe()))
                    if not elapsed: break
                else: self.fail('LID fixture did not finish')
            finally: e.close()
            report = re.sub(rb'(?m)^[ \t]*(?:Analysis (?:begun|ended)|Total elapsed time)[^\r\n]*',
                            b'', e.paths[1].read_bytes()).replace(os.fsencode(root), b'<workspace>')
            details = {p.name: p.read_bytes() for p in root.glob('detail-*.txt')}
            return history, e.paths[2].read_bytes(), report, details, changed

    def test_all_types_units_and_configurations_restore_complete_results(self):
        cases = [(kind, units, 'normal') for kind, units in product(('BC','RG','GR','IT','PP','RB','RD','VS'), ('CFS','CMS'))]
        cases += [(kind, 'CFS', variant) for kind, variant in (
            ('PP','regen'),('PP','no-soil'),('RB','delay'),('BC','hysteresis'),('BC','multiple'),
            ('RB','to-catchment'),('BC','to-pervious'),('BC','curve'),('BC','full'),('VS','full'),
            ('BC','unused'),('RB','disabled'),('BC','ignore'),('RB','dry'),('RB','edges'),
            ('BC','no-report'))]
        for family in ('standard','custom'):
            for kind, units, variant in cases:
                with self.subTest(family=family,kind=kind,units=units,variant=variant):
                    reference = self.run_case(family,kind,units,variant)
                    restored = self.run_case(family,kind,units,variant,'restore')
                    self.assertEqual(restored,reference)
                    EVIDENCE.append(dict(kind='restore',family=family,lid=kind,units=units,variant=variant))

    def test_scratch_and_inactive_infiltration_fields_do_not_affect_future_results(self):
        for family, kind in product(('standard','custom'), ('BC','RG','GR','IT','PP','RB','RD','VS')):
            with self.subTest(family=family,lid=kind):
                self.assertEqual(self.run_case(family,kind,action='scratch'), self.run_case(family,kind))
                EVIDENCE.append(dict(kind='scratch',family=family,lid=kind))

    def test_corruption_and_staging_failure_preserve_all_live_owners(self):
        for family in ('standard','custom'):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory); e = self.engine(family,root,fixture(root,variant='curve'))
                try:
                    for _ in range(12): e.step()
                    raw = e.dump(); fields = layout(raw); before = handles()
                    def reject(bad):
                        calls=e.provider_calls
                        self.assertNotEqual(e.restore(bad),0)
                        self.assertFalse(e.committed); self.assertEqual(e.provider_calls,calls)
                        self.assertEqual(e.dump(),raw); self.assertEqual(handles(),before)
                    for size in range(fields['start'],len(raw)): reject(raw[:size])
                    reject(raw+b'x')
                    for offset in fields['bindings']:
                        bad=bytearray(raw); bad[offset]^=1; reject(bad)
                    for offset in fields['floats']:
                        for value in (float('nan'),float('inf'),-float('inf')):
                            bad=bytearray(raw); struct.pack_into('<d',bad,offset,value); reject(bad)
                    for offset in fields['integers']:
                        bad=bytearray(raw); struct.pack_into('<i',bad,offset,-1); reject(bad)
                    for offset,begin,length in fields['rows']:
                        bad=bytearray(raw); struct.pack_into('<I',bad,offset,256); reject(bad)
                        if length:
                            bad=bytearray(raw); bad[begin]=0; reject(bad)
                    self.assertEqual(e.restore(raw),0)
                    for fail_at in range(e.calls):
                        error=e.restore(raw,fail_at)
                        if e.committed: self.assertEqual((error,e.cleanup),(0,7))
                        else: self.assertIn(error,(6,7))
                        self.assertEqual(e.dump(),raw); self.assertEqual(handles(),before)
                        self.assertEqual(e.restore(raw),0)
                finally: e.close()

    def test_report_cache_tail_and_success_boundary_are_explicit(self):
        for family in ('standard','custom'):
            with tempfile.TemporaryDirectory() as directory:
                root=Path(directory); e=self.engine(family,root,fixture(root))
                try:
                    for _ in range(5): e.step()
                    raw=e.dump(); observed=e.observe()
                    e.lib.es_test_lid_change(7,1)
                    self.assertEqual(e.restore(raw),0)
                    self.assertEqual(e.dump(),raw); self.assertEqual(e.observe(),observed)
                    # The restored terminator hides the poisoned unused tail.
                    for flag in (1,2,3):
                        e.lib.es_test_lid_report_guard(flag)
                        size=c.c_size_t()
                        self.assertEqual(e.lib.es_test_lid_save(None,0,c.byref(size),1),5)
                        e.lib.es_test_lid_report_guard(0)
                        self.assertEqual(e.dump(),raw)
                finally:
                    e.lib.es_test_lid_report_guard(0); e.close()

    def test_omitted_state_changes_future_science_statistics_or_report_rows(self):
        for family in ('standard','custom'):
            for kind, variant, group in (('BC','normal',1),('RB','delay',2),('PP','regen',3),
                                         ('BC','normal',4),('BC','normal',5),('RB','edges',7)):
                with self.subTest(family=family,lid=kind,variant=variant,group=group):
                    reference=self.run_case(family,kind,variant=variant)
                    omitted=self.run_case(family,kind,variant=variant,action='omit'+str(group))
                    self.assertTrue(omitted[-1])
                    self.assertNotEqual(omitted[1:4],reference[1:4])
                    EVIDENCE.append(dict(kind='omission',family=family,lid=kind,group=group))

    def test_new_process_reconstructs_only_lid_numeric_and_report_cache_owner(self):
        for family, kind in product(('standard','custom'),('PP','RB')):
            with tempfile.TemporaryDirectory() as directory:
                root=Path(directory); (root/'first').mkdir(); (root/'second').mkdir()
                source=fixture(root/'first',kind)
                e=self.engine(family,root/'first',source)
                try:
                    for _ in range(12): e.step()
                    raw=e.dump(only=True); expected=e.observe()
                finally: e.close()
                (root/'state.bin').write_bytes(raw)
                (root/'source.inp').write_text(source.replace(str(root/'first'),str(root/'second')))
                code="""import json,sys
from pathlib import Path
from test_native_v2_checkpoint_lid import Engine
library,source,state,destination=sys.argv[1:]
e=Engine(library,destination,Path(source).read_text())
try:
 raw=Path(state).read_bytes();initial=e.observe()
 e.lib.es_test_lid_change(0,1)
 assert e.restore(raw,only=True)==0 and e.committed and e.cleanup==0
 assert e.dump(only=True)==raw and e.observe()!=initial
 Path(destination,'observed.json').write_text(json.dumps(e.observe()))
finally:e.close()
"""
                env=dict(os.environ,PYTHONPATH=os.pathsep.join((str(Path(__file__).parent),str(Path(__file__).parents[1]/'src'))))
                p=subprocess.run([sys.executable,'-B','-c',code,os.environ['EASYSEWER_CHECKPOINT_'+family.upper()],
                    str(root/'source.inp'),str(root/'state.bin'),str(root/'second')],env=env,capture_output=True,text=True,timeout=60)
                self.assertEqual(p.returncode,0,p.stdout+p.stderr)
                self.assertEqual(json.loads((root/'second/observed.json').read_text()),expected)
                EVIDENCE.append(dict(kind='fresh-owner',family=family,lid=kind))
