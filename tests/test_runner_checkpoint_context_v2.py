"""Runner context persistence; synthetic state does not claim native validity."""
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from easysewer.io.json import JsonDocument
from easysewer.runtime import Checkpoint, RunContinuation, RunResult
from easysewer.runtime._checkpoint_container import Builder, execution_digest
from easysewer.runtime._runner_checkpoint import RunnerContext, capture, load
from easysewer.validation import Diagnostic, Severity, SourceSpan, ValidationReport
import test_checkpoint_container_v2 as fixture
from test_result_archive_v2 import failure_result


def continuation(snapshot, **overrides):
    values=dict(attempt_id='resume-attempt',execution_run_id=snapshot.run_id,
        checkpoint_sha256='b'*64,state_sha256='c'*64,
        execution_sha256=execution_digest(snapshot,[]),started_at=datetime(2026,9,24,tzinfo=timezone.utc),
        simulation_seconds=1.,steps=1,config_json=snapshot.config_json)
    values.update(overrides)
    return RunContinuation(**values)


def context(snapshot):
    return RunnerContext(asset_directory='assets',steps=3,continuations=(continuation(snapshot),),
        diagnostics=ValidationReport(diagnostics=(Diagnostic(code='test:retained-warning',message='保留原始诊断',
            severity=Severity.WARNING,span=SourceSpan(line=7,column=1,end_column=4,source='old-input.inp')),)))


class FakeSession:
    def __init__(self,snapshot):self.snapshot=snapshot
    def save_checkpoint(self,directory):
        with Builder(directory,self.snapshot) as builder:
            builder.finish(fixture.native_prefix(self.snapshot,builder.binding),())
        return Checkpoint.load(directory)


def rewrite(root,change):
    path=root/'runner.json';data=json.loads(path.read_bytes());change(data)
    raw=JsonDocument.from_data(data).to_bytes();path.write_bytes(raw)
    (root/'runner.commit').write_bytes(hashlib.sha256(raw).hexdigest().encode()+b'\n')


