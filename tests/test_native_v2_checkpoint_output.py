"""OUT numeric and finalized ponding owners, not output-file/process resume."""
import ctypes as c
from datetime import time, timedelta
import hashlib
import os
from pathlib import Path
import re
import struct
import subprocess
import sys
import tempfile
import unittest

from test_native_v2_checkpoint_balance import Engine as BalanceEngine, fixture as base_fixture, OPEN
from test_native_v2_ponding_accounting import sealed_model

EVIDENCE = []


class Engine(BalanceEngine):
    def __init__(self, *args):
        super().__init__(*args)
        for name, args, result in (
            ('es_test_output_save', [c.c_void_p,c.c_size_t,c.POINTER(c.c_size_t),c.c_int], c.c_int),
            ('es_test_output_restore', [c.c_void_p,c.c_size_t,c.c_int,OPEN,c.c_int,c.POINTER(c.c_int),c.POINTER(c.c_int)], c.c_int),
            ('es_test_output_change', [c.c_int,c.c_int], None),
            ('es_test_output_guard', [c.c_int,c.c_int], None),
            ('es_test_output_periods', [], c.c_int),
            ('swmm_getSavedValue', [c.c_int,c.c_int,c.c_int], c.c_double),
            ('swmm_getIndex', [c.c_int,c.c_char_p], c.c_int),
        ):
            f=getattr(self.lib,name);f.argtypes,f.restype=args,result
        if hasattr(self.lib,'es_test_ponding_change'):
            self.lib.es_test_ponding_change.argtypes=[c.c_int,c.c_int]
            self.lib.es_test_ponding_change.restype=None
            for name in ('swmm_getPondingStep','swmm_getCurrentTime','swmm_getRoutingDuration'):
                f=getattr(self.lib,name);f.argtypes=[];f.restype=c.c_double

    def dump(self, only=False):
        size=c.c_size_t()
        self.check(self.lib.es_test_output_save(None,0,c.byref(size),only))
        raw=c.create_string_buffer(size.value)
        self.check(self.lib.es_test_output_save(raw,size.value,c.byref(size),only))
        return raw.raw

    def restore(self, raw, fail_at=-1, only=False):
        cleanup,committed=c.c_int(),c.c_int()
        error=self.lib.es_test_output_restore(c.create_string_buffer(bytes(raw)),len(raw),fail_at,
            self.provider,only,c.byref(cleanup),c.byref(committed))
        self.cleanup,self.committed=cleanup.value,bool(committed.value)
        return error

    def damage(self):
        super().damage()
        self.lib.es_test_output_change(0,1)
        if hasattr(self.lib,'es_test_ponding_change'):self.lib.es_test_ponding_change(0,1)

    def split_step(self, units):
        self.check(self.lib.swmm_execRouting())
        dt=self.lib.swmm_getPondingStep()
        index=self.lib.swmm_getIndex(2,b'J')
        assert index>=0
        # The sealed node has maxDepth=0 and pondedArea=100 ft2. Its geometry
        # and flows are returned in the selected model units.
        # SWMM UCF(VOLUME)=.02832, independently of UCF(LENGTH)^3.
        area=100 if units=='CFS' else 100*.02832/.3048
        volume=self.lib.swmm_getValue(305,index)
        depth=self.lib.swmm_getValue(310,index)
        overflow=self.lib.swmm_getValue(308,index)
        removed=min(volume,depth*area,overflow*dt)*.4
        for code,value in ((311,removed/dt),(305,volume-removed),
                           (310,depth-removed/area),(308,overflow-removed/dt)):
            self.lib.swmm_setValue(code,index,value)
        self.check(self.lib.swmm_saveResults())
        now=self.lib.swmm_getCurrentTime()
        finished=now*86400000>=self.lib.swmm_getRoutingDuration()-1e-6
        return (0. if finished else now,depth,volume,overflow,removed)


