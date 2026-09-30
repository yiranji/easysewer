"""Actual Runner capture, moved resume, lineage and publication contracts."""
from dataclasses import replace
from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from easysewer.io.inp import InpDocument
from easysewer.model import FileReference, Model
from easysewer.runtime import (CheckpointSchedule, ResumeConfig, Runner, RunnerCheckpoint,
                              RunResult, ReportReadOptions)
from test_options_v2 import network
from test_runner_v2 import config
from test_files_v2 import bind
from easysewer.runtime import StandardBackend, FlexiblePondingBackend

EVIDENCE=[]

# Test-only capability declarations for the exact separately qualified ABI2
# artifacts (coordinator-final-checks.json). An arbitrary external library must
# never acquire packaged I/O guarantees through version or ABI checks alone.
QUALIFIED={
    'standard':{'Windows':'0c7df28ac70bcbaa4adea7dbd6d4d86147a773aac8f1858e2aa89482c283cd1e',
                'Linux':'d1760121fbb708a6561b2747fb38550fdd9cc34ad82cb7a222d1d7fa4da66087'},
    'custom':{'Windows':'2a6caa5513a129d666f29b49340a7f1b4784bb5799a4e005bf03b516e20e5e64',
              'Linux':'2d24bcecb7edfddc77b76fe4fcfb75d52a6920c0e75d2e43f107e738d09d0ec7'}}


class QualifiedCandidate:
    def execution_info(self,info):
        value=super().execution_info(info)
        family='standard' if self.key=='swmm:standard' else 'custom'
        if value.sha256!=QUALIFIED[family].get(value.platform):raise ValueError('Unqualified checkpoint test artifact')
        capabilities=tuple('easysewer:'+name+':1' for name in ('runoff-physics','runoff-rain-clock',
            'rdii-io','routing-io','solver-output-io','climate-io','timeseries-io','report-io','lid-report-io'))
        semantics=(('swmm:runoff-replay','easysewer:runoff-physics:1'),('swmm:runoff-rain-clock','easysewer:runoff-rain-clock:1'))
        return replace(value,capabilities=value.capabilities+capabilities,
                       output_semantics=value.output_semantics+semantics)


class CandidateStandard(QualifiedCandidate,StandardBackend):pass
class CandidateCustom(QualifiedCandidate,FlexiblePondingBackend):pass


def backend(family):
    if os.environ.get('EASYSEWER_CHECKPOINT_PACKAGED')=='1':
        return (StandardBackend if family=='standard' else FlexiblePondingBackend)()
    path=os.environ['EASYSEWER_CHECKPOINT_'+family.upper()]
    # Preserve the historical candidate audit; every other artifact must use
    # the real backend's identity policy, with no test capability injection.
    if hashlib.sha256(Path(path).read_bytes()).hexdigest() in QUALIFIED[family].values():
        cls=CandidateStandard if family=='standard' else CandidateCustom
    else:
        cls=StandardBackend if family=='standard' else FlexiblePondingBackend
    return cls(library=path)


def runner(family):
    value=backend(family)
    return Runner(backends={value.key:value})


def schedule(path, callback=None, seconds=120):
    return CheckpointSchedule(directory=FileReference(path=str(path),direction='output'),
                              interval=timedelta(seconds=seconds),on_saved=callback)


def resume_config(path, **options):
    return ResumeConfig(output_directory=FileReference(path=str(path),direction='output'),**options)


def reports(raw):
    return re.sub(rb'(?m)^[ \t]*(?:Analysis (?:begun|ended)|Total elapsed time)[^\r\n]*',b'',raw)


def content(value,artifact):
    raw=artifact.read_bytes()
    if artifact.role=='run:report':raw=reports(raw)
    if artifact.role in ('run:report','swmm:lid-detail','swmm:interface.outflows'):
        raw=raw.replace(os.fsencode(value.snapshot.execution_directory),b'<workspace>')
    return raw


