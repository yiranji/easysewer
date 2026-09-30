"""Groundwater state/statistics and independent K-expression correction checks.

Restoring this owner in a new process does not restore the rest of the solver.
"""
import ctypes as c
from dataclasses import replace
from itertools import product
import json
import math
import os
from pathlib import Path
import re
import struct
import subprocess
import sys
import tempfile
import unittest

from easysewer.model import Model
from easysewer.io.inp import InpDocument
from easysewer.io.inp.groundwater import GroundwaterExpressionCodec
from test_quality_v2 import ref
from test_native_v2_checkpoint_catchment import fixture as catchment
from test_native_v2_checkpoint_controls import fixture as controls
from test_native_v2_checkpoint_frames import Engine as FrameEngine, OPEN
from test_native_v2_standard_io import handles


class Engine(FrameEngine):
    def __init__(self, *args):
        super().__init__(*args)
        for name, args, result in (
            ('es_test_groundwater_save', [c.c_void_p, c.c_size_t, c.POINTER(c.c_size_t), c.c_int], c.c_int),
            ('es_test_groundwater_restore', [c.c_void_p, c.c_size_t, c.c_int, OPEN, c.c_int,
                                             c.POINTER(c.c_int), c.POINTER(c.c_int)], c.c_int),
            ('es_test_groundwater_poison', [c.c_int], None),
            ('es_test_groundwater_reset', [c.c_int], None),
            ('es_test_groundwater_observe', [c.c_int, c.POINTER(c.c_double)], c.c_int),
            ('es_test_groundwater_percolation', [c.c_double]*7+[c.POINTER(c.c_double)], c.c_double),
        ):
            f = getattr(self.lib, name); f.argtypes, f.restype = args, result

    def dump(self, only=False):
        size = c.c_size_t()
        self.check(self.lib.es_test_groundwater_save(None, 0, c.byref(size), only))
        data = c.create_string_buffer(size.value)
        self.check(self.lib.es_test_groundwater_save(data, size.value, c.byref(size), only))
        return data.raw

    def restore(self, raw, fail_at=-1, only=False):
        cleanup, committed = c.c_int(), c.c_int()
        error = self.lib.es_test_groundwater_restore(c.create_string_buffer(bytes(raw)), len(raw),
            fail_at, self.provider, only, c.byref(cleanup), c.byref(committed))
        self.cleanup, self.committed = cleanup.value, bool(committed.value)
        self.calls, self.hit = self.lib.es_test_tables_calls(), self.lib.es_test_tables_hit()
        return error

    def observe(self):
        result = []
        for index in range(2):
            values = (c.c_double*15)()
            count = self.lib.es_test_groundwater_observe(index, values)
            result.extend(values[:count])
        return result


def fixture(*, units='CFS', moisture=.3, dry=False, expressions='original', overrides=(),
            multiple=False, impervious=False, zero_area=False, ignore=False, lid=None, reverse=False):
    model = Model.from_document(InpDocument.from_text(catchment(groundwater=True, lid=lid)), strict=True)
    model.aquifers.update('Aquifer', upper_moisture=moisture)
    if overrides:
        model.groundwater.update('S', **{name: value for name, value, flag in zip(
            ('threshold_elevation','bottom_elevation','water_table_elevation','upper_moisture'),
            (-1,-12,3,.35), overrides) if flag})
    if impervious: model.subcatchments.update('S', impervious_percent=100)
    if zero_area: model.subcatchments.update('S', area=0)
    if ignore: model.update_options(ignore_groundwater=True)
    if reverse:
        model.groundwater.update('S', fixed_surface_depth=10, surface_water_coefficient=.01,
                                groundwater_coefficient=0, interaction_coefficient=0)
    if dry:
        model.timeseries.update('Rain', points=tuple(replace(p, value=0) for p in model.timeseries['Rain'].points))
    codec = GroundwaterExpressionCodec()
    if expressions == 'none':
        model.gwf.remove(('S', 'DEEP')); model.gwf.remove(('S', 'LATERAL'))
    elif expressions in ('K', 'oracle'):
        expression = 'K' if expressions == 'K' else 'KS * EXP((THETA - PHI) * 10)'
        model.gwf.update(('S','DEEP'), expression=codec.parse(expression))
        model.gwf.update(('S','LATERAL'), expression=codec.parse('0.0001 * '+expression))
    if multiple:
        model.subcatchments.add(replace(model.subcatchments['S'], id='Second'))
        model.aquifers.add(replace(model.aquifers['Aquifer'], id='SecondAquifer',
                                  upper_moisture=.4, conductivity=.4, conductivity_slope=20))
        model.groundwater.add(replace(model.groundwater['S'], subcatchment=ref('subcatchments','Second'),
                                     aquifer=ref('aquifers','SecondAquifer')))
        for kind in ('DEEP', 'LATERAL'):
            if ('S',kind) in model.gwf:
                value = replace(model.gwf[('S',kind)], subcatchment=ref('subcatchments','Second'))
                if expressions == 'oracle':
                    value = replace(value, expression=codec.parse(('0.0001 * ' if kind == 'LATERAL' else '')+
                                                                  'KS * EXP((THETA - PHI) * 20)'))
                model.gwf.add(value)
    if units != 'CFS': model.convert_units(units)
    text = model.to_document().text
    if dry: text += '[EVAPORATION]\nCONSTANT 0\n'
    return text


