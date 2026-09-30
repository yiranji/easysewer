"""Mass balance/statistics owners; this is not whole-solver continuation."""
import ctypes as c
from datetime import time,timedelta
import hashlib,json,os,re,struct,subprocess,sys,tempfile,unittest
from pathlib import Path
from easysewer.model import Model
from easysewer.io.inp import InpDocument
from test_native_v2_checkpoint_lid import Engine as LidEngine, OPEN, fixture as lid
from test_native_v2_checkpoint_network import storage_fixture,regulator_fixture
from test_native_v2_checkpoint_dynwave import fixture as hydraulic
from test_native_v2_checkpoint_clocks import fixture as hydrology
from test_native_v2_checkpoint_groundwater import fixture as groundwater
from test_native_v2_checkpoint_snow import process_fixture as snow
from test_native_v2_checkpoint_frames import fixture as frames
from test_native_v2_standard_io import handles

EVIDENCE=[]
CASES=('hydraulic','slot','ponding','steady','kinwave','dry','no-routing','events','averages',
       'delayed','no-quality','report-disabled','empty','node-only','storage','pump','weir',
       'runoff','lid','gwater','snow','rdii','interface','count')

class Engine(LidEngine):
    def __init__(self,*args):
        super().__init__(*args)
        for name,args,result in (
            ('es_test_balance_save',[c.c_void_p,c.c_size_t,c.POINTER(c.c_size_t),c.c_int],c.c_int),
            ('es_test_balance_restore',[c.c_void_p,c.c_size_t,c.c_int,OPEN,c.c_int,c.POINTER(c.c_int),c.POINTER(c.c_int)],c.c_int),
            ('es_test_massbal_change',[c.c_int,c.c_int],None),('es_test_stats_change',[c.c_int,c.c_int],None)):
            f=getattr(self.lib,name);f.argtypes,f.restype=args,result
    def dump(self,only=False):
        size=c.c_size_t();self.check(self.lib.es_test_balance_save(None,0,c.byref(size),only))
        raw=c.create_string_buffer(size.value)
        self.check(self.lib.es_test_balance_save(raw,size.value,c.byref(size),only));return raw.raw
    def restore(self,raw,fail_at=-1,only=False):
        cleanup,committed=c.c_int(),c.c_int()
        error=self.lib.es_test_balance_restore(c.create_string_buffer(bytes(raw)),len(raw),fail_at,self.provider,only,c.byref(cleanup),c.byref(committed))
        self.cleanup,self.committed=cleanup.value,bool(committed.value)
        self.calls,self.hit=self.lib.es_test_tables_calls(),self.lib.es_test_tables_hit()
        return error
    def damage(self):
        self.lib.es_test_climate_bundle_poison();self.lib.es_test_frames_poison(0)
        self.lib.es_test_groundwater_poison(1);self.lib.es_test_groundwater_poison(2)
        self.lib.es_test_snow_change(0,1);self.lib.es_test_lid_change(0,1)
        self.lib.es_test_massbal_change(0,1);self.lib.es_test_stats_change(0,1)
        self.lib.es_test_tables_drop();self.lib.es_test_streams_drop()

def fixture(root,name='storage',units='CFS'):
    resource_binding=''
    if name in ('hydraulic','slot','ponding','steady','kinwave'):
        source=hydraulic(surcharge='SLOT' if name=='slot' else 'EXTRAN',ponding=name=='ponding')
        if name in ('steady','kinwave'):
            source=re.sub(r'(?m)^FLOW_ROUTING\s+\S+','FLOW_ROUTING '+('STEADY' if name=='steady' else 'KINWAVE'),source)
    elif name in ('storage','count'):
        source=storage_fixture()
        if name=='count':source=source.replace('Mass MG/L','Mass #/L')
    elif name=='pump':source=regulator_fixture('PUMP1')
    elif name=='weir':source=regulator_fixture('TRANSVERSE')
    elif name=='gwater':source=groundwater(multiple=True)
    elif name=='snow':source=snow(removal='all',multiple=True)
    elif name=='lid':source=lid(root,'BC',variant='to-catchment')
    elif name=='rdii':
        source=frames(root,'rdii-text')
        # Construct/convert model geometry before binding the independent RDII
        # file. Its header declares CFS and the same unchanged node identities.
        source,section,binding=source.partition('[FILES]')
        resource_binding=section+binding
    elif name=='interface':source=frames(root,'mapped',units)
    elif name in ('empty','node-only'):
        source='[OPTIONS]\nFLOW_UNITS CFS\nSTART_DATE 01/01/2020\nEND_DATE 01/01/2020\nEND_TIME 00:05:00\nREPORT_STEP 00:00:30\nROUTING_STEP 5\n'
        if name=='node-only':source+='[JUNCTIONS]\nJ 0 5\n[OUTFALLS]\nO 0 FREE NO\n[INFLOWS]\nJ FLOW "" FLOW 1 1 .1\n'
    else:
        source=hydrology(events=name=='events',averages=name=='averages',ignore_routing=name=='no-routing',units=units if name=='events' else 'CFS')
    model=Model.from_document(InpDocument.from_text(source),strict=True)
    if name=='dry':
        from dataclasses import replace
        model.timeseries.update('Rain',points=tuple(replace(p,value=0) for p in model.timeseries['Rain'].points))
    if name=='delayed':model.update_options(report_start_time=time(0,3))
    if name=='no-quality':
        source=storage_fixture();model=Model.from_document(InpDocument.from_text(source),strict=True)
        model.update_options(ignore_quality=True)
    if units!='CFS' and name!='events':model.convert_units(units)
    source=model.to_document().text+resource_binding
    if name=='report-disabled':source+='[REPORT]\nDISABLED YES\n'
    else:source+='[REPORT]\nSUBCATCHMENTS ALL\nNODES ALL\nLINKS ALL\n'
    return source

