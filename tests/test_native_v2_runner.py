"""Actual Runner snapshots, file publication and native numerical oracles."""

from dataclasses import replace
from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import unittest

from easysewer import get_native_capabilities
from easysewer.io.inp import InpDocument
from easysewer.io.output_metadata import NotRecordedError, OutputMetadata
from easysewer.model import FileReference, Model, Ref
from easysewer.model import climate as climate
from easysewer.model.report import ReportSelection
from easysewer.runtime import Runner, ReportReadOptions, RunError
from test_runner_v2 import config
from test_options_v2 import network
from test_climate_v2 import climate_model
from test_native_v2_climate import user_weather
from test_files_v2 import bind
import test_native_v2_project as direct
import test_native_v2_lid as lid_native
import test_quality_v2 as quality
from test_rdii_v2 import rdii_model


@unittest.skipUnless(get_native_capabilities()['swmm_solver'],'Native solver unavailable')
class NativeRunnerTests(unittest.TestCase):
    def check_success(self,result):
        self.assertTrue(result.succeeded,repr(result.failure)+' '+repr(result.diagnostics.errors))
        self.assertTrue(result.native_completed)
        self.assertIsNone(result.retained_directory)
        self.assertFalse(Path(result.snapshot.execution_directory).exists())
        self.assertEqual(result.snapshot.input_bytes,result.input.read_bytes())
        self.assertEqual(result.report_document.raw,result.report.read_bytes())
        record=json.loads(result.artifact('run:execution-record').read_bytes())
        self.assertEqual(record['input_sha256'],result.snapshot.input_sha256)
        self.assertEqual(record['backend']['sha256'],result.backend.sha256)
        self.assertTrue(record['native_completed'])
        self.assertEqual(result.artifact('run:model').read_bytes(),result.snapshot.model_json)
        self.assertEqual(result.artifact('run:executed-input').read_bytes(),result.snapshot.input_bytes)

    @unittest.skipUnless(get_native_capabilities()['swmm_output'],'Direct OUT oracle unavailable')
    def test_selected_out_identity_and_utf8_directory_match_direct_native_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);model=network();model.update_options(report_step=timedelta(seconds=30))
            # Native Windows path resolution uses the process ANSI code page.
            # Keep native scratch compatible while Python publishes Unicode paths.
            model.update_options(temp_directory=FileReference(path=str(root/'scratch'),direction='output'))
            model.update_report(nodes=ReportSelection(mode='SELECTED',members=(Ref(collection='swmm:nodes',key='J'),)),links=ReportSelection(mode='ALL'))
            original=model.to_json_document().to_bytes()
            (root/'scratch').mkdir()
            oracle=direct.NativeProjectTests().solve(root,'direct',model.to_document().text)
            def edit_original(progress):
                if progress.phase=='opening':model.nodes.rename('J','ChangedAfterSnapshot')
            result=model.run(config(root/'中文😀',report_read=ReportReadOptions(encoding='cp1252')),progress=edit_original)
            self.check_success(result)
            self.assertEqual(result.output.read_bytes(),(root/'direct.out').read_bytes())
            self.assertEqual(result.snapshot.model_json,original)
            self.assertIn('J',result.snapshot.model().nodes)
            self.assertNotIn('J',model.nodes)
            self.assertEqual(result.engine_objects.names('swmm:nodes'),('J','O'))
            self.assertEqual(result.output_metadata.names('swmm:nodes'),oracle['ids'][1])
            with self.assertRaises(NotRecordedError):result.output_metadata.index(Ref(collection='swmm:nodes',key='O'))
            self.assertEqual(result.output_metadata.index(Ref(collection='swmm:nodes',key='j')),0)

    def test_explicit_unicode_scratch_preserves_native_path_failure_and_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);scratch=root/'中文😀';destination=root/'published';destination.mkdir()
            for name in ('model.inp','model.rpt','model.out'):
                (destination/name).write_bytes(b'previous-success')
            model=network();model.update_options(temp_directory=FileReference(path=str(scratch),direction='output'))
            compatible=True
            if os.name=='nt':
                try:
                    text=str(scratch.resolve())
                    compatible=text.encode('mbcs',errors='strict').decode('mbcs')==text
                except UnicodeError:compatible=False
            result=Runner().run(model,config(destination,overwrite=True,keep_failed_artifacts=False))
            self.assertEqual(Path(result.snapshot.execution_directory).parent,scratch.resolve())
            if compatible:
                self.check_success(result)
            else:
                self.assertEqual(result.status,'failed')
                self.assertEqual((result.failure.native.stage,result.failure.native.code),('open',303))
                self.assertIn('input path cannot be resolved or exceeds native path buffer',result.failure.native.message)
                self.assertIn('run.native_path',{d.code for d in result.diagnostics.diagnostics})
                self.assertFalse(result.native_completed)
                self.assertIsNone(result.retained_directory)
                self.assertFalse(Path(result.snapshot.execution_directory).exists())
                for name in ('model.inp','model.rpt','model.out'):
                    self.assertEqual((destination/name).read_bytes(),b'previous-success')

    @unittest.skipUnless(get_native_capabilities()['swmm_output'],'Direct OUT oracle unavailable')
    def test_resource_capture_survives_source_edit_and_published_inp_can_run_again(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);weather=root/'天气.dat';user_weather(weather);raw=weather.read_bytes()
            model=climate_model();model.update_climate(file=climate.ClimateFile(file=FileReference(path=str(weather)),units='F'),
                wind=climate.FileWind(),evaporation=climate.Evaporation(source=climate.FileEvaporation()))
            # Direct native uses an ASCII filename; Runner handles the Unicode source path by capture.
            oracle_weather=root/'oracle.dat';oracle_weather.write_bytes(raw)
            oracle_model=model.copy();oracle_model.update_climate(file=replace(model.climate.file,file=FileReference(path=str(oracle_weather))))
            oracle_source=root/'oracle-source.inp';oracle_source.write_text(oracle_model.to_document().text,encoding='utf-8')
            # The legacy direct ABI leaks climate FILE* for this no-catchment
            # fixture. Isolate the independent oracle, too.
            package=str(Path(__file__).resolve().parents[1]/'src');tests=str(Path(__file__).resolve().parent)
            code='import sys;sys.path[:0]='+repr([package,tests])+';from pathlib import Path;from test_native_v2_project import NativeProjectTests;NativeProjectTests().solve(sys.argv[1],"direct",Path(sys.argv[2]).read_text(encoding="utf-8"),report_encoding="cp1252")'
            subprocess.run([sys.executable,'-I','-B','-c',code,str(root),str(oracle_source)],capture_output=True,check=True,timeout=30,
                creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            original=model.to_json_document().to_bytes()
            def mutate(progress):
                if progress.phase=='opening':user_weather(weather,evaporation=4)
            result=Runner().run(model,config(root/'run'),progress=mutate)
            self.check_success(result)
            self.assertEqual(result.output.read_bytes(),(root/'direct.out').read_bytes())
            self.assertEqual(result.snapshot.resources[0].sha256,hashlib.sha256(raw).hexdigest())
            self.assertEqual(result.artifact('run:resource').read_bytes(),raw)
            self.assertEqual(model.to_json_document().to_bytes(),original)
            rerun=Runner().run(Model.from_inp(result.input.path),config(root/'rerun'))
            self.check_success(rerun)
            self.assertEqual(result.output.read_bytes(),rerun.output.read_bytes())

    def test_callback_failure_preserves_all_existing_outputs_and_cleanup_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for retain in (True,False):
                folder=root/str(retain);folder.mkdir()
                for name in ('model.inp','model.rpt','model.out'):(folder/name).write_bytes(b'previous-success')
                error=RuntimeError('progress callback failed')
                def fail(progress):
                    if progress.phase=='running':raise error
                result=Runner().run(network(),config(folder,overwrite=True,keep_failed_artifacts=retain,step_batch_size=1),progress=fail)
                self.assertEqual(result.status,'failed');self.assertEqual(result.failure.stage,'callback')
                self.assertIn(str(error),result.failure.message)
                for name in ('model.inp','model.rpt','model.out'):self.assertEqual((folder/name).read_bytes(),b'previous-success')
                self.assertEqual(result.retained_directory is not None,retain)
                if retain:self.assertTrue(Path(result.retained_directory).is_dir());self.assertFalse(result.input.complete)
                else:self.assertFalse(list(folder.glob('.easysewer-*')))
            with self.assertRaises(RunError) as caught:
                Runner().run(network(),config(root/'cause',keep_failed_artifacts=False),progress=fail,raise_on_error=True)
            self.assertIs(caught.exception.__cause__,error)

    def test_native_failure_after_preflight_keeps_primary_and_never_publishes(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            # An explicit backend failure remains a failed run, not successful paths.
            from easysewer.runtime import StandardBackend
            backend=StandardBackend()
            class Broken:
                key=backend.key
                def probe(self,**kwargs):return backend.probe(**kwargs)
                def validate_profile(self,profile):return backend.validate_profile(profile)
                def session(self,**kwargs):
                    session=backend.session(**kwargs)
                    def broken_start(**options):
                        from easysewer.runtime import NativeFailure,SessionError
                        raise SessionError(NativeFailure(stage='start',code=777,message='injected native start failure'))
                    session.start=broken_start
                    return session
            result=Runner(backends={'swmm:standard':Broken()}).run(network(),config(root/'run'))
            self.assertEqual(result.failure.native.code,777)
            self.assertEqual(result.status,'failed')
            self.assertFalse((root/'run'/'model.out').exists())
            self.assertIsNotNone(result.retained_directory)

    def test_inputs_aliases_existing_destinations_and_unknown_inventory_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);source=root/'model.inp';source.write_bytes(network().to_document().to_bytes())
            model=Model.from_inp(source)
            result=Runner().run(model,config(root,overwrite=True,keep_failed_artifacts=False))
            self.assertFalse(result.succeeded)
            self.assertIn('aliases an input',result.failure.message)
            self.assertEqual(source.read_bytes(),model.document.to_bytes())
            unsupported=Model.from_document(InpDocument.from_text(network().to_document().text+'[UNKNOWN_FILE_DOMAIN]\nx dangerous.dat\n'))
            result=Runner().run(unsupported,config(root/'opaque'))
            self.assertEqual(result.status,'rejected')
            self.assertIn('run.opaque_consumers',{d.code for d in result.diagnostics.errors})

    def test_execution_encoding_is_validated_after_utf8_conversion(self):
        source='[TITLE]\n'+('é'*600)+'\n'+network().to_document().text
        model=Model.from_document(InpDocument.from_text(source,encoding='cp1252'))
        self.assertIn('inp.native_line_length', {d.code for d in model.validate(for_run=True).errors})
        with tempfile.TemporaryDirectory() as directory:
            result=Runner().run(model,config(Path(directory)/'run',keep_failed_artifacts=False))
            self.assertEqual(result.status,'rejected')
            self.assertIn('inp.native_line_length',{d.code for d in result.diagnostics.errors})
            self.assertIsNone(result.engine_objects)
            self.assertFalse(result.native_completed)

    def test_report_decode_policy_preserves_raw_bytes_or_fails_before_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            source='[TITLE]\nCafé\n'+network().to_document().text
            model=Model.from_document(InpDocument.from_text(source))
            preserved=Runner().run(model,config(root/'preserved',report_read=ReportReadOptions(encoding='ascii')))
            self.check_success(preserved)
            self.assertIsNone(preserved.report_document.text)
            self.assertEqual(preserved.report_document.raw,preserved.report.read_bytes())
            target=root/'strict';target.mkdir();(target/'model.rpt').write_bytes(b'previous-success')
            failed=Runner().run(model,config(target,overwrite=True,keep_failed_artifacts=False,
                report_read=ReportReadOptions(encoding='ascii',on_decode_error='raise')))
            self.assertEqual(failed.status,'failed')
            self.assertEqual(failed.failure.stage,'report_read')
            self.assertTrue(failed.native_completed)
            self.assertIsNone(failed.report_document)
            self.assertEqual((target/'model.rpt').read_bytes(),b'previous-success')

    def test_overlapping_runs_reserve_destinations_before_native_open(self):
        from concurrent.futures import ThreadPoolExecutor
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)/'run';entered=threading.Event();release=threading.Event()
            def wait(progress):
                if progress.phase=='preparing':
                    entered.set()
                    if not release.wait(15):raise TimeoutError('Test did not release capture')
            with ThreadPoolExecutor(max_workers=1) as pool:
                first=pool.submit(Runner().run,network(),config(root),progress=wait)
                try:
                    self.assertTrue(entered.wait(15))
                    second=Runner().run(network(),config(root))
                    self.assertFalse(second.succeeded)
                    self.assertIsNone(second.engine_objects)
                    self.assertEqual(second.failure.exception_type,'FileExistsError')
                finally:release.set()
                self.check_success(first.result(timeout=30))
            self.assertFalse(list(root.glob('.easysewer-*')))

    def test_hotstart_and_runoff_producer_evidence_binds_actual_bytes_layout_and_backend(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for kind in ('HOTSTART','RUNOFF','RDII'):
                with self.subTest(kind=kind):
                    factory=rdii_model if kind=='RDII' else quality.quality_model
                    model=factory();destination=root/(kind+'.cache')
                    bind(model,kind,'SAVE',destination)
                    before=model.to_json_document().to_bytes()
                    produced=Runner().run(model,config(root/(kind+'-producer')))
                    self.check_success(produced)
                    self.assertEqual(model.to_json_document().to_bytes(),before)
                    evidence,=produced.produced_caches
                    self.assertEqual(evidence.artifact.read_bytes(),destination.read_bytes())
                    self.assertEqual(evidence.manifest.producer_input_sha256,produced.snapshot.input_sha256)
                    target=factory();bind(target,kind,'USE',evidence.artifact.path)
                    path=Path(evidence.artifact.path);old_mode=path.stat().st_mode
                    try:
                        os.chmod(path,stat.S_IREAD)
                        consumed=Runner().run(target,config(root/(kind+'-consumer')),producers={kind:evidence})
                        self.check_success(consumed)
                    finally:os.chmod(path,old_mode)
                    self.assertEqual(consumed.consumed_caches[0].producer_run_id,produced.run_id)
                    self.assertEqual(consumed.consumed_caches[0].sha256,evidence.artifact.sha256)
                    if kind in ('RUNOFF','RDII'):
                        self.assertEqual(consumed.consumed_caches[0].reuse.status,'matched')
                        self.assertNotIn('run.cache_physical_scope',{d.code for d in consumed.diagnostics.diagnostics})
                    else:
                        self.assertEqual(consumed.consumed_caches[0].reuse.status,'changed')
                        self.assertIn('cache:hotstart-continuation-time',consumed.consumed_caches[0].reuse.differences)
                        self.assertIn('run.cache_physical_scope',{d.code for d in consumed.diagnostics.diagnostics})
                    # Same counts with a different identity must fail before open.
                    mismatch=factory()
                    if kind=='RDII':mismatch.nodes.rename('J','Different')
                    else:mismatch.subcatchments.rename('S','Different')
                    bind(mismatch,kind,'USE',path)
                    rejected=Runner().run(mismatch,config(root/(kind+'-mismatch'),keep_failed_artifacts=False),producers={kind:evidence})
                    self.assertEqual(rejected.status,'rejected',rejected.failure)
                    self.assertIsNone(rejected.engine_objects)

    def test_final_callback_cannot_replace_native_outputs_before_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'model.out').write_bytes(b'previous-success')
            def corrupt(progress):
                if progress.phase=='finalizing':
                    workspace=next(path for path in root.glob('.easysewer-*') if path.is_dir())
                    (workspace/'model.out').write_bytes(b'changed after native completion')
            result=Runner().run(network(),config(root,overwrite=True,keep_failed_artifacts=False),progress=corrupt)
            self.assertEqual(result.status,'failed')
            self.assertTrue(result.native_completed)
            self.assertIsNone(result.output_metadata)
            self.assertEqual((root/'model.out').read_bytes(),b'previous-success')
            self.assertIn('run.output_changed',{d.code for d in result.diagnostics.errors})

    def test_all_lid_detail_destinations_are_captured_and_published_with_their_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for kind in ('BC','RG','GR','IT','PP','RB','RD','VS'):
                with self.subTest(kind=kind):
                    destination=root/(kind+' detail.txt')
                    source=lid_native.literal_source(kind,detail=str(destination))
                    model=Model.from_document(InpDocument.from_text(source),strict=True)
                    result=Runner().run(model,config(root/kind))
                    self.check_success(result)
                    detail=result.artifact('swmm:lid-detail')
                    self.assertEqual(detail.read_bytes(),destination.read_bytes())
                    self.assertIsNotNone(detail.owner)

    def test_cancellation_during_steps_and_report_disabled_are_explicit(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);event=threading.Event()
            def cancel(progress):
                if progress.phase=='running':event.set()
            result=Runner().run(network(),config(root/'cancel',step_batch_size=1,keep_failed_artifacts=False),progress=cancel,cancel_event=event)
            self.assertEqual(result.status,'cancelled');self.assertFalse(result.native_completed)
            model=network();model.update_report(disabled=True,nodes=ReportSelection(mode='ALL'))
            result=Runner().run(model,config(root/'disabled'))
            self.check_success(result)
            self.assertNotIn(b'<<< Node',result.report.read_bytes())
            self.assertEqual(result.output_metadata.names('swmm:nodes'),('J','O'))

    def test_file_budget_and_explicit_temp_directory_control_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);weather=root/'weather.dat';user_weather(weather)
            model=climate_model();model.update_climate(file=climate.ClimateFile(file=FileReference(path=str(weather)),units='F'))
            temporary=root/'custom scratch';model.update_options(temp_directory=FileReference(path=str(temporary),direction='output'))
            result=Runner().run(model,config(root/'limited',file_inspection_limit=32,keep_failed_artifacts=False))
            self.assertEqual(result.status,'rejected')
            self.assertIn('run.incomplete_inspection',{d.code for d in result.diagnostics.errors})
            result=Runner().run(model,config(root/'good'))
            self.check_success(result)
            self.assertEqual(Path(result.snapshot.execution_directory).parent,temporary.resolve())

    def test_out_metadata_rejects_corrupt_completion_counts_names_and_variable_layout(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);model=network();model.update_report(nodes=ReportSelection(mode='ALL'))
            result=Runner().run(model,config(root/'run'));self.check_success(result)
            data=result.output.read_bytes();path=root/'broken.out'
            for change in ('magic','count','error','truncated','identity'):
                bad=bytearray(data)
                if change=='magic':struct.pack_into('<i',bad,0,0)
                elif change=='count':struct.pack_into('<i',bad,16,100000)
                elif change=='error':struct.pack_into('<i',bad,len(bad)-8,1)
                elif change=='truncated':bad=bad[:-1]
                else:struct.pack_into('<i',bad,28,100000)
                path.write_bytes(bad)
                with self.subTest(change=change),self.assertRaises(ValueError):OutputMetadata.read(path)


if __name__=='__main__':unittest.main()
