"""Snow owner restoration and initialization; not whole-process solver recovery."""
import ctypes as c
from dataclasses import replace
from datetime import date, timedelta, time
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

from test_climate_v2 import climate_model
from test_native_v2_climate import SNOW_CATCHMENT
from test_native_v2_checkpoint_groundwater import Engine as GroundwaterEngine
from test_native_v2_checkpoint_groundwater import OPEN, fixture as groundwater
from test_native_v2_checkpoint_controls import fixture as controls
from test_native_v2_standard_io import handles
from easysewer.io.inp import InpDocument
from easysewer.model import Model
from easysewer.model import climate as climate_types, hydrology as hydrology_types
from easysewer.model.resources import SeriesPoint
from test_climate_v2 import add_series
from test_quality_v2 import ref


def fixture(impervious=30, plowable=.5):
    model = climate_model()
    model.update_options(end_date=model.options.start_date, end_time=time(6),
                         wet_step=timedelta(minutes=1), report_step=timedelta(minutes=5))
    return model.to_document().text + SNOW_CATCHMENT.replace(
        'S R J 2 30', 'S R J 2 '+str(impervious)).replace('1 0 .5\n', '1 0 '+str(plowable)+'\n')


class Engine(GroundwaterEngine):
    def __init__(self, *args):
        super().__init__(*args)
        f = self.lib.es_test_snow_reinitialize
        f.argtypes, f.restype = [c.c_int, c.POINTER(c.c_double)], c.c_int
        f = self.lib.es_test_snow_immediate_poison
        f.argtypes, f.restype = [], None
        for name, args, result in (
            ('es_test_snow_save',[c.c_void_p,c.c_size_t,c.POINTER(c.c_size_t),c.c_int],c.c_int),
            ('es_test_snow_restore',[c.c_void_p,c.c_size_t,c.c_int,OPEN,c.c_int,c.POINTER(c.c_int),c.POINTER(c.c_int)],c.c_int),
            ('es_test_snow_change',[c.c_int,c.c_int],None),
            ('es_test_snow_observe',[c.c_int,c.POINTER(c.c_double)],c.c_int),
        ):
            f = getattr(self.lib,name); f.argtypes,f.restype = args,result

    def dump(self, only=False):
        size = c.c_size_t()
        self.check(self.lib.es_test_snow_save(None,0,c.byref(size),only))
        data = c.create_string_buffer(size.value)
        self.check(self.lib.es_test_snow_save(data,size.value,c.byref(size),only))
        return data.raw

    def restore(self, raw, fail_at=-1, only=False):
        cleanup,committed = c.c_int(),c.c_int()
        result = self.lib.es_test_snow_restore(c.create_string_buffer(bytes(raw)),len(raw),fail_at,
                                             self.provider,only,c.byref(cleanup),c.byref(committed))
        self.cleanup,self.committed = cleanup.value,bool(committed.value)
        self.calls,self.hit = self.lib.es_test_tables_calls(),self.lib.es_test_tables_hit()
        return result

    def observe(self):
        result = []
        for index in range(2):
            values = (c.c_double*24)()
            count = self.lib.es_test_snow_observe(index,values)
            result.extend(values[:count])
        return result