def layout(raw):
    start=raw.index(b'ESMASS01');pos=start+8;bindings=[];floats=[];integers=[];counts=[]
    def integer(fixed=True,width=4):
        nonlocal pos
        v=struct.unpack_from('<i' if width==4 else '<Q',raw,pos)[0]
        (bindings if fixed else integers if width==4 else counts).append(pos);pos+=width;return v
    def identity():
        nonlocal pos
        n=integer();bindings.append(pos);pos+=n
    def number(fixed=False):
        nonlocal pos
        (bindings if fixed else floats).append(pos);pos+=8
    sub,node,link,poll=[integer() for _ in range(4)]
    for _ in range(7):integer()
    number(True)
    for _ in range(sub+node+link):identity()
    for _ in range(45):number()
    for _ in range(poll):
        identity();integer();number(True)
        for _ in range(29):number()
    for _ in range(2*node+4):number()
    assert raw[pos:pos+8]==b'ESSTAT01';pos+=8
    s,n,l,p,storage,outfall,pump,levels,classes=[integer() for _ in range(9)]
    for _ in range(5):integer()
    number(True)
    for _ in range(p):identity()
    have=[integer() for _ in range(6)]
    for _ in range(s):
        identity()
        if have[0]:
            for _ in range(8):number()
    for _ in range(n):
        identity();typ=integer()
        if have[1]:
            for _ in range(13):number()
            integer(False);number();number()
        if typ==2:  # STORAGE
            integer()
            for _ in range(7):number()
        if typ==1:  # OUTFALL
            integer();number();number()
            for _ in range(p):number()
            integer(False)
    for _ in range(l):
        identity();typ=integer()
        if have[2]:
            for _ in range(12+classes+1):number()
            integer(False,8);integer(False)
        if typ==1:  # PUMP
            integer()
            for _ in range(8):number()
            integer(False);integer(False)
    for _ in range(3):number()
    integer(False);number();number()
    for _ in range(levels):number(True)
    for _ in range(1,levels):integer(False)
    number();number();number()
    assert pos==len(raw),(pos,len(raw))
    return dict(start=start,bindings=bindings,floats=floats,integers=integers,counts=counts)