class RunnerCheckpointContextTests(unittest.TestCase):
    def test_context_moves_and_materializes_without_native_or_old_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);snapshot=fixture.snapshot(root);value=context(snapshot)
            saved=capture(FakeSession(snapshot),value,root/'saved')
            (root/'saved').rename(root/'moved')
            with patch('ctypes.CDLL',side_effect=AssertionError('Offline context loaded native code')):
                loaded=load(root/'moved')
                self.assertEqual(loaded.context,value);self.assertEqual(loaded.sha256,saved.sha256)
                actual=loaded.materialize(root/'rebuilt')
                self.assertEqual(actual.input_bytes,snapshot.input_bytes)
                self.assertEqual(actual.execution_directory,str(root/'rebuilt'))
                with self.assertRaises(ValueError):replace(loaded,context=replace(value,steps=4)).materialize(root/'forged')
                self.assertFalse((root/'forged').exists())

    def test_manifest_identity_budget_and_context_forgery_are_rejected(self):
        for mode in ('missing-commit','state','version','extra','progress','execution','blob','budget','extra-file'):
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as directory:
                root=Path(directory);snapshot=fixture.snapshot(root)
                saved=capture(FakeSession(snapshot),context(snapshot),root/'saved');target=saved.directory
                if mode=='missing-commit':(target/'runner.commit').unlink()
                elif mode=='state':rewrite(target,lambda d:d.update(state_sha256='d'*64))
                elif mode=='version':rewrite(target,lambda d:d.update(codec_version='2.0'))
                elif mode=='extra':rewrite(target,lambda d:d['context'].update(unexpected=True))
                elif mode=='progress':rewrite(target,lambda d:d['context'].update(steps=0))
                elif mode=='execution':rewrite(target,lambda d:d['context']['continuations']['values'][0]['fields'].update(execution_sha256='d'*64))
                elif mode=='blob':next((target/'blobs').iterdir()).write_bytes(b'changed')
                elif mode=='extra-file':(target/'unexpected').write_bytes(b'x')
                options={'limits':replace(fixture.Limits(),total_bytes=1)} if mode=='budget' else {}
                with self.assertRaises((ValueError,TypeError,FileNotFoundError)):load(target,**options)

    def test_context_write_failure_and_cancellation_preserve_old_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);snapshot=fixture.snapshot(root);session=FakeSession(snapshot)
            first=capture(session,context(snapshot),root/'first');before=(root/'first/runner.json').read_bytes()
            with self.assertRaises(FileExistsError):capture(session,context(snapshot),root/'first')
            def cancel():
                if (root/'cancelled/state/checkpoint.commit').exists():raise KeyboardInterrupt('cancel outer metadata')
            with self.assertRaises(KeyboardInterrupt):capture(session,context(snapshot),root/'cancelled',checkpoint=cancel)
            self.assertFalse((root/'cancelled').exists())
            from easysewer.runtime import _runner_checkpoint as module
            real=module.os.fsync
            def fail(fd):
                if (root/'failed/runner.json').exists():raise OSError('context disk full')
                return real(fd)
            with patch.object(module.os,'fsync',side_effect=fail):
                with self.assertRaisesRegex(OSError,'context disk full'):capture(session,context(snapshot),root/'failed')
            self.assertFalse((root/'failed').exists())
            self.assertEqual((root/'first/runner.json').read_bytes(),before)
            self.assertEqual(load(first.directory).context,context(snapshot))
            with patch.object(module,'_context_data',side_effect=OSError('primary context failure')):
                with patch.object(module,'remove_owned_tree',side_effect=OSError('cleanup denied')):
                    with self.assertRaisesRegex(OSError,'primary context failure') as raised:
                        capture(session,context(snapshot),root/'cleanup-failed')
            self.assertIn('cleanup denied',raised.exception.runner_checkpoint_cleanup)
            self.assertTrue((root/'cleanup-failed').exists())
            with self.assertRaises(ValueError):load(root/'cleanup-failed')
            self.assertEqual(load(first.directory).context,context(snapshot))

    def test_continuation_history_survives_every_unsuccessful_result_without_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);snapshot=fixture.snapshot(root);history=(continuation(snapshot),)
            for status in ('failed','rejected','cancelled','timed_out'):
                value=replace(failure_result(status),run_id=snapshot.run_id,snapshot=snapshot,
                              backend=snapshot.backend,continuations=history)
                path=value.save(root/status)
                self.assertEqual(json.loads((path/'result.json').read_bytes())['schema_version'],'1.3')
                self.assertEqual(RunResult.load(path),value)
            for change in ({'attempt_id':''},{'state_sha256':None},{'steps':True},
                           {'simulation_seconds':float('nan')},{'started_at':datetime(2026,1,1)}):
                with self.subTest(change=change),self.assertRaises((ValueError,TypeError)):continuation(snapshot,**change)
            value=replace(failure_result(),run_id=snapshot.run_id,snapshot=snapshot,backend=snapshot.backend)
            for history in ((continuation(snapshot,execution_run_id='other'),),
                            (continuation(snapshot),continuation(snapshot)),
                            (continuation(snapshot,config_json=fixture.config(root,backend='other:backend').to_json_document().to_bytes()),)):
                with self.assertRaises(ValueError):replace(value,continuations=history)

    def test_explicit_legacy_codec_migration_never_drops_new_history(self):
        from easysewer.runtime.archive import _Blobs
        from easysewer.runtime._result_codec import Codec
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);target=root/'legacy';target.mkdir();(target/'blobs').mkdir()
            blobs=_Blobs(target/'blobs',8*1024**3)
            data=Codec(blobs,result_version='1.0').encode(failure_result())
            self.assertNotIn('continuations',data['fields'])
            manifest=dict(kind='easysewer:run-result',schema_version='1.0',result=data,
                          blobs=[dict(sha256=k,size=v) for k,v in sorted(blobs.inventory.items())])
            (target/'result.json').write_bytes(JsonDocument.from_data(manifest).to_bytes())
            old=RunResult.load(target);self.assertEqual(old,failure_result());self.assertEqual(old.continuations,())
            old.save(root/'new');self.assertEqual(RunResult.load(root/'new'),old)
            # A 1.0 envelope cannot smuggle new fields in, nor silently drop them.
            data['fields']['continuations']={'type':'core:tuple','values':[]}
            (target/'result.json').write_bytes(JsonDocument.from_data(manifest).to_bytes())
            with self.assertRaises(ValueError):RunResult.load(target)
            snapshot=fixture.snapshot(root)
            value=replace(failure_result(),run_id=snapshot.run_id,snapshot=snapshot,
                backend=snapshot.backend,continuations=(continuation(snapshot),))
            with self.assertRaisesRegex(ValueError,'cannot retain'):Codec(blobs,result_version='1.0').encode(value)