def process_fixture(*, units='CFS', removal=None, multiple=False, zero_area=False,
                    ignore=False, lid=None, initial=1, full_cover=4, partial=False,
                    impervious=30, plowable=.5, abrupt_thaw=False):
    model = Model.from_document(InpDocument.from_text(fixture(impervious,plowable)),strict=True)
    model.update_options(start_date=date(2020,1,31),start_time=time(18),
        report_start_date=date(2020,1,31),report_start_time=time(18),
        end_date=date(2020,2,1),end_time=time(6),ignore_snowmelt=ignore)
    # Relative series start at 18:00; freeze, new snow on partial cover, thaw,
    # rain-on-snow, refreeze and a second thaw cross a daily coefficient update.
    air = add_series(model,'Air',((0,15),(1,26),(3,26),(4,60),(7,60),(8,10),(9,10),(10,50),(12,50)))
    if abrupt_thaw:
        # A gradual warming can exhaust cold content below the melt threshold.
        # Keep a cold pack until immediately before a rapid warm transition so
        # retained heat deficit actually delays melt above the threshold.
        model.timeseries.update('Air',points=tuple(SeriesPoint(time=timedelta(hours=h),value=v)
            for h,v in ((0,15),(1,26),(3,26),(4,60),(7,60),(8,10),(9-1/3600,10),(9,50),(12,50))))
    model.update_climate(temperature=climate_types.SeriesTemperature(series=air),
        snowmelt=climate_types.Snowmelt(snowfall_temperature=34,antecedent_weight=.5,
            negative_melt_ratio=.6,elevation=150,latitude=35,solar_time_correction=-10),
        impervious_depletion=climate_types.ArealDepletion(fractions=tuple(i/10 for i in range(10))),
        pervious_depletion=climate_types.ArealDepletion(fractions=tuple(i/10 for i in range(10))))
    model.raingages.update('R',interval=timedelta(minutes=5))
    model.timeseries.update('Rain',points=tuple(SeriesPoint(time=timedelta(hours=h),value=v)
        for h,v in ((0,.1),(2,0),(3,.1),(3.5,0),(5,.1),(6,0),(9,.2),(9.5,0),(12,0))))
    snow = model.snowpacks['Snow']
    snow = replace(snow,plowable=replace(snow.plowable,initial_snow=initial),
        impervious=replace(snow.impervious,initial_snow=initial,full_cover_depth=full_cover),
        pervious=replace(snow.pervious,initial_snow=initial,full_cover_depth=full_cover))
    model.snowpacks.replace('Snow',snow)
    if partial: model.snowpacks.update('Snow',plowable=None,impervious=None)
    if multiple or removal:
        model.snowpacks.add(replace(snow,id='Other',removal=None,
            pervious=replace(snow.pervious,minimum_melt=.002,maximum_melt=.006)))
        model.subcatchments.add(replace(model.subcatchments['S'],id='Second',snowpack=ref('snowpacks','Other')))
    if removal:
        fields=('out_of_system','to_impervious','to_pervious','immediate_melt','to_subcatchment')
        selected=dict(zip(('out','impervious','pervious','immediate','other'),fields))
        rates={f:(.1 if removal=='all' else .6 if f==selected[removal] else 0) for f in fields}
        model.snowpacks.update('Snow',removal=hydrology_types.SnowRemoval(threshold=.8,
            **rates,destination=ref('subcatchments','Second') if rates['to_subcatchment'] else None))
    if zero_area: model.subcatchments.update('S',area=0)
    if lid:
        from test_lid_v2 import control,usage
        model.lid_controls.add(replace(control(lid),removals=()))
        model.lid_usage.add(usage(to_pervious=True))
    if units!='CFS': model.convert_units(units)
    return model.to_document().text+'[REPORT]\nSUBCATCHMENTS ALL\nNODES ALL\nLINKS ALL\n'


def layout(raw):
    start = raw.index(b'ESSNW001'); pos=start+8; bindings=[]; floats=[]
    def integer():
        nonlocal pos
        value=struct.unpack_from('<i',raw,pos)[0];bindings.append(pos);pos+=4
        return value
    def identity():
        nonlocal pos
        count=integer()
        if count:bindings.append(pos)
        pos+=count
    def number(fixed=True):
        nonlocal pos
        (bindings if fixed else floats).append(pos);pos+=8
    melts=integer();catchments=integer();integer();integer();integer()
    for _ in range(melts):
        identity();number();number()
        if integer()>=0:identity()
        for _ in range(5):number()
        for _ in range(3):
            for _ in range(7):number()
            number(False)
    for _ in range(catchments):
        identity()
        if not integer():continue
        integer();number();number()
        for _ in range(3):
            number()
            for _ in range(7):number(False)
    assert pos==len(raw),(pos,len(raw))
    return dict(start=start,bindings=bindings,floats=floats)


@unittest.skipUnless(os.environ.get('EASYSEWER_CHECKPOINT_STANDARD') and
                     os.environ.get('EASYSEWER_CHECKPOINT_CUSTOM'), 'Requires snow candidate libraries')