def fixture(root, name, units, averages=True):
    if name=='split':
        model=sealed_model(units,with_pump=True)
        model.update_options(end_time=time(0,1,2),routing_step=timedelta(seconds=7),
            report_step=timedelta(seconds=20),rule_step=timedelta(seconds=20),variable_step=0)
        source=model.to_document().text
    else:source=base_fixture(root,'storage' if name=='subset' else name,units)
    source+='\n[REPORT]\nAVERAGES '+('YES' if averages else 'NO')+'\n'
    if name=='subset':
        # Resolve actual identities instead of relying on fixture naming.
        from easysewer.model import Model
        from easysewer.io.inp import InpDocument
        model=Model.from_document(InpDocument.from_text(source),strict=True)
        source+='NODES '+list(model.nodes)[-1]+'\nLINKS '+list(model.links)[-1]+'\n'
    return source


def layout(raw):
    pos=raw.index(b'ESOUT001')+8
    bindings=[];floats=[]
    def integer():
        nonlocal pos
        bindings.append(pos);v=struct.unpack_from('<i',raw,pos)[0];pos+=4;return v
    def identity():
        nonlocal pos
        n=integer();bindings.append(pos);pos+=n
    unit,flow,ignore,avg,step=[integer() for _ in range(5)]
    bindings.append(pos);pos+=8
    sv,nv,lv,poll,subs,nodes,links=[integer() for _ in range(7)]
    for _ in range(4):bindings.append(pos);pos+=8
    s,n,l=[integer() for _ in range(3)]
    for _ in range(s+n):identity();integer()
    for _ in range(l):identity();integer();integer()
    for _ in range(poll):identity();integer()
    periods=pos;pos+=8;steps=pos;pos+=4
    for _ in range((nodes*nv+links*lv) if avg else 0):floats.append(pos);pos+=8
    pond=None
    if pos<len(raw):
        assert raw[pos:pos+8]==b'ESPOND01';pos+=8
        for _ in range(3):integer()
        count=integer()
        for _ in range(count):identity()
        pond=pos;pos+=20
    assert pos==len(raw),(pos,len(raw))
    return dict(bindings=bindings,floats=floats,periods=periods,steps=steps,pond=pond)


@unittest.skipUnless(os.environ.get('EASYSEWER_CHECKPOINT_STANDARD') and os.environ.get('EASYSEWER_CHECKPOINT_CUSTOM'),
                     'Requires internal OUT state libraries')