def layout(raw):
    start = raw.index(b'ESGWT001'); pos = start+8
    bindings, floats, numeric_groups, stats_groups = [], [], [], []
    def integer():
        nonlocal pos
        result = struct.unpack_from('<i', raw, pos)[0]
        bindings.append(pos); pos += 4
        return result
    def text():
        nonlocal pos
        count = integer()
        if count: bindings.append(pos)
        pos += count
    def number(fixed=True):
        nonlocal pos
        (bindings if fixed else floats).append(pos); pos += 8
    count = integer(); aquifers = integer(); integer(); integer(); integer()
    for _ in range(aquifers):
        text()
        for _ in range(12): number()
        pattern = integer()
        if pattern >= 0:
            text(); integer(); integer()
            for _ in range(12): number()
    for _ in range(count):
        text()
        if not integer(): continue
        integer(); integer(); text()
        for _ in range(13): number()
        for _ in range(2):
            for _ in range(integer()): integer(); integer(); number()
        begin = pos
        for _ in range(5): number(False)
        if integer(): number(False)
        numeric_groups.append((begin,pos))
        begin = pos
        for _ in range(9): number(False)
        stats_groups.append((begin,pos))
    assert pos == len(raw), (pos,len(raw))
    return dict(start=start, bindings=bindings, floats=floats,
                numeric_groups=numeric_groups, stats_groups=stats_groups)


@unittest.skipUnless(os.environ.get('EASYSEWER_CHECKPOINT_STANDARD') and os.environ.get('EASYSEWER_CHECKPOINT_CUSTOM'),
                     'Requires groundwater checkpoint candidate libraries')
