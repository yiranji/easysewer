"""Actual produced cache verification and interrupted preparation of resumable runs."""
from pathlib import Path
import hashlib,tempfile,threading,time,unittest
from unittest.mock import patch
from easysewer import get_native_capabilities
from easysewer.io.inp import InpDocument
from easysewer.model import Model
from easysewer.runtime import Runner,RunResult,RunnerCheckpoint,FileArtifact
from easysewer.runtime import runner as rm
from test_runner_v2 import config
from test_files_v2 import bind
from test_options_v2 import network
from test_native_v2_runner_checkpoint import schedule,resume_config
import test_native_v2_cache_reuse as cache_fixture
EVIDENCE=[]
@unittest.skipUnless(get_native_capabilities()['swmm_solver'] and get_native_capabilities()['flexible_ponding'],'Both real backends required')
class NativePreparationResumeTests(unittest.TestCase):
    def old_outputs(self,target):
        target.mkdir()
        for name in ('model.inp','model.rpt','model.out'):(target/name).write_bytes(b'previous')
    def verify_failure(self,result,target,status,stage,retained,save):
        self.assertEqual(result.status,status,result.failure);self.assertEqual(result.failure.stage,stage)
        self.assertFalse(result.native_completed);self.assertEqual(result.retained_directory is not None,retained)
        for name in ('model.inp','model.rpt','model.out'):self.assertEqual((target/name).read_bytes(),b'previous')
        self.assertFalse(list(target.rglob('.easysewer-lock-*')))
        result.save(save);loaded=RunResult.load(save);self.assertEqual(loaded.failure,result.failure);self.assertEqual(loaded.status,result.status);self.assertEqual(loaded.continuations,result.continuations)
    def test_actual_producer_artifact_verification_is_cancellable(self):
        for backend in ('swmm:standard','easysewer:flexible-ponding'):
            with self.subTest(backend=backend),tempfile.TemporaryDirectory() as directory:
                root=Path(directory);fixture=cache_fixture.NativeCacheReuseTests('test_both_families_match_metadata_changes_and_reject_physical_edits_before_open')
                base,evidence,producer=fixture.produce(root,backend);self.assertTrue(producer.succeeded,producer.failure)
                original_bytes=evidence.artifact.read_bytes();consumer=base.copy();bind(consumer,'RUNOFF','USE',evidence.artifact.path)
                consumer_baseline=Runner().run(consumer,config(root/'consumer-baseline',backend=backend,cache_reuse=(('RUNOFF','require_match'),)),producers={'RUNOFF':evidence})
                self.assertTrue(consumer_baseline.succeeded,consumer_baseline.failure)
                before=consumer.to_json_document().to_bytes()
                for status in ('cancelled','timed_out'):
                    for keep in (False,True):
                        target=root/(status+str(keep));self.old_outputs(target);active=[];checks=[];done=[];event=threading.Event();triggered=[]
                        original=FileArtifact.read_bytes;original_check=rm._Cancellation.check
                        def read(value):
                            enabled=value is evidence.artifact
                            if enabled:active.append(True)
                            try:
                                data=original(value)
                                if enabled:done.append(True)
                                return data
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
                        with patch.object(FileArtifact,'read_bytes',read),patch.object(rm._Cancellation,'check',check):
                            result=Runner().run(consumer,config(target,backend=backend,overwrite=True,keep_failed_artifacts=keep,cache_reuse=(('RUNOFF','require_match'),)),producers={'RUNOFF':evidence},cancel_event=event)
                        returned=time.monotonic();self.assertEqual(len(checks),3);self.assertFalse(done)
                        self.verify_failure(result,target,status,'preflight',keep,root/('saved-'+status+str(keep)))
                        self.assertEqual(evidence.artifact.read_bytes(),original_bytes);self.assertEqual(consumer.to_json_document().to_bytes(),before)
                        EVIDENCE.append(dict(kind='producer-artifact',backend=backend,status=status,keep=keep,checks=3,stage='preflight',old_outputs_preserved=True,producer_preserved=True,model_unchanged=True,native_completed=False,return_seconds=returned-triggered[0]))
                again=Runner().run(consumer,config(root/'successful-retry',backend=backend,cache_reuse=(('RUNOFF','require_match'),)),producers={'RUNOFF':evidence})
                self.assertTrue(again.succeeded,again.failure);self.assertEqual(again.output.read_bytes(),consumer_baseline.output.read_bytes())
                EVIDENCE.append(dict(kind='producer-retry',backend=backend,complete_output_equal=True,output_sha256=hashlib.sha256(again.output.read_bytes()).hexdigest()))
    def test_resume_parse_consumers_and_final_input_keep_checkpoint_reusable(self):
        for backend in ('swmm:standard','easysewer:flexible-ponding'):
            with self.subTest(backend=backend),tempfile.TemporaryDirectory() as directory:
                root=Path(directory);saved=[];model=network();model.update_options(flow_routing='DYNWAVE',allow_ponding=True)
                baseline=Runner().run(model,config(root/'original',backend=backend),checkpoints=schedule(root/'checkpoints',saved.append))
                self.assertTrue(baseline.succeeded,baseline.failure);self.assertTrue(saved)
                checkpoint=RunnerCheckpoint.load(saved[0].directory)
                before={p.relative_to(checkpoint.directory).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in checkpoint.directory.rglob('*') if p.is_file()}
                for phase in ('parse','consumers','input_verify'):
                    for status in ('cancelled','timed_out'):
                        for keep in (False,True):
                            label=phase+status+str(keep);target=root/label;self.old_outputs(target)
                            preparing=[];active=[];checks=[];done=[];event=threading.Event();triggered=[];original_check=rm._Cancellation.check
                            module,name={'parse':(InpDocument,'from_bytes'),'consumers':(Model,'file_uses'),'input_verify':(rm,'input_matches')}[phase]
                            original=getattr(module,name)
                            def entered(*a,**k):
                                enabled=bool(preparing)
                                if enabled:active.append(True)
                                try:
                                    result=original(*a,**k)
                                    if enabled:done.append(True)
                                    return result
                                finally:
                                    if enabled:active.pop()
                            def progress(value):
                                if value.phase=='preparing':preparing.append(True)
                            def check(control):
                                if active:
                                    checks.append(1)
                                    if len(checks)==3:
                                        triggered.append(time.monotonic())
                                        if status=='cancelled':event.set()
                                        else:control.deadline=time.monotonic()-1
                                return original_check(control)
                            with patch.object(module,name,entered),patch.object(rm._Cancellation,'check',check):
                                result=Runner().resume(checkpoint,resume_config(target,overwrite=True,keep_failed_artifacts=keep),cancel_event=event,progress=progress)
                            returned=time.monotonic();self.assertEqual(len(checks),3);self.assertFalse(done)
                            stage='open' if phase=='input_verify' else 'checkpoint_prepare'
                            self.verify_failure(result,target,status,stage,keep and phase!='parse',root/('saved-'+label))
                            after={p.relative_to(checkpoint.directory).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in checkpoint.directory.rglob('*') if p.is_file()};self.assertEqual(after,before)
                            EVIDENCE.append(dict(kind='resume-interruption',backend=backend,phase=phase,status=status,keep=keep,checks=3,stage=stage,old_outputs_preserved=True,checkpoint_preserved=True,native_completed=False,return_seconds=returned-triggered[0]))
                restored=Runner().resume(checkpoint,resume_config(root/'successful-resume'))
                self.assertTrue(restored.succeeded,restored.failure);self.assertEqual(restored.output.read_bytes(),baseline.output.read_bytes())
                EVIDENCE.append(dict(kind='resume-retry',backend=backend,complete_output_equal=True,output_sha256=hashlib.sha256(restored.output.read_bytes()).hexdigest()))
if __name__=='__main__':unittest.main()