class NativeCheckpointOutputTests(unittest.TestCase):
    def engine(self,family,root,source):
        return Engine(os.environ['EASYSEWER_CHECKPOINT_'+family.upper()],root,source)

    def run_case(self,family,name,units='CFS',averages=True,action='normal'):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);e=self.engine(family,root,fixture(root,name,units,averages))
            trace=[];midperiod=False;changed=False
            try:
                for step in range(20000):
                    raw=e.dump();info=layout(raw)
                    midperiod|=struct.unpack_from('<i',raw,info['steps'])[0]>1
                    if action=='restore':
                        e.damage();self.assertEqual(e.restore(raw),0)
                        self.assertTrue(e.committed);self.assertEqual(e.cleanup,0)
                        self.assertEqual(e.dump(),raw)
                    elif action=='scratch':
                        e.lib.es_test_output_change(4,1)
                        if family=='custom':e.lib.es_test_ponding_change(4,1)
                        self.assertEqual(e.dump(),raw)
                    elif action=='omit' and not changed and midperiod:
                        e.lib.es_test_output_change(3,0);changed=True
                    row=e.split_step(units) if name=='split' else e.step()
                    trace.append(row)
                    if not row[0]:break
                else:self.fail('did not finish')
                self.check_queries(e)
            finally:e.close()
            rpt=re.sub(rb'(?m)^[ \t]*(?:Analysis (?:begun|ended)|Total elapsed time)[^\r\n]*',b'',e.paths[1].read_bytes()).replace(os.fsencode(root),b'<workspace>')
            return trace,e.paths[2].read_bytes(),rpt,e.queries,midperiod

    def check_queries(self,e):
        e.check(e.lib.swmm_end())
        periods=e.lib.es_test_output_periods()
        e.queries=tuple(e.lib.swmm_getSavedValue(code,0,p) for p in range(1,periods+1)
                        for code in (303,305,308,407,410))
        e.lib.es_test_output_change(4,1)
        self.assertEqual(e.queries,tuple(e.lib.swmm_getSavedValue(code,0,p) for p in range(1,periods+1)
                        for code in (303,305,308,407,410)))
        e.check(e.lib.swmm_report());e.lib.swmm_close();e.closed=True

    def test_restoration_preserves_full_outputs_and_post_end_queries(self):
        cases=('hydraulic','averages','subset','pump','weir','runoff','lid','gwater','snow',
               'no-quality','delayed','no-routing','report-disabled','empty','node-only')
        for family in ('standard','custom'):
            for name in cases+(('split',) if family=='custom' else ()):
                for units in ('CFS','CMS'):
                    with self.subTest(family=family,case=name,units=units):
                        expected=self.run_case(family,name,units)
                        self.assertEqual(self.run_case(family,name,units,action='restore'),expected)
                        EVIDENCE.append(dict(kind='restore',family=family,case=name,units=units,
                            midperiod=expected[-1],steps=len(expected[0]),out_sha256=hashlib.sha256(expected[1]).hexdigest()))

    def test_query_scratch_and_completed_split_step_scratch_are_recomputed(self):
        for family in ('standard','custom'):
            for name in ('hydraulic','pump','runoff','empty')+(('split',) if family=='custom' else ()):
                for avg in (False,True):
                    with self.subTest(family=family,case=name,averages=avg):
                        self.assertEqual(self.run_case(family,name,averages=avg,action='scratch'),
                                         self.run_case(family,name,averages=avg))
                        EVIDENCE.append(dict(kind='scratch',family=family,case=name,averages=avg))

    def test_omitting_average_buffers_changes_actual_results(self):
        for family in ('standard','custom'):
            for name in ('averages','pump')+(('split',) if family=='custom' else ()):
                with self.subTest(family=family,case=name):
                    normal=self.run_case(family,name);omitted=self.run_case(family,name,action='omit')
                    self.assertTrue(normal[-1]);self.assertNotEqual(normal[1],omitted[1])
                    EVIDENCE.append(dict(kind='omission',family=family,case=name))

    def test_corrupt_payload_rejected_before_any_owner_or_resource_changes(self):
        for family in ('standard','custom'):
            with tempfile.TemporaryDirectory() as folder:
                root=Path(folder);e=self.engine(family,root,fixture(root,'pump','CFS'))
                try:
                    for _ in range(7):e.step()
                    raw=e.dump();info=layout(raw);bad=[]
                    for pos in info['bindings']:
                        b=bytearray(raw);b[pos]^=1;bad.append(b)
                    for pos in info['floats']:
                        for value in (float('nan'),float('inf'),1e100,1.0000000000000002):
                            b=bytearray(raw);struct.pack_into('<d',b,pos,value);bad.append(b)
                    for pos,fmt,value in ((info['periods'],'<Q',2**31),(info['steps'],'<i',-1)):
                        b=bytearray(raw);struct.pack_into(fmt,b,pos,value);bad.append(b)
                    if info['pond'] is not None:
                        for offset,fmt,value in ((0,'<i',2),(4,'<d',-1),(12,'<d',float('nan'))):
                            b=bytearray(raw);struct.pack_into(fmt,b,info['pond']+offset,value);bad.append(b)
                    bad.extend((raw[:-1],raw+b'\x00'))
                    for b in bad:
                        calls=e.provider_calls
                        self.assertNotEqual(e.restore(b),0);self.assertFalse(e.committed)
                        self.assertEqual(e.provider_calls,calls);self.assertEqual(e.dump(),raw)
                    EVIDENCE.append(dict(kind='corruption',family=family,payloads=len(bad)))
                finally:e.close()

    def test_capture_requires_ready_unfailed_unended_and_finalized_owner(self):
        for family in ('standard','custom'):
            with tempfile.TemporaryDirectory() as folder:
                root=Path(folder);e=self.engine(family,root,fixture(root,'split','CFS'))
                try:
                    for group,invalid,valid in ((0,0,1),(1,1,0),(2,1,0)):
                        e.lib.es_test_output_guard(group,invalid);size=c.c_size_t()
                        self.assertNotEqual(e.lib.es_test_output_save(None,0,c.byref(size),1),0)
                        e.lib.es_test_output_guard(group,valid);e.dump()
                    if family=='custom':
                        e.check(e.lib.swmm_execRouting());size=c.c_size_t()
                        self.assertNotEqual(e.lib.es_test_output_save(None,0,c.byref(size),1),0)
                        e.check(e.lib.swmm_saveResults());e.dump()
                finally:e.close()

    def test_float_accumulators_preserve_signed_zero_subnormal_and_finite_extremes(self):
        for family in ('standard','custom'):
            with tempfile.TemporaryDirectory() as folder:
                root=Path(folder);e=self.engine(family,root,fixture(root,'pump','CFS'))
                try:
                    raw=e.dump(only=True);pos=layout(raw)['floats'][0]
                    for bits in (0,0x80000000,1,0x80000001,0x7f7fffff,0xff7fffff):
                        value=struct.unpack('<f',struct.pack('<I',bits))[0]
                        changed=bytearray(raw);struct.pack_into('<d',changed,pos,value)
                        self.assertEqual(e.restore(changed,only=True),0)
                        self.assertEqual(e.dump(only=True),changed)
                    self.assertEqual(e.restore(raw,only=True),0)
                finally:e.close()

    def test_no_binary_saving_keeps_output_owner_empty_during_routing(self):
        for family in ('standard','custom'):
            with tempfile.TemporaryDirectory() as folder:
                root=Path(folder);e=self.engine(family,root,fixture(root,'hydraulic','CFS'))
                try:
                    e.check(e.lib.swmm_end());e.check(e.lib.swmm_close())
                    e.check(e.lib.swmm_open(*(os.fsencode(p) for p in e.paths)))
                    e.check(e.lib.swmm_start(0))
                    for _ in range(25):
                        raw=e.dump();info=layout(raw)
                        self.assertEqual(struct.unpack_from('<Q',raw,info['periods'])[0],0)
                        self.assertEqual(struct.unpack_from('<i',raw,info['steps'])[0],0)
                        e.damage();self.assertEqual(e.restore(raw),0);self.assertEqual(e.dump(),raw)
                        e.step()
                finally:e.close()

    def test_fresh_process_reconstructs_only_numeric_owners_without_continuing(self):
        for family in ('standard','custom'):
            for name in ('pump',)+(('split',) if family=='custom' else ()):
                with tempfile.TemporaryDirectory() as folder:
                    root=Path(folder);(root/'first').mkdir();(root/'second').mkdir()
                    source=fixture(root/'first',name,'CFS');e=self.engine(family,root/'first',source)
                    try:
                        for _ in range(4):
                            e.split_step('CFS') if name=='split' else e.step()
                        raw=e.dump(only=True)
                    finally:e.close()
                    (root/'state').write_bytes(raw);(root/'source').write_text(source)
                    code='''from pathlib import Path
import sys
from test_native_v2_checkpoint_output import Engine
lib,source,state,dest=sys.argv[1:]
e=Engine(lib,dest,Path(source).read_text())
try:
 initial=e.dump(only=True);raw=Path(state).read_bytes()
 e.lib.es_test_output_change(0,1)
 if hasattr(e.lib,'es_test_ponding_change'):e.lib.es_test_ponding_change(0,1)
 assert e.restore(raw,only=True)==0 and e.committed and e.cleanup==0
 assert e.dump(only=True)==raw and raw!=initial
 # Numeric-only proof: restore startup state before closing the fresh file.
 assert e.restore(initial,only=True)==0
finally:e.close()
'''
                    env=dict(os.environ,PYTHONPATH=os.pathsep.join((str(Path(__file__).parent),str(Path(__file__).parents[1]/'src'))))
                    p=subprocess.run([sys.executable,'-B','-c',code,os.environ['EASYSEWER_CHECKPOINT_'+family.upper()],
                        str(root/'source'),str(root/'state'),str(root/'second')],env=env,capture_output=True,text=True,timeout=60)
                    self.assertEqual(p.returncode,0,p.stdout+p.stderr)
                    EVIDENCE.append(dict(kind='fresh-owner',family=family,case=name))


if __name__=='__main__':unittest.main()