@unittest.skipUnless(os.environ.get('EASYSEWER_CHECKPOINT_STANDARD') and os.environ.get('EASYSEWER_CHECKPOINT_CUSTOM'),
                     'Requires ABI2 candidate libraries')
class NativeRunnerCheckpointTests(unittest.TestCase):
    def success(self,value):
        self.assertTrue(value.succeeded,(value.failure,value.diagnostics))

    def original(self,root,family,*,callback=None,checkpoints=True,model=None):
        model=model or network();model.update_options(allow_ponding=True)
        bind(model,'HOTSTART','SAVE',root/'original.hsf')
        saved=[]
        def capture(value):
            saved.append(value)
            if callback:callback(value)
        value=runner(family).run(model,config(root/'first',backend=backend(family).key,step_batch_size=7,
            report_read=ReportReadOptions(tables=('swmm:node_depth','swmm:node_flooding'))),
            checkpoints=schedule(root/'saved',capture) if checkpoints else None)
        return value,saved

    def equivalent(self,before,after):
        self.success(after)
        self.assertEqual(after.run_id,before.run_id)
        self.assertEqual(after.output.read_bytes(),before.output.read_bytes())
        self.assertEqual(content(after,after.report),content(before,before.report))
        self.assertEqual(after.mass_balance,before.mass_balance)
        self.assertEqual(after.engine_objects,before.engine_objects)
        self.assertEqual(after.backend_results,before.backend_results)
        self.assertEqual(after.consumed_caches,before.consumed_caches)
        self.assertEqual(len(after.produced_caches),len(before.produced_caches))
        for a,b in zip(before.produced_caches,after.produced_caches):
            self.assertEqual(a.artifact.read_bytes(),b.artifact.read_bytes())
            self.assertEqual(a.manifest,b.manifest)
            self.assertEqual(a.reuse_evidence,b.reuse_evidence)
        a=[v for v in before.artifacts if v.role=='easysewer:flexible-ponding-steps']
        b=[v for v in after.artifacts if v.role=='easysewer:flexible-ponding-steps']
        self.assertEqual([v.read_bytes() for v in a],[v.read_bytes() for v in b])
        for artifact in before.artifacts:
            if artifact.role.startswith('swmm:'):
                other=after.artifact(artifact.role,owner=artifact.owner,field=artifact.field)
                self.assertEqual(content(before,artifact),content(after,other),artifact.role)

    def test_move_repeat_resume_publish_and_result_archive(self):
        for family in ('standard','custom'):
            with self.subTest(family=family),tempfile.TemporaryDirectory() as directory:
                root=Path(directory);original,saved=self.original(root,family);self.success(original)
                self.assertTrue(saved);original.save(root/'expected')
                expected=RunResult.load(root/'expected')
                shutil.rmtree(root/'first');(root/'original.hsf').unlink()
                (root/'saved').rename(root/'moved')
                checkpoint=RunnerCheckpoint.load(root/'moved'/saved[0].directory.name)
                again=[];events=[]
                restored=runner(family).resume(checkpoint,resume_config(root/'second',step_batch_size=9),
                    progress=events.append,checkpoints=schedule(root/'next',again.append))
                self.equivalent(expected,restored)
                self.assertFalse((root/'original.hsf').exists())
                self.assertEqual(len(restored.continuations),1)
                self.assertEqual(restored.continuations[0].checkpoint_sha256,checkpoint.sha256)
                self.assertEqual(next(e for e in events if e.phase=='resumed').simulation_seconds,checkpoint.simulation_seconds)
                self.assertTrue(all(e.simulation_seconds>=checkpoint.simulation_seconds for e in events))
                self.assertTrue(again)
                record=json.loads(restored.artifact('run:execution-record').read_bytes())
                self.assertEqual(record['schema_version'],'1.1')
                self.assertEqual(record['continuations'][0]['attempt_id'],restored.continuations[0].attempt_id)
                restored.save(root/'archive');loaded=RunResult.load(root/'archive')
                self.equivalent(expected,loaded)
                self.assertEqual(loaded.continuations,restored.continuations)
                shutil.rmtree(root/'second')
                third=runner(family).resume(again[0],resume_config(root/'third',step_batch_size=11))
                self.equivalent(expected,third)
                self.assertEqual(len(third.continuations),2)
                self.assertNotEqual(third.continuations[0].attempt_id,third.continuations[1].attempt_id)
                EVIDENCE.append(dict(kind='runner-resume',family=family,checkpoints=len(saved),
                    output_sha256=third.output.sha256,continuations=2))

    def test_saved_callback_failure_keeps_checkpoint_and_can_resume(self):
        for family in ('standard','custom'):
            with self.subTest(family=family),tempfile.TemporaryDirectory() as directory:
                root=Path(directory)
                def fail(value):raise RuntimeError('stop after committed checkpoint')
                failed,saved=self.original(root,family,callback=fail)
                self.assertEqual(failed.status,'failed');self.assertEqual(failed.failure.stage,'checkpoint_callback')
                self.assertFalse((root/'first/model.out').exists())
                self.assertEqual(len(saved),1)
                loaded=RunnerCheckpoint.load(saved[0].directory)
                resumed=runner(family).resume(loaded,resume_config(root/'resumed'));self.success(resumed)
                baseline,_=self.original(root/'baseline',family,checkpoints=False);self.success(baseline)
                self.assertEqual(baseline.output.read_bytes(),resumed.output.read_bytes())
                EVIDENCE.append(dict(kind='saved-callback-failure',family=family))

    def test_resume_cancellation_and_callback_failures_preserve_lineage_and_old_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);original,saved=self.original(root,'standard');self.success(original)
            for mode in ('cancel','callback','finalizing','tamper','timed_out','publication','tamper-final'):
                with self.subTest(mode=mode):
                    target=root/mode;target.mkdir();event=threading.Event();clock_shift=[0.]
                    import time
                    monotonic=time.monotonic;real_replace=os.replace
                    def publish_replace(source,destination):
                        if mode=='publication' and Path(destination)==target/'model.out' and Path(source).name.startswith('.easysewer-publish-'):
                            raise OSError('publication fault')
                        return real_replace(source,destination)
                    for name in ('model.inp','model.rpt','model.out'):(target/name).write_bytes(b'old')
                    def progress(value):
                        if value.phase=='resumed':
                            if mode=='cancel':event.set()
                            if mode=='callback':raise RuntimeError('resumed callback failed')
                            if mode=='timed_out':clock_shift[0]=100000.
                            if mode=='tamper':
                                path=next(target.glob('.easysewer-*/execution/model.out'))
                                path.write_bytes(b'outside modification')
                        if value.phase=='finalizing' and mode=='finalizing':raise RuntimeError('publication callback failed')
                        if value.phase=='finalizing' and mode=='tamper-final':
                            next(target.glob('.easysewer-*/execution/model.out')).write_bytes(b'changed completed output')
                    with patch('easysewer.runtime.runner.time.monotonic',side_effect=lambda:monotonic()+clock_shift[0]),\
                         patch('easysewer.runtime._workspace.os.replace',side_effect=publish_replace):
                        value=runner('standard').resume(saved[0],resume_config(target,overwrite=True,
                            keep_failed_artifacts=mode!='publication',wall_time_limit=timedelta(hours=1)),progress=progress,cancel_event=event)
                    self.assertEqual(value.status,'cancelled' if mode=='cancel' else 'timed_out' if mode=='timed_out' else 'failed',value.failure)
                    self.assertEqual(len(value.continuations),1)
                    for name in ('model.inp','model.rpt','model.out'):self.assertEqual((target/name).read_bytes(),b'old')
                    self.assertTrue(value.failure_report)
                    self.assertIn(b'STORM WATER MANAGEMENT',value.failure_report.document.raw)
                    if mode=='publication':self.assertEqual(value.artifacts,());self.assertIsNone(value.retained_directory)
                    value.save(root/(mode+'-result'));loaded=RunResult.load(root/(mode+'-result'))
                    self.assertEqual(loaded.continuations,value.continuations)
                    EVIDENCE.append(dict(kind='runner-resume-failure',mode=mode,status=value.status,stage=value.failure.stage))

    def test_wrong_backend_and_changed_archive_are_rejected_before_session(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);original,saved=self.original(root,'standard');self.success(original)
            changed=replace(saved[0],manifest=saved[0].manifest+b' ')
            for value in (changed,saved[0]):
                selected=runner('standard')
                selected_backend=selected.backends['swmm:standard']
                info=selected_backend.probe()
                with patch.object(type(selected_backend),'probe',return_value=replace(info,sha256='0'*64)),\
                     patch.object(type(selected_backend),'session',side_effect=AssertionError('must not start')):
                    result=selected.resume(value,resume_config(root/'rejected'))
                self.assertEqual(result.status,'rejected',result.failure)
            self.assertFalse((root/'rejected').exists())

    def test_hydrology_streams_lid_snow_groundwater_and_custom_policy(self):
        from test_native_v2_checkpoint_coordinator import native_source
        from test_native_v2_checkpoint_worker import model_for
        from test_flexible_v2 import configuration
        from easysewer.runtime import FlexiblePondingPolicy
        from easysewer.io.cache_manifest import CacheManifest
        from easysewer.io.runoff_cache import RunoffLayout, RdiiLayout
        for family in ('standard','custom'):
            for case in ('all','combined','climate','rdii-binary','rdii-text','routing','gwater','snow','averages','report-disabled','no-routing','adaptive'):
                with self.subTest(family=family,case=case),tempfile.TemporaryDirectory() as directory:
                    root=Path(directory);inputs=root/'inputs';inputs.mkdir()
                    if case=='adaptive':model=model_for('adaptive')
                    else:
                        source=native_source(inputs,case)
                        if case=='combined':
                            source=source.replace('RainProbe CONTROL 0 0 1000 1',
                                'RainProbe CONTROL 0 0 1000 1\nIntensityProbe CONTROL 0 0 1000 1')
                            source=source.replace('THEN OUTLET Probe0 SETTING = CURVE RainProbe',
                                'THEN OUTLET Probe0 SETTING = CURVE IntensityProbe')
                        model=Model.from_document(InpDocument.from_text(source),strict=True)
                    if family=='custom':
                        model.update_options(allow_ponding=True)
                        settings=configuration(root/'first',FlexiblePondingPolicy(
                            external_flooding_ratio=.37,depth_threshold_m=0,flow_threshold_cms=0),step_batch_size=1 if case in ('adaptive','all','no-routing','climate','combined') else 100)
                    else:settings=config(root/'first',step_batch_size=1 if case in ('adaptive','all','no-routing','climate','combined') else 100)
                    duration=model.effective_options.duration.total_seconds();saved=[]
                    manifests={}
                    for use in model.file_uses():
                        kind=use.role.rsplit('.',1)[-1].upper()
                        if use.active and use.access!='write' and kind in ('RDII','RUNOFF') and case!='rdii-text':
                            layout=(RdiiLayout if kind=='RDII' else RunoffLayout).from_model(model)
                            manifests[kind]=CacheManifest.asserted(Path(use.file.path).read_bytes(),layout=layout)
                    original=runner(family).run(model,settings,relative_to=inputs,
                        interface_manifests=manifests,checkpoints=schedule(root/'saved',saved.append,seconds=duration/3))
                    if family=='custom' and case=='no-routing':
                        self.assertEqual(original.status,'rejected');self.assertIn('flexible.routing',original.failure.message)
                        self.assertFalse(saved);EVIDENCE.append(dict(kind='runner-unsupported',family=family,case=case))
                        continue
                    self.success(original);self.assertTrue(saved)
                    original.save(root/'expected');expected=RunResult.load(root/'expected')
                    shutil.rmtree(root/'first');shutil.rmtree(inputs)
                    resumed=runner(family).resume(saved[0],resume_config(root/'resumed',step_batch_size=83))
                    self.equivalent(expected,resumed)
                    self.assertFalse(inputs.exists())
                    if family=='custom' and case=='adaptive':self.assertGreater(resumed.backend_results.data['removed_volume'],0)
                    EVIDENCE.append(dict(kind='runner-domain',family=family,case=case,
                        output_sha256=resumed.output.sha256,artifacts=len(resumed.artifacts)))

    def test_fresh_parent_process_after_source_deletion(self):
        child='''import sys
from pathlib import Path
package_root,test_root=sys.argv[1:3];del sys.argv[1:3]
sys.path[:0]=[package_root,test_root]
import easysewer
assert Path(easysewer.__file__).resolve().parent.parent==Path(package_root).resolve()
from test_native_v2_runner_checkpoint import runner,resume_config
value=runner(sys.argv[1]).resume(sys.argv[2],resume_config(Path(sys.argv[3])))
assert value.succeeded,(value.failure,value.diagnostics)
value.save(sys.argv[4])
'''
        for family in ('standard','custom'):
            with self.subTest(family=family),tempfile.TemporaryDirectory() as directory:
                root=Path(directory);original,saved=self.original(root,family);self.success(original)
                original.save(root/'expected');expected=RunResult.load(root/'expected')
                shutil.rmtree(root/'first');(root/'original.hsf').unlink();(root/'saved').rename(root/'moved')
                import easysewer
                package_root=Path(easysewer.__file__).resolve().parent.parent
                test_root=Path(__file__).resolve().parent
                env=dict(os.environ,PYTHONPATH=os.pathsep.join((str(package_root),str(test_root))))
                outcome=subprocess.run([sys.executable,'-B','-c',child,str(package_root),str(test_root),family,
                    str(root/'moved'/saved[0].directory.name),str(root/'child'),str(root/'child-result')],
                    env=env,capture_output=True,text=True,timeout=90,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                self.assertEqual(outcome.returncode,0,outcome.stdout+outcome.stderr)
                value=RunResult.load(root/'child-result');self.equivalent(expected,value)
                self.assertEqual(len(value.continuations),1)
                EVIDENCE.append(dict(kind='runner-fresh-parent',family=family,fresh_parent=True,
                    original_workspace_removed=True,package_root=str(package_root)))

    def test_committed_malformed_response_omits_unverified_startup_outputs(self):
        from easysewer.runtime._process_session import ProcessSession
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);original,saved=self.original(root,'standard');self.success(original)
            real=ProcessSession._rpc
            def malformed(session,command,args,**kwargs):
                value=real(session,command,args,**kwargs)
                if command=='checkpoint_restore':value['output_directory']=str(root)
                return value
            with patch.object(ProcessSession,'_rpc',new=malformed):
                value=runner('standard').resume(saved[0],resume_config(root/'failed'))
            self.assertEqual(value.status,'failed');self.assertEqual(value.failure.stage,'checkpoint_restore')
            self.assertEqual(len(value.continuations),1)
            self.assertIsNone(value.failure_report)
            self.assertFalse(any(a.role in ('run:report','run:output','swmm:interface.hotstart') for a in value.artifacts))
            self.assertIn('run.checkpoint_committed',{d.code for d in value.diagnostics.diagnostics})
            self.assertIn('run.checkpoint_outputs_unavailable',{d.code for d in value.diagnostics.diagnostics})
            value.save(root/'failure-result');loaded=RunResult.load(root/'failure-result')
            self.assertEqual(loaded.continuations,value.continuations)
            self.success(runner('standard').resume(saved[0],resume_config(root/'healthy')))
            EVIDENCE.append(dict(kind='runner-committed-response-failure',fresh_recovery=True))
