"""Result persistence is strict data loading, independent of native libraries."""

from dataclasses import replace
from datetime import timedelta
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from easysewer.io.json import JsonDocument
from easysewer.io.report_document import ReportCapture, ReportDocument
from easysewer.runtime import FileArtifact, NativeFailure, RunFailure, RunResult, Runner
from easysewer.validation import Diagnostic, Severity, SourceSpan, ValidationReport


def failure_result(status='failed'):
    return RunResult(run_id='fixture', status=status,
        diagnostics=ValidationReport(diagnostics=(Diagnostic(code='example:failure', message='原始错误',
            severity=Severity.ERROR, span=SourceSpan(line=7,column=2,end_column=4,source='original.inp')),)),
        failure=RunFailure(stage='step', exception_type='SessionError', message='primary',
            native=NativeFailure(stage='step',code=101,message='native primary'),
            cleanup=(NativeFailure(stage='close',code=306,message='secondary'),),
            stderr='raw stderr \ufffd\n', worker_returncode=-11),
        failure_report=ReportCapture(document=ReportDocument.from_bytes(b'bad \xff report\r\n',source='old.rpt'),truncated=True),
        backend_results=JsonDocument.from_bytes(b'{"future:field": [1, 2]}\n',source='observed'))


class ResultArchiveTests(unittest.TestCase):
    def test_actual_runner_rejection_cancellation_and_worker_timeout_roundtrip(self):
        from test_options_v2 import network
        from test_runner_v2 import config
        from test_backend_v2 import BackendContractTests
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);event=threading.Event();event.set()
            rejected=Runner(backends={}).run(network(),config(root/'unused'))
            cancelled=Runner().run(network(),config(root/'unused'),cancel_event=event)
            self.assertEqual((rejected.status,cancelled.status),('rejected','cancelled'))
            for value in (rejected,cancelled):
                value.save(root/value.status)
                self.assertEqual(RunResult.load(root/value.status),value)
        with BackendContractTests().fixture('hang:step') as (root,backend):
            value=Runner(backends={'swmm:standard':backend}).run(network(),config(root/'run',
                native_call_timeout=timedelta(seconds=.5),cancellation_poll_interval=timedelta(seconds=.01),
                keep_failed_artifacts=False))
            self.assertEqual(value.status,'timed_out',value.failure)
            value.save(root/'archive')
            self.assertEqual(RunResult.load(root/'archive'),value)

    def test_all_unsuccessful_statuses_roundtrip_without_native_or_original_files(self):
        for status in ('rejected','failed','cancelled','timed_out'):
            with self.subTest(status=status),tempfile.TemporaryDirectory() as directory:
                result=failure_result(status)
                root=Path(directory)
                with patch('ctypes.CDLL',side_effect=AssertionError('Archive must not load native code')):
                    result.save(root/'original')
                    (root/'original').rename(root/'moved')
                    loaded=RunResult.load(root/'moved')
                self.assertEqual(loaded,result)
                self.assertEqual(loaded.failure.stderr,'raw stderr \ufffd\n')
                self.assertTrue(loaded.failure_report.truncated)
                self.assertIsNone(loaded.failure_report.document.text)

    def test_partial_artifact_moves_and_later_changes_are_detected(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);raw=b'incomplete output';source=root/'partial.out';source.write_bytes(raw)
            artifact=FileArtifact(role='run:output',path=str(source),sha256=hashlib.sha256(raw).hexdigest(),
                size=len(raw),complete=False,declared_path='requested.out')
            result=replace(failure_result(),artifacts=(artifact,),retained_directory=str(root))
            result.save(root/'archive');source.unlink()
            (root/'archive').rename(root/'moved')
            loaded=RunResult.load(root/'moved')
            self.assertEqual(loaded.output.read_bytes(),raw)
            self.assertFalse(loaded.output.complete)
            self.assertEqual(loaded.retained_directory,result.retained_directory)  # historical provenance
            self.assertEqual(loaded.output.declared_path,'requested.out')
            Path(loaded.output.path).write_bytes(b'changed')
            with self.assertRaises(ValueError):loaded.output.read_bytes()
            with self.assertRaises(ValueError):RunResult.load(root/'moved')

    def test_existing_destinations_and_failed_save_are_not_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);target=root/'existing';target.mkdir();(target/'keep').write_bytes(b'old')
            with self.assertRaises(FileExistsError):failure_result().save(target)
            self.assertEqual((target/'keep').read_bytes(),b'old')
            with self.assertRaisesRegex(ValueError,'max_bytes'):failure_result().save(root/'limited',max_bytes=1)
            self.assertFalse((root/'limited').exists())
            with patch('easysewer.runtime.archive.os.fsync',side_effect=OSError('disk failed')):
                with self.assertRaises(OSError):failure_result().save(root/'fault')
            self.assertFalse((root/'fault').exists())

    def test_missing_modified_unlisted_and_oversized_content_are_rejected(self):
        for mode in ('missing','changed','extra','budget','manifest-budget'):
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as directory:
                target=Path(directory)/'archive';failure_result().save(target)
                blob=next((target/'blobs').iterdir())
                if mode=='missing':blob.unlink()
                elif mode=='changed':blob.write_bytes(b'x'*blob.stat().st_size)
                elif mode=='extra':(target/'blobs'/'unexpected').write_bytes(b'unknown')
                options={'max_bytes':1} if mode=='budget' else {'max_manifest_bytes':1} if mode=='manifest-budget' else {}
                with self.assertRaises((ValueError,FileNotFoundError)):RunResult.load(target,**options)

    def test_unknown_types_duplicate_keys_bad_hashes_and_forged_success_are_rejected(self):
        for mode in ('version','type','extra-field','boolean-code','traversal','duplicate','success','decoded-text'):
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as directory:
                target=Path(directory)/'archive';failure_result().save(target)
                path=target/'result.json';data=json.loads(path.read_bytes());result=data['result']['fields']
                if mode=='version':data['schema_version']='2.0'
                elif mode=='type':data['result']['type']='os:system'
                elif mode=='extra-field':result['unexpected']='no'
                elif mode=='boolean-code':result['failure']['fields']['native']['fields']['code']=True
                elif mode=='traversal':data['blobs'][0]['sha256']='../outside'
                elif mode=='success':result['status']='succeeded'
                elif mode=='decoded-text':result['failure_report']['fields']['document']['decoded_sha256']='0'*64
                text=json.dumps(data)
                if mode=='duplicate':
                    version=json.dumps(data['schema_version'])
                    text=text.replace('"schema_version": '+version,'"schema_version": '+version+', "schema_version": '+version)
                path.write_text(text,encoding='utf-8')
                with self.assertRaises((ValueError,TypeError)):RunResult.load(target)

    def test_public_result_and_artifact_invariants_fail_before_io(self):
        value=failure_result()
        for change in ({'status':'unknown'},{'status':'succeeded'},{'native_completed':1},
                       {'artifacts':[]},{'failure':None,'diagnostics':ValidationReport()},
                       {'backend_results':{'mutable':True}}):
            with self.subTest(change=change),self.assertRaises((ValueError,TypeError)):replace(value,**change)
        for change in ({'size':True},{'sha256':None},{'complete':1},{'field':[]}):
            args=dict(role='run:output',path='does-not-exist',sha256='a'*64,size=10,complete=False)
            args.update(change)
            with self.subTest(change=change),self.assertRaises((ValueError,TypeError)):FileArtifact(**args)

    def test_metadata_failure_capture_and_resave_preserve_unknown_json_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);original=failure_result()
            original.save(root/'one')
            loaded=RunResult.load(root/'one');loaded.save(root/'two')
            twice=RunResult.load(root/'two')
            self.assertEqual(twice,original)
            self.assertEqual(twice.backend_results.to_bytes(),original.backend_results.to_bytes())
            self.assertEqual((root/'one'/'result.json').read_bytes(),(root/'two'/'result.json').read_bytes())