@unittest.skipUnless(os.environ.get('EASYSEWER_CHECKPOINT_STANDARD') and os.environ.get('EASYSEWER_CHECKPOINT_CUSTOM'),'Requires internal balance test libraries')
class NativeCheckpointBalanceTests(unittest.TestCase):
    def engine(self,family,root,source):return Engine(os.environ['EASYSEWER_CHECKPOINT_'+family.upper()],root,source)
    def run_case(self,family,name='storage',units='CFS',action='normal'):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);e=self.engine(family,root,fixture(root,name,units));trace=[];changed=False;elapsed=0
            try:
                for step in range(20000):
                    raw=e.dump();layout(raw)
                    if action=='restore':
                        e.damage();self.assertEqual(e.restore(raw),0);self.assertTrue(e.committed)
                        self.assertEqual(e.cleanup,0);self.assertEqual(e.dump(),raw)
                    elif action=='scratch':
                        e.lib.es_test_massbal_change(11,1);e.lib.es_test_stats_change(8,1)
                        self.assertEqual(e.dump(),raw)
                    elif action.startswith('omit') and not changed and elapsed*86400>=(600 if name=='lid' else 60):
                        domain,group=action.split(':')[1:]
                        getattr(e.lib,'es_test_'+domain+'_change')(int(group),0);changed=True
                    values=e.step();elapsed=values[0];trace.append(values)
                    if not elapsed:break
                else:self.fail('did not finish')
            finally:e.close()
            rpt=re.sub(rb'(?m)^[ \t]*(?:Analysis (?:begun|ended)|Total elapsed time)[^\r\n]*',b'',e.paths[1].read_bytes()).replace(os.fsencode(root),b'<workspace>')
            detail={p.name:p.read_bytes() for p in root.glob('detail-*.txt')}
            return trace,e.paths[2].read_bytes(),rpt,detail,changed
    def test_complete_restored_results_across_processes_units_and_statistics(self):
        for family in ('standard','custom'):
            for name in CASES:
                for units in ('CFS','CMS'):
                    with self.subTest(family=family,case=name,units=units):
                        expected=self.run_case(family,name,units)
                        self.assertEqual(self.run_case(family,name,units,'restore'),expected)
                        EVIDENCE.append(dict(kind='restore',family=family,case=name,units=units,steps=len(expected[0]),out_sha256=hashlib.sha256(expected[1]).hexdigest()))
    def test_derived_and_unused_fields_can_be_poisoned_without_future_effects(self):
        for family in ('standard','custom'):
            for name in CASES:
                with self.subTest(family=family,case=name):
                    self.assertEqual(self.run_case(family,name,action='scratch'),self.run_case(family,name))
                    EVIDENCE.append(dict(kind='scratch',family=family,case=name))
    def test_corruption_rejects_before_resource_staging_and_preserves_owners(self):
        for family in ('standard','custom'):
            for name in ('storage','pump','gwater'):
                with tempfile.TemporaryDirectory() as directory:
                    root=Path(directory);e=self.engine(family,root,fixture(root,name))
                    try:
                        for _ in range(8):e.step()
                        raw=e.dump();f=layout(raw);before=handles()
                        def reject(bad):
                            calls=e.provider_calls
                            self.assertNotEqual(e.restore(bad),0);self.assertFalse(e.committed)
                            self.assertEqual(e.provider_calls,calls);self.assertEqual(e.dump(),raw)
                            self.assertEqual(handles(),before)
                        for size in range(f['start'],len(raw)):reject(raw[:size])
                        reject(raw+b'x')
                        for off in f['bindings']:
                            bad=bytearray(raw);bad[off]^=1;reject(bad)
                        for off in f['floats']:
                            for v in (float('nan'),float('inf'),-float('inf')):
                                bad=bytearray(raw);struct.pack_into('<d',bad,off,v);reject(bad)
                        for off in f['integers']:
                            bad=bytearray(raw);struct.pack_into('<i',bad,off,-7);reject(bad)
                        for off in f['counts']:
                            bad=bytearray(raw);struct.pack_into('<Q',bad,off,2**64-1);reject(bad)
                        self.assertEqual(e.restore(raw),0)
                        for fail_at in range(e.calls):
                            error=e.restore(raw,fail_at)
                            if e.committed:self.assertEqual((error,e.cleanup),(0,7))
                            else:self.assertIn(error,(6,7))
                            self.assertEqual(e.dump(),raw);self.assertEqual(handles(),before)
                            self.assertEqual(e.restore(raw),0)
                        EVIDENCE.append(dict(kind='corruption',family=family,case=name,bytes=len(raw)-f['start'],bindings=len(f['bindings']),floats=len(f['floats']),integers=len(f['integers'])+len(f['counts'])))
                    finally:e.close()
    def test_omitted_totals_and_statistics_change_actual_future_reports(self):
        cases=(('runoff','massbal',1),('gwater','massbal',2),('storage','massbal',3),
               ('lid','massbal',6),('count','massbal',7),('storage','massbal',9),
               ('runoff','stats',1),('storage','stats',2),('hydraulic','stats',3),
               ('storage','stats',4),('count','stats',5),('pump','stats',6),('hydraulic','stats',7))
        for family in ('standard','custom'):
            for name,domain,group in cases:
                with self.subTest(family=family,case=name,domain=domain,group=group):
                    expected=self.run_case(family,name)
                    omitted=self.run_case(family,name,action=f'omit:{domain}:{group}')
                    self.assertTrue(omitted[-1]);self.assertTrue(omitted[1:4]!=expected[1:4], 'Omitted state did not change actual results')
                    EVIDENCE.append(dict(kind='omission',family=family,case=name,domain=domain,group=group))
    def test_new_process_reconstructs_only_balance_and_statistics_owners(self):
        for family in ('standard','custom'):
            for name in ('storage','pump','gwater'):
                with tempfile.TemporaryDirectory() as directory:
                    root=Path(directory);(root/'first').mkdir();(root/'second').mkdir()
                    source=fixture(root/'first',name);e=self.engine(family,root/'first',source)
                    try:
                        for _ in range(25):e.step()
                        raw=e.dump(only=True)
                    finally:e.close()
                    (root/'state').write_bytes(raw);(root/'source').write_text(source)
                    code='''from pathlib import Path
import sys
from test_native_v2_checkpoint_balance import Engine
lib,source,state,dest=sys.argv[1:]
e=Engine(lib,dest,Path(source).read_text())
try:
 initial=e.dump(only=True);raw=Path(state).read_bytes()
 e.lib.es_test_massbal_change(0,1);e.lib.es_test_stats_change(0,1)
 assert e.restore(raw,only=True)==0 and e.committed and e.cleanup==0
 assert e.dump(only=True)==raw and raw!=initial
finally:e.close()
'''
                    env=dict(os.environ,PYTHONPATH=os.pathsep.join((str(Path(__file__).parent),str(Path(__file__).parents[1]/'src'))))
                    p=subprocess.run([sys.executable,'-B','-c',code,os.environ['EASYSEWER_CHECKPOINT_'+family.upper()],str(root/'source'),str(root/'state'),str(root/'second')],env=env,capture_output=True,text=True,timeout=60)
                    self.assertEqual(p.returncode,0,p.stdout+p.stderr)
                    EVIDENCE.append(dict(kind='fresh-owner',family=family,case=name))