class NativeCheckpointSnowTests(unittest.TestCase):
    def engine(self, family, root, source):
        return Engine(os.environ['EASYSEWER_CHECKPOINT_'+family.upper()], root, source)

    def run_case(self, family, source, action='none'):
        with tempfile.TemporaryDirectory() as root:
            e = self.engine(family,root,source); history=[];changed=False;elapsed=0
            try:
                for _ in range(10000):
                    raw=e.dump();layout(raw)
                    if action=='restore':
                        e.lib.es_test_climate_bundle_poison();e.lib.es_test_frames_poison(0)
                        e.lib.es_test_groundwater_poison(1);e.lib.es_test_groundwater_poison(2)
                        e.lib.es_test_snow_change(0,1)
                        e.lib.es_test_tables_drop();e.lib.es_test_streams_drop()
                        self.assertEqual(e.restore(raw),0);self.assertTrue(e.committed)
                        self.assertEqual(e.cleanup,0);self.assertEqual(e.dump(),raw)
                    elif action=='scratch':e.lib.es_test_snow_immediate_poison()
                    elif action.startswith('omit') and not changed and elapsed*86400>3600:
                        group=int(action[-1]);values=e.observe()
                        ready=group in (1,4)
                        # Discard cold content just before the second thaw,
                        # and depletion history after the last snowfall of
                        # the first thaw. Earlier omissions get overwritten.
                        if group==2:ready=elapsed*86400>=32040 and any(values[i]>0 for i in (2,10,18))
                        if group==3:ready=elapsed*86400>=11700 and any(values[i+5]<1 and values[i+6]>values[i+5] for i in (8,16))
                        if ready:e.lib.es_test_snow_change(group,0);changed=True
                    elapsed=e.step()[0]
                    history.append((elapsed,e.lib.swmm_getValue(303,0),*e.observe()))
                    if not elapsed:break
                else:self.fail('Snow process fixture did not finish')
                if action.startswith('omit'):self.assertTrue(changed,'Omission state was not activated')
            finally:e.close()
            report=re.sub(rb'(?m)^[ \t]*(?:Analysis (?:begun|ended)|Total elapsed time)[^\r\n]*',
                          b'',e.paths[1].read_bytes())
            return history,e.paths[2].read_bytes(),report

    def test_snow_states_restore_through_freeze_thaw_removal_and_boundaries(self):
        cases=[dict(units=units,removal='all') for units in ('CFS','GPM','MGD','CMS','LPS','MLD')]
        cases += [dict(removal=kind) for kind in ('out','impervious','pervious','immediate','other')]
        cases += [dict(),dict(multiple=True),dict(zero_area=True),dict(ignore=True),dict(lid='BC'),
                  dict(lid='RB'),dict(initial=0),dict(full_cover=0),dict(partial=True),dict(abrupt_thaw=True)]
        cases += [dict(impervious=i,plowable=p) for i,p in product((0,30,100),(0,1))]
        for family in ('standard','custom'):
            for options in cases:
                with self.subTest(family=family,options=options):
                    source=process_fixture(**options)
                    self.assertEqual(self.run_case(family,source),self.run_case(family,source,'restore'))
            for source in (groundwater(),controls()):
                self.assertEqual(self.run_case(family,source),self.run_case(family,source,'restore'))

    def test_all_four_persistent_groups_affect_future_results(self):
        for family in ('standard','custom'):
            source=process_fixture();expected=self.run_case(family,source)
            for group in range(1,5):
                with self.subTest(family=family,group=group):
                    current=process_fixture(abrupt_thaw=True) if group==2 else source
                    baseline=self.run_case(family,current) if group==2 else expected
                    observed=self.run_case(family,current,'omit'+str(group))
                    self.assertTrue(observed[1:]!=baseline[1:],'Omitted state did not change OUT or RPT')
            source=process_fixture(removal='all',multiple=True)
            self.assertEqual(self.run_case(family,source),self.run_case(family,source,'scratch'))

    def test_corruption_and_failed_staging_do_not_change_any_owner(self):
        for family in ('standard','custom'):
            with tempfile.TemporaryDirectory() as root:
                e=self.engine(family,root,process_fixture(removal='all',multiple=True))
                try:
                    for _ in range(7):e.step()
                    raw=e.dump();fields=layout(raw);before=handles()
                    def reject(bad):
                        calls=e.provider_calls
                        self.assertNotEqual(e.restore(bad),0);self.assertFalse(e.committed)
                        self.assertEqual(e.provider_calls,calls);self.assertEqual(e.dump(),raw)
                        self.assertEqual(handles(),before)
                    for size in range(fields['start'],len(raw)):reject(raw[:size])
                    reject(raw+b'x')
                    for offset in fields['bindings']:
                        bad=bytearray(raw);bad[offset]^=1;reject(bad)
                    for offset in fields['floats']:
                        for value in (float('nan'),float('inf'),-float('inf')):
                            bad=bytearray(raw);struct.pack_into('<d',bad,offset,value);reject(bad)
                    self.assertEqual(e.restore(raw),0);count=e.calls
                    for fail_at in range(count):
                        result=e.restore(raw,fail_at)
                        if e.committed:self.assertEqual((result,e.cleanup),(0,7))
                        else:self.assertIn(result,(6,7))
                        self.assertEqual(e.dump(),raw);self.assertEqual(handles(),before)
                        self.assertEqual(e.restore(raw),0)
                finally:e.close()

    def test_new_process_reconstructs_only_the_snow_owner(self):
        for family,units in product(('standard','custom'),('CFS','CMS')):
            with tempfile.TemporaryDirectory() as directory:
                root=Path(directory);source=process_fixture(units=units,removal='all',multiple=True)
                (root/'model.inp').write_text(source);(root/'first').mkdir();(root/'second').mkdir()
                e=self.engine(family,root/'first',source)
                try:
                    for _ in range(50):e.step()
                    raw=e.dump(only=True);(root/'saved.bin').write_bytes(raw);expected=e.observe()
                finally:e.close()
                code="""import json,sys
from pathlib import Path
from test_native_v2_checkpoint_snow import Engine
library,source,saved,dest=sys.argv[1:]
e=Engine(library,dest,Path(source).read_text())
try:
 initial=e.observe();raw=Path(saved).read_bytes()
 e.lib.es_test_snow_change(0,1)
 assert e.restore(raw,only=True)==0 and e.committed and e.cleanup==0
 assert e.dump(only=True)==raw and e.observe()!=initial
 Path(dest,'observed.json').write_text(json.dumps(e.observe()))
finally:e.close()
"""
                env=dict(os.environ,PYTHONPATH=os.pathsep.join((str(Path(__file__).parent),str(Path(__file__).parents[1]/'src'))))
                result=subprocess.run([sys.executable,'-B','-c',code,os.environ['EASYSEWER_CHECKPOINT_'+family.upper()],
                    str(root/'model.inp'),str(root/'saved.bin'),str(root/'second')],env=env,capture_output=True,text=True,timeout=60)
                self.assertEqual(result.returncode,0,result.stdout+result.stderr)
                self.assertEqual(json.loads((root/'second/observed.json').read_text()),expected)

    def test_initialization_overwrites_all_three_previously_uninitialized_arrays(self):
        for family, impervious, plowable in product(('standard','custom'), (0,30,100), (0,.5,1)):
            with self.subTest(family=family,impervious=impervious,plowable=plowable):
                with tempfile.TemporaryDirectory() as root:
                    e = self.engine(family, root, fixture(impervious,plowable))
                    try:
                        values = (c.c_double*9)()
                        self.assertEqual(e.lib.es_test_snow_reinitialize(0,values),9)
                        self.assertEqual(list(values),[0.]*9)
                    finally: e.close()

    def test_immediate_melt_is_rebuilt_even_for_zero_area_surfaces(self):
        def run(family, source, poison):
            with tempfile.TemporaryDirectory() as root:
                e = self.engine(family, root, source); history = []
                try:
                    for _ in range(10000):
                        if poison: e.lib.es_test_snow_immediate_poison()
                        elapsed = e.step()[0]
                        history.append((elapsed,e.lib.swmm_getValue(303,0)))
                        if not elapsed: break
                    else: self.fail('Snow fixture did not finish')
                finally: e.close()
                report = re.sub(rb'(?m)^[ \t]*(?:Analysis (?:begun|ended)|Total elapsed time)[^\r\n]*',
                                b'',e.paths[1].read_bytes())
                return history,e.paths[2].read_bytes(),report
        for family, impervious, plowable in product(('standard','custom'), (0,30,100), (0,.5,1)):
            with self.subTest(family=family,impervious=impervious,plowable=plowable):
                source = fixture(impervious,plowable)
                self.assertEqual(run(family,source,False),run(family,source,True))
