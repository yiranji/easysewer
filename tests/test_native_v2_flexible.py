"""Custom native execution compared with an independent direct-ABI policy."""

from datetime import timedelta
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest

from easysewer import get_native_capabilities
from easysewer.model import Ref
from easysewer.runtime import FlexiblePondingBackend,FlexiblePondingPolicy,Runner,SessionError
from test_flexible_v2 import configuration,ponding_model
from test_files_v2 import bind


# Independent process: no easysewer import or shared adjustment implementation.
# Conversion constants are from the C ABI's Ucf/Qcf tables; state comes from C.
ORACLE=r'''
import ctypes as c,json,math,sys
from pathlib import Path
library,input,report,output,parameters,result_path=sys.argv[1:]
p=json.loads(Path(parameters).read_text(encoding='utf-8'));policy=p['policy']
lib=c.CDLL(library)
for name,args,rest in [('open',[c.c_char_p]*3,c.c_int),('start',[c.c_int],c.c_int),('end',[],c.c_int),('report',[],c.c_int),('close',[],c.c_int),('execRouting',[],c.c_int),('saveResults',[],c.c_int),('getCurrentTime',[],c.c_double),('getRoutingDuration',[],c.c_double),('getValue',[c.c_int,c.c_int],c.c_double),('setValue',[c.c_int,c.c_int,c.c_double],None),('getIndex',[c.c_int,c.c_char_p],c.c_int)]:
 f=getattr(lib,'swmm_'+name);f.argtypes=args;f.restype=rest
def check(value):
 assert value==0,value
rows=[]
try:
 check(lib.swmm_open(input.encode(),report.encode(),output.encode()));check(lib.swmm_start(1))
 units=int(lib.swmm_getValue(8,0));vf=1 if units<3 else .02832;lf=1 if units<3 else .3048;af=1 if units<3 else .3048**2
 qf=(1,448.831,.64632,.02832,28.317,2.4466)[units];k=qf/vf
 minimum_depth=policy['depth_threshold_m']/.3048*lf
 minimum_flow=policy['flow_threshold_cms']/.02832*qf
 nodes=[]
 for item in p['nodes']:
  i=lib.swmm_getIndex(2,item['id'].encode());assert i>=0
  nodes.append(dict(id=item['id'],index=i,area=item['area']*vf/af/lf,depth=lib.swmm_getValue(310,i),volume=lib.swmm_getValue(305,i)))
 now=lib.swmm_getCurrentTime()*86400;duration=lib.swmm_getRoutingDuration()/1000
 for step in range(100000):
  if now>=duration-1e-6:break
  check(lib.swmm_execRouting());following=lib.swmm_getCurrentTime()*86400;dt=following-now;assert dt>0
  for n in nodes:
   i=n['index'];h=lib.swmm_getValue(310,i);v=lib.swmm_getValue(305,i);q=lib.swmm_getValue(308,i);r=policy['external_flooding_ratio'];dv=0
   if h>minimum_depth and q>minimum_flow and h>n['depth'] and v>n['volume']:
    dv=min(r*(v-n['volume']),r*n['area']*(h-n['depth']),r*n['area']*h,r*v,q*dt/k)
   ex=dv/dt*k
   lib.swmm_setValue(311,i,ex)
   if dv:
    lib.swmm_setValue(310,i,h-dv/n['area']);lib.swmm_setValue(305,i,v-dv);lib.swmm_setValue(308,i,q-ex)
   n['depth']=lib.swmm_getValue(310,i);n['volume']=lib.swmm_getValue(305,i)
   rows.append(dict(id=n['id'],time=following,volume=n['volume'],depth=n['depth'],external=ex,removed=dv))
  check(lib.swmm_saveResults());now=following
 else:raise AssertionError('Direct policy did not finish')
 check(lib.swmm_end());check(lib.swmm_report())
finally:check(lib.swmm_close())
Path(result_path).write_text(json.dumps(rows),encoding='utf-8')
'''


