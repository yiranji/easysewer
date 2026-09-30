"""Actual native completion and pre-publication finalization interruptions."""
from datetime import timedelta
from pathlib import Path
import tempfile,threading,time,unittest
from unittest.mock import patch
from easysewer import get_native_capabilities
from easysewer.io.output_metadata import OutputMetadata
from easysewer.io.cache_manifest import CacheManifest
from easysewer.io.hotstart_manifest import HotstartManifest
from easysewer.io.runoff_cache import RunoffLayout
from easysewer.runtime import Runner,RunResult
from easysewer.runtime import runner as rm
from easysewer.runtime._workspace import OutputTransaction
from test_options_v2 import network
from test_runner_v2 import config
from test_cache_reuse_v2 import model as cache_model
from test_rdii_v2 import rdii_model
from test_native_v2_files import selected
from test_files_v2 import bind
EVIDENCE=[]
PHASES=('context','metadata','input_verify','cache_layout','cache_read','cache_verify','manifest_json','hotstart_verify','rdii_verify')
@unittest.skipUnless(get_native_capabilities()['swmm_solver'] and get_native_capabilities()['flexible_ponding'],'Both actual backends required')
class NativeResultFinalizationCancellationTests(unittest.TestCase):
    def test_after_native_finalization_interruption_and_archive(self):
        for backend in ('swmm:standard','easysewer:flexible-ponding'):
            for phase in PHASES:
                for status in ('cancelled','timed_out'):
                    for keep in (False,True):
                        with self.subTest(backend=backend,phase=phase,status=status,keep=keep),tempfile.TemporaryDirectory() as directory:
                            root=Path(directory);target=root/'out';target.mkdir();cache=phase.startswith('cache_') or phase in ('manifest_json','hotstart_verify','rdii_verify')
                            filenames=['model.inp','model.rpt','model.out']+(['history.bin'] if cache else [])
                            for name in filenames:(target/name).write_bytes(b'previous')
                            model=(selected(rdii_model()) if phase=='rdii_verify' else cache_model() if cache else network())
                            if phase=='rdii_verify':model.update_options(routing_step=timedelta(seconds=60))
                            if backend=='easysewer:flexible-ponding':model.update_options(flow_routing='DYNWAVE',allow_ponding=True)
                            if cache:bind(model,'HOTSTART' if phase=='hotstart_verify' else 'RDII' if phase=='rdii_verify' else 'RUNOFF','SAVE',target/'history.bin')
                            before=model.to_json_document().to_bytes();event=threading.Event();active=[];checks=[];completed=[];triggered=[];progress=[];calls=[]
                            module,name={'context':(rm,'context_from_model'),'metadata':(OutputMetadata,'from_stream'),'input_verify':(rm,'input_matches'),'cache_layout':(RunoffLayout,'from_model'),'cache_read':(rm,'read_record_bytes'),'cache_verify':(CacheManifest,'verify'),'manifest_json':(CacheManifest,'to_bytes'),'hotstart_verify':(HotstartManifest,'verify'),'rdii_verify':(CacheManifest,'verify')}[phase]
                            original=getattr(module,name);original_check=rm._Cancellation.check
                            def entered(*a,**k):
                                calls.append(True)
                                enabled=('finalizing' in progress) if cache else len(calls)>1 if phase=='input_verify' else True
                                if enabled:active.append(True)
                                try:
                                    result=original(*a,**k)
                                    if enabled:completed.append(True)
                                    return result
                                finally:
                                    if enabled:active.pop()
                            def check(control):
                                if active:
                                    checks.append(1)
                                    if len(checks)==3:
                                        triggered.append(time.monotonic())
                                        if status=='cancelled':event.set()
                                        else:control.deadline=time.monotonic()-1
                                return original_check(control)
                            with patch.object(module,name,entered),patch.object(rm._Cancellation,'check',check):
                                result=Runner().run(model,config(target,backend=backend,overwrite=True,keep_failed_artifacts=keep),cancel_event=event,progress=lambda row:progress.append(row.phase))
                            returned=time.monotonic();stage='end' if phase=='context' else 'publication' if cache else 'verify'
                            self.assertEqual(result.status,status,result.failure);self.assertEqual(result.failure.stage,stage);self.assertEqual(result.native_completed,phase!='context')
                            self.assertEqual(len(checks),3);self.assertFalse(completed);self.assertEqual(model.to_json_document().to_bytes(),before)
                            for name in filenames:self.assertEqual((target/name).read_bytes(),b'previous')
                            self.assertFalse(list(target.rglob('.easysewer-lock-*')));self.assertEqual(result.retained_directory is not None,keep)
                            if keep:
                                for artifact in result.artifacts:artifact.read_bytes()
                            else:self.assertFalse(result.artifacts)
                            result.save(root/'saved');loaded=RunResult.load(root/'saved');self.assertEqual(loaded.failure,result.failure);self.assertEqual(loaded.failure_report,result.failure_report)
                            EVIDENCE.append(dict(backend=backend,phase=phase,status=status,keep=keep,checks=3,stage=stage,old_outputs_preserved=True,model_unchanged=True,native_completed=result.native_completed,return_seconds=returned-triggered[0]))

    def test_cancel_after_commit_keeps_published_success(self):
        for backend in ('swmm:standard','easysewer:flexible-ponding'):
            with self.subTest(backend=backend),tempfile.TemporaryDirectory() as directory:
                root=Path(directory);model=network()
                if backend=='easysewer:flexible-ponding':model.update_options(flow_routing='DYNWAVE',allow_ponding=True)
                baseline=Runner().run(model,config(root/'baseline',backend=backend));self.assertTrue(baseline.succeeded,baseline.failure)
                event=threading.Event();committed=[];original=OutputTransaction.publish
                def publish(transaction,*a,**k):
                    result=original(transaction,*a,**k);self.assertTrue(transaction.committed);committed.append(True);event.set();return result
                with patch.object(OutputTransaction,'publish',publish):result=Runner().run(model,config(root/'published',backend=backend),cancel_event=event)
                self.assertEqual(committed,[True]);self.assertTrue(result.succeeded,result.failure);self.assertTrue(result.native_completed)
                self.assertEqual(result.output.read_bytes(),baseline.output.read_bytes());result.save(root/'saved');self.assertTrue(RunResult.load(root/'saved').succeeded)
                EVIDENCE.append(dict(kind='after-commit',backend=backend,status=result.status,complete_output_equal=True,native_completed=True))
if __name__=='__main__':unittest.main()