class NativeCheckpointGroundwaterTests(unittest.TestCase):
    def engine(self, family, root, text):
        return Engine(os.environ['EASYSEWER_CHECKPOINT_'+family.upper()], root, text)

    def run_case(self, family, text, action='none'):
        with tempfile.TemporaryDirectory() as directory:
            e = self.engine(family, directory, text); history = []
            try:
                for index in range(10000):
                    raw = e.dump(); layout(raw)
                    if action == 'restore':
                        e.lib.es_test_climate_bundle_poison(); e.lib.es_test_frames_poison(0)
                        e.lib.es_test_groundwater_poison(1); e.lib.es_test_groundwater_poison(2)
                        e.lib.es_test_tables_drop(); e.lib.es_test_streams_drop()
                        self.assertEqual(e.restore(raw), 0); self.assertTrue(e.committed)
                        self.assertEqual(e.cleanup, 0); self.assertEqual(e.dump(), raw)
                    elif action == 'scratch': e.lib.es_test_groundwater_poison(0)
                    elif action.startswith('omit') and index == 20:
                        e.lib.es_test_groundwater_reset(int(action[-1]))
                    now = e.step()[0]
                    history.append((now, e.lib.swmm_getValue(303,0), *e.observe()))
                    if not now: break
                else: self.fail('Groundwater fixture did not finish')
            finally: e.close()
            report = re.sub(rb'(?m)^[ \t]*(?:Analysis (?:begun|ended)|Total elapsed time)[^\r\n]*', b'', e.paths[1].read_bytes())
            return history, e.paths[2].read_bytes(), report

    def test_K_is_current_conductivity_even_without_percolation(self):
        for family in ('standard', 'custom'):
            with tempfile.TemporaryDirectory() as directory:
                e = self.engine(family, directory, fixture())
                try:
                    for theta, depth, slope in product((.4,.25,.2,.1), (10.,.001,0.,-1.), (0.,10.,20.)):
                        conductivity = c.c_double()
                        result = e.lib.es_test_groundwater_percolation(theta,depth,.45,.2,slope,.25,15.,c.byref(conductivity))
                        expected = .2*math.exp((theta-.45)*slope)
                        self.assertTrue(math.isclose(conductivity.value,expected,rel_tol=1e-14,abs_tol=1e-16))
                        flow = expected*(1+15*2*(theta-.25)/depth) if theta>.25 and depth>0 else 0
                        self.assertTrue(math.isclose(result,flow,rel_tol=1e-14,abs_tol=1e-16))
                finally: e.close()

    def test_K_matches_explicit_expression_and_repeated_projects(self):
        for family in ('standard', 'custom'):
            for units in ('CFS','CMS'):
                for multiple in (False,True):
                    dry = fixture(units=units,moisture=.2,dry=True,expressions='K',multiple=multiple)
                    first = self.run_case(family,dry)
                    self.run_case(family,fixture(units=units,moisture=.4,expressions='K'))
                    self.assertEqual(first,self.run_case(family,dry))
                    oracle = self.run_case(family,fixture(units=units,moisture=.2,dry=True,expressions='oracle',multiple=multiple))
                    # Algebraically identical expressions differ in native unit
                    # multiplication order; compare every internal trajectory
                    # value at a predeclared floating-point tolerance.
                    self.assertEqual(len(first[0]),len(oracle[0]))
                    for left,right in zip(first[0],oracle[0]):
                        self.assertEqual(len(left),len(right))
                        for a,b in zip(left,right):
                            self.assertTrue(math.isclose(a,b,rel_tol=1e-10,abs_tol=1e-10),(a,b))

    def test_full_state_and_statistics_restore_across_configurations(self):
        cases = [dict(units=units,overrides=flags) for units in ('CFS','CMS') for flags in product((False,True),repeat=4)]
        cases += [dict(moisture=.2,expressions='K',dry=True),dict(moisture=.4,expressions='K',multiple=True),
                  dict(impervious=True),dict(zero_area=True),dict(ignore=True),dict(expressions='none'),
                  dict(lid='BC'),dict(lid='RB'),dict(reverse=True),dict(multiple=True)]
        for family in ('standard','custom'):
            for options in cases:
                with self.subTest(family=family,options=options):
                    text = fixture(**options)
                    self.assertEqual(self.run_case(family,text,'restore'),self.run_case(family,text))
            for text in (catchment(),controls()):
                self.assertEqual(self.run_case(family,text,'restore'),self.run_case(family,text))

    def test_scratch_is_rebuilt_and_both_persistent_groups_are_needed(self):
        for family in ('standard','custom'):
            for options in (dict(),dict(moisture=.2,dry=True,expressions='K'),dict(multiple=True,expressions='K'),
                            dict(lid='BC'),dict(ignore=True),dict(impervious=True)):
                text = fixture(**options); expected = self.run_case(family,text)
                self.assertEqual(expected,self.run_case(family,text,'scratch'))
            for group in (1,2):
                text = fixture()
                self.assertNotEqual(self.run_case(family,text),self.run_case(family,text,'omit'+str(group)))

    def test_bad_payload_and_resource_stage_failures_are_atomic(self):
        for family in ('standard','custom'):
            with tempfile.TemporaryDirectory() as root:
                e = self.engine(family,root,fixture(multiple=True))
                try:
                    for _ in range(7): e.step()
                    raw = e.dump(); fields = layout(raw); before = handles()
                    def reject(bad):
                        calls = e.provider_calls
                        self.assertNotEqual(e.restore(bad),0); self.assertFalse(e.committed)
                        self.assertEqual(e.provider_calls,calls); self.assertEqual(e.dump(),raw)
                        self.assertEqual(handles(),before)
                    for size in range(fields['start'],len(raw)): reject(raw[:size])
                    reject(raw+b'x')
                    for offset in fields['bindings']:
                        bad = bytearray(raw); bad[offset] ^= 1; reject(bad)
                    for offset in fields['floats']:
                        for value in (float('nan'),float('inf'),-float('inf')):
                            bad = bytearray(raw); struct.pack_into('<d',bad,offset,value); reject(bad)
                    self.assertEqual(e.restore(raw),0); count = e.calls
                    for fail_at in range(count):
                        result = e.restore(raw,fail_at)
                        if e.committed: self.assertEqual((result,e.cleanup),(0,7))
                        else: self.assertIn(result,(6,7))
                        self.assertEqual(e.dump(),raw); self.assertEqual(handles(),before)
                        self.assertEqual(e.restore(raw),0)
                finally: e.close()

    def test_new_process_reconstructs_groundwater_owner(self):
        for family,units in product(('standard','custom'),('CFS','CMS')):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory); text = fixture(units=units,multiple=True,expressions='K')
                (root/'model.inp').write_text(text); (root/'first').mkdir(); (root/'second').mkdir()
                e = self.engine(family,root/'first',text)
                try:
                    for _ in range(35): e.step()
                    raw = e.dump(only=True); (root/'saved.bin').write_bytes(raw); expected = e.observe()
                finally: e.close()
                code = """import json,sys
from pathlib import Path
from test_native_v2_checkpoint_groundwater import Engine
library,source,saved,dest=sys.argv[1:]
e=Engine(library,dest,Path(source).read_text())
try:
 initial=e.observe();raw=Path(saved).read_bytes()
 e.lib.es_test_groundwater_poison(1);e.lib.es_test_groundwater_poison(2)
 assert e.restore(raw,only=True)==0 and e.committed and e.cleanup==0
 assert e.dump(only=True)==raw and e.observe()!=initial
 Path(dest,'observed.json').write_text(json.dumps(e.observe()))
finally:e.close()
"""
                env = dict(os.environ,PYTHONPATH=os.pathsep.join((str(Path(__file__).parent),str(Path(__file__).parents[1]/'src'))))
                p = subprocess.run([sys.executable,'-B','-c',code,os.environ['EASYSEWER_CHECKPOINT_'+family.upper()],
                    str(root/'model.inp'),str(root/'saved.bin'),str(root/'second')],env=env,capture_output=True,text=True,timeout=60)
                self.assertEqual(p.returncode,0,p.stdout+p.stderr)
                self.assertEqual(json.loads((root/'second'/'observed.json').read_text()),expected)