@unittest.skipUnless(get_native_capabilities()['flexible_ponding'],'Custom native solver unavailable')
class NativeFlexibleTests(unittest.TestCase):
    def success(self, result):
        self.assertTrue(result.succeeded,repr(result.failure)+' '+repr(result.diagnostics.errors))
        self.assertTrue(result.native_completed)
        self.assertIsNotNone(result.backend_results)
        self.assertEqual(result.backend.numerical_policy,'easysewer:flexible-ponding:incremental-volume:2:native:14')
        self.assertEqual(dict(result.output_metadata.semantics)['swmm:nodes:overflow'],'easysewer:system-flooding')
        record=json.loads(result.artifact('run:execution-record').read_bytes())
        self.assertEqual(record['backend_settings']['input_sha256'],result.snapshot.input_sha256)
        self.assertEqual(record['backend_results'],result.backend_results.data)

    def trace(self,result):
        values=[json.loads(line) for line in result.artifact('easysewer:flexible-ponding-steps').read_bytes().decode().splitlines()]
        self.assertEqual(values[0]['kind'],'easysewer:flexible-ponding-trace')
        self.assertEqual(values[-1]['kind'],'completed')
        return values[1:-1]

    def test_six_units_match_independent_native_policy_and_conservation_ledger(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);script=root/'oracle.py';script.write_text(ORACLE,encoding='utf-8')
            totals=[]
            for units in ('CFS','GPM','MGD','CMS','LPS','MLD'):
                with self.subTest(units=units):
                    model=ponding_model(units);original=model.to_json_document().to_bytes()
                    run=Runner().run(model,configuration(root/units,step_batch_size=7))
                    self.success(run);rows=self.trace(run)
                    self.assertEqual(model.to_json_document().to_bytes(),original)
                    self.assertGreater(run.backend_results.data['removed_volume'],0)
                    parameters=root/(units+'-parameters.json');parameters.write_bytes(run.snapshot.backend_settings)
                    output=root/(units+'-oracle.out');ledger=root/(units+'-oracle.json')
                    subprocess.run([sys.executable,'-I','-B',str(script),run.backend.library,run.input.path,
                        str(root/(units+'-oracle.rpt')),str(output),str(parameters),str(ledger)],cwd=Path(run.input.path).parent,
                        check=True,capture_output=True,timeout=30,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                    oracle=json.loads(ledger.read_text(encoding='utf-8'))
                    self.assertEqual(len(rows),len(oracle))
                    for row,expected in zip(rows,oracle):
                        self.assertAlmostEqual(row['after']['volume'],expected['volume'],places=8)
                        self.assertAlmostEqual(row['external_flow'],expected['external'],places=8)
                        self.assertAlmostEqual(row['before']['volume']-row['after']['volume'],row['removed_volume'],places=8)
                        factor=json.loads(run.snapshot.backend_settings)['flow_per_volume_rate']
                        self.assertAlmostEqual(row['external_flow']/factor*row['dt_seconds'],row['removed_volume'],places=8)
                        self.assertEqual(row['index'],run.engine_objects.index(Ref(collection='swmm:nodes',key=row['id'])))
                    self.assertEqual(run.output.read_bytes(),output.read_bytes())
                    totals.append(run.backend_results.data['removed_volume']/(1 if units in ('CFS','GPM','MGD') else .02832))
                    self.assertTrue(any(row['removed_volume']==0 for row in rows))
                    self.assertTrue(any(row['removed_volume']>0 for row in rows))
            for total in totals[1:]:self.assertAlmostEqual(total,totals[0],places=5)

    def test_actual_identity_and_snapshot_survive_original_rename_after_preparation(self):
        with tempfile.TemporaryDirectory() as directory:
            model=ponding_model()
            def change(value):
                if value.phase=='opening':model.nodes.rename('J','Different')
            result=Runner().run(model,configuration(Path(directory)/'run'),progress=change)
            self.success(result)
            self.assertEqual(result.engine_objects.names('swmm:nodes'),('O','J'))
            self.assertEqual(result.backend_results.data['nodes'][0]['index'],1)
            self.assertEqual(result.backend_results.data['nodes'][0]['id'],'J')
            self.assertIn('Different',model.nodes)

    def test_rule_step_and_step_batches_do_not_freeze_custom_clock_or_change_results(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);model=ponding_model();model.update_options(rule_step=timedelta(seconds=20))
            outputs=[]
            for size in (1,17,10000):
                run=Runner().run(model,configuration(root/str(size),step_batch_size=size))
                self.success(run);outputs.append(run.output.read_bytes())
                self.assertGreaterEqual(run.backend_results.data['time_seconds'],60)
            self.assertEqual(outputs,[outputs[0]]*3)

    def test_policy_endpoints_no_trace_and_invalid_settings_before_start(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for ratio in (0,1):
                result=Runner().run(ponding_model(),configuration(root/str(ratio),FlexiblePondingPolicy(external_flooding_ratio=ratio,record_steps=False)))
                self.success(result)
                if ratio==0:self.assertEqual(result.backend_results.data['removed_volume'],0)
                else:self.assertGreater(result.backend_results.data['removed_volume'],0)
                with self.assertRaises(KeyError):result.artifact('easysewer:flexible-ponding-steps')
            model=ponding_model();model.update_options(allow_ponding=False)
            invalid=Runner().run(model,configuration(root/'invalid',keep_failed_artifacts=False))
            self.assertEqual(invalid.status,'rejected');self.assertIsNone(invalid.engine_objects)

    def test_hotstart_initial_state_is_read_from_loaded_engine(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);producer=ponding_model();bind(producer,'HOTSTART','SAVE',root/'state.hsf')
            first=Runner().run(producer,configuration(root/'producer'));self.success(first)
            evidence,=first.produced_caches
            consumer=ponding_model();bind(consumer,'HOTSTART','USE',evidence.artifact.path)
            second=Runner().run(consumer,configuration(root/'consumer'),producers={'HOTSTART':evidence})
            self.success(second);initial=self.trace(second)[0]
            self.assertGreater(initial['previous']['volume'],0)
            self.assertGreater(initial['previous']['depth'],0)
            self.assertLess(initial['removed_volume'],initial['previous']['volume'])

    def test_callback_failure_and_cancellation_keep_old_outputs_and_partial_trace(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for cancel in (False,True):
                folder=root/str(cancel);folder.mkdir();(folder/'model.out').write_bytes(b'old-success')
                event=threading.Event()
                def stop(progress):
                    if progress.phase=='running':
                        if cancel:event.set()
                        else:raise RuntimeError('callback failed')
                run=Runner().run(ponding_model(),configuration(folder,overwrite=True,step_batch_size=1),progress=stop,cancel_event=event)
                self.assertEqual(run.status,'cancelled' if cancel else 'failed')
                self.assertEqual((folder/'model.out').read_bytes(),b'old-success')
                self.assertFalse(run.native_completed)
                partial=run.artifact('easysewer:flexible-ponding-steps')
                self.assertFalse(partial.complete)
                self.assertTrue(partial.read_bytes().startswith(b'{'))

    def test_missing_custom_symbols_and_unbound_session_fail_explicitly(self):
        from easysewer.runtime import StandardBackend
        standard=StandardBackend().probe()
        capability=FlexiblePondingBackend(library=standard.library).probe()
        self.assertFalse(capability.available)
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);ponding_model().to_inp(root/'model.inp')
            with FlexiblePondingBackend().session(working_directory=root) as session:
                session.open('model.inp','model.rpt','model.out')
                with self.assertRaises(SessionError) as raised:session.start()
                self.assertIn('binding',str(raised.exception))
                self.assertIsNotNone(session.returncode)

    def test_long_absolute_input_path_matches_short_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);folder=root
            while len(str(folder/'model.inp'))<275:
                folder=folder/('p'*40)
            folder.mkdir(parents=True);ponding_model().to_inp(folder/'model.inp')
            ponding_model().to_inp(root/'short.inp')
            # A sibling cwd ensures open sends the absolute input path to C.
            # Keep cwd short because Windows CreateProcess has a separate limit.
            work=root/'work';work.mkdir()
            observations=[]
            for index,inp in enumerate((root/'short.inp',folder/'model.inp')):
                with FlexiblePondingBackend().session(working_directory=work) as session:
                    session.open(inp,str(index)+'.rpt',str(index)+'.out')
                    observations.append((session.objects,session.flow_units))
                self.assertEqual(session.returncode,0)
            self.assertEqual(observations[0],observations[1])
            self.assertEqual(observations[1][0].names('swmm:nodes'),('O','J'))


if __name__=='__main__':unittest.main()
