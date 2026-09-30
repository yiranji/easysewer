"""Actual isolated workers, bounded bootstrap and recoverable rejections."""
import hashlib
from dataclasses import replace
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import unittest
from unittest.mock import patch

from easysewer.runtime import StandardBackend, FlexiblePondingBackend, SessionError
from easysewer.runtime._checkpoint_container import load
from easysewer.runtime._process_session import CheckpointRequestRejected
import test_native_v2_checkpoint_lifecycle as fixture

EVIDENCE=[]


def backend(family):
    return (StandardBackend if family=='standard' else FlexiblePondingBackend)(
        library=os.environ['EASYSEWER_CHECKPOINT_'+family.upper()])


def finish(session, archive, folder, custom):
    for _ in range(20000):
        if session.step(max_steps=3).finished:break
    else:raise AssertionError('Did not finish')
    balance=session.end();session.report()
    result=session.execution_results() if custom else None
    session.close()
    hashes={}
    for item in archive.data['outputs']:
        raw=(folder/('output-'+str(item['index']))).read_bytes()
        if item['role']==0:raw=re.sub(rb'(?m)^[ \t]*(?:Analysis (?:begun|ended)|Total elapsed time)[^\r\n]*',b'',raw)
        if item['text']:raw=raw.replace(os.fsencode(session.working_directory),b'<workspace>')
        hashes[str(item['index'])]=dict(role=item['role'],sha256=hashlib.sha256(raw).hexdigest(),size=len(raw))
    trace=folder/'trace'
    return dict(outputs=hashes,trace=hashlib.sha256(trace.read_bytes()).hexdigest() if trace.exists() else None,
                balance=dict(zip(('runoff_percent','flow_percent','quality_percent'),balance.raw_percentages)),results=result)


@unittest.skipUnless(os.environ.get('EASYSEWER_CHECKPOINT_STANDARD') and os.environ.get('EASYSEWER_CHECKPOINT_CUSTOM'),
                     'Requires clean ABI2 candidate libraries')
class CheckpointRPCTests(unittest.TestCase):
    def test_provider_failure_and_cleanup_status_survive_native_rejection(self):
        from easysewer.runtime._process_session import _worker_command
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);first=root/'first';first.mkdir()
            with fixture.fixture.working(first):
                lifecycle=fixture.prepare(first,'standard','combined')
                try:
                    lifecycle.open_start();lifecycle.step(3);archive=lifecycle.capture(root/'saved')
                finally:lifecycle.cleanup()
            snapshot=archive.materialize(root/'second');command=_worker_command()
            command[-1]=command[-1].removesuffix('; main()')+'''
from dataclasses import replace
from easysewer.runtime._checkpoint_native import NativeCheckpoint
original_restore=NativeCheckpoint.restore
def failed_provider(self,payload,*,input_provider,output_provider):
 def fail(key):raise OSError('injected input provider failure')
 result=original_restore(self,payload,input_provider=fail,output_provider=output_provider)
 assert not result.committed and result.provider_errors
 # The callback failure is real; this cleanup status is synthetic transport coverage.
 return replace(result,cleanup_error=991)
NativeCheckpoint.restore=failed_provider
main()
'''
            with patch('easysewer.runtime._process_session._worker_command',return_value=command):
                with backend('standard').session(working_directory=root/'second') as session:
                    session._open_checkpoint(snapshot);pid=session.pid
                    with self.assertRaises(CheckpointRequestRejected) as raised:session._restore_checkpoint(root/'saved')
                    self.assertIn('injected input provider failure',raised.exception.failure.message)
                    self.assertEqual(raised.exception.failure.code,7)
                    self.assertEqual(raised.exception.cleanup[0].code,991)
                    self.assertEqual(session.pid,pid);self.assertIsNone(session.returncode)
                    self.assertFalse(list((root/'second').glob('.checkpoint-restore-*')))
                    session.step(max_steps=2)
            EVIDENCE.append(dict(kind='provider-failure',synthetic_cleanup_code=991))

    def test_capture_retains_primary_and_failed_workspace_cleanup(self):
        from easysewer.runtime._process_session import _worker_command
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            with fixture.fixture.working(root):
                lifecycle=fixture.prepare(root,'standard','all');snapshot=lifecycle.snapshot;lifecycle.cleanup()
            command=_worker_command()
            command[-1]=command[-1].removesuffix('; main()')+'''
from unittest.mock import patch
from easysewer.runtime._checkpoint_lifecycle import CheckpointLifecycle
original_capture=CheckpointLifecycle.capture
def failed_capture(self,*args,**kwargs):
 with patch('easysewer.runtime._checkpoint_container.os.fsync',side_effect=OSError('injected disk full')):
  with patch('easysewer.runtime._checkpoint_container.remove_owned_tree',side_effect=OSError('injected cleanup denied')):
   return original_capture(self,*args,**kwargs)
CheckpointLifecycle.capture=failed_capture
main()
'''
            with patch('easysewer.runtime._process_session._worker_command',return_value=command):
                with backend('standard').session(working_directory=root) as session:
                    session._open_checkpoint(snapshot);pid=session.pid
                    with self.assertRaises(CheckpointRequestRejected) as raised:session._capture_checkpoint(root/'rejected')
                    self.assertIn('injected disk full',raised.exception.failure.message)
                    self.assertEqual(len(raised.exception.cleanup),1)
                    self.assertIn('injected cleanup denied',raised.exception.cleanup[0].message)
                    self.assertTrue((root/'rejected').is_dir())
                    self.assertEqual(session.state,'STARTED');self.assertEqual(session.pid,pid)
                    session.step(max_steps=2)
            EVIDENCE.append(dict(kind='capture-cleanup'))

    def test_failure_after_actual_state_commit_is_not_reported_as_rejection(self):
        from easysewer.runtime._process_session import _worker_command
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);first=root/'first';first.mkdir()
            with fixture.fixture.working(first):
                lifecycle=fixture.prepare(first,'custom','normal')
                try:
                    lifecycle.open_start();lifecycle.step(3);archive=lifecycle.capture(root/'saved')
                finally:lifecycle.cleanup()
            snapshot=archive.materialize(root/'second')
            command=_worker_command()
            command[-1]=command[-1].removesuffix('; main()')+'''
from easysewer.runtime import _checkpoint_worker as owner
original_apply=owner.PreparedWorker.apply
def fail_after_commit(self):
 original_apply(self)
 raise OSError('injected failure after native and Python state commit')
owner.PreparedWorker.apply=fail_after_commit
main()
'''
            with patch('easysewer.runtime._process_session._worker_command',return_value=command):
                with backend('custom').session(working_directory=root/'second') as session:
                    session._open_checkpoint(snapshot)
                    with self.assertRaises(SessionError) as raised:session._restore_checkpoint(root/'saved')
                    self.assertNotIsInstance(raised.exception,CheckpointRequestRejected)
                    self.assertTrue(raised.exception.checkpoint_committed)
                    self.assertIn('after native and Python state commit',raised.exception.failure.message)
                    self.assertEqual(session.state,'FAILED');self.assertIsNotNone(session.returncode)
            EVIDENCE.append(dict(kind='committed-failure'))

    def test_bootstrap_larger_than_rpc_frame_is_transferred_outside_the_pipe(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            with fixture.fixture.working(root):
                lifecycle=fixture.prepare(root,'standard','all');snapshot=lifecycle.snapshot;lifecycle.cleanup()
            # Whitespace preserves the exact same valid model JSON semantics.
            model=b' '*(17*1024**2)+snapshot.model_json
            snapshot=replace(snapshot,model_json=model,model_sha256=hashlib.sha256(model).hexdigest())
            with backend('standard').session(working_directory=root,call_timeout=60) as session:
                session._open_checkpoint(snapshot);session.step(max_steps=3)
                saved=session._capture_checkpoint(root/'large-saved')
                loaded=load(saved['directory'])
                self.assertEqual(loaded.snapshot.model_json,model)
                self.assertEqual(session.state,'STARTED');self.assertIsNone(session.returncode)
            EVIDENCE.append(dict(kind='large-bootstrap',model_json_bytes=len(model)))

    def test_real_worker_restores_and_survives_capture_and_restore_rejections(self):
        for family,case in (('standard','all'),('standard','combined'),('custom','normal'),('custom','climate')):
            with self.subTest(family=family,case=case),tempfile.TemporaryDirectory() as directory:
                root=Path(directory);first=root/'first';first.mkdir()
                with fixture.fixture.working(first):
                    lifecycle=fixture.prepare(first,family,case)
                    try:
                        lifecycle.open_start();lifecycle.step(3);archive=lifecycle.capture(root/'saved')
                        expected=fixture.finish(lifecycle)
                    finally:lifecycle.cleanup()
                shutil.rmtree(first);snapshot=archive.materialize(root/'second')
                with backend(family).session(working_directory=root/'second') as session:
                    session._open_checkpoint(snapshot);pid=session.pid
                    self.assertFalse(list((root/'second').glob('.checkpoint-context-*')))
                    session.step(max_steps=2)
                    saved=session._capture_checkpoint(root/'worker-saved')
                    self.assertEqual(hashlib.sha256(load(saved['directory']).manifest).hexdigest(),saved['sha256'])
                    for command,path in (('capture',root/'worker-saved'),('restore',root/'missing')):
                        with self.assertRaises(CheckpointRequestRejected) as rejected:
                            (session._capture_checkpoint if command=='capture' else session._restore_checkpoint)(path)
                        self.assertFalse(rejected.exception.checkpoint_committed)
                        self.assertEqual(session.state,'STARTED');self.assertEqual(session.pid,pid)
                        self.assertIsNone(session.returncode);self.assertIsNone(session.failure)
                    broken=root/'broken';shutil.copytree(root/'saved',broken)
                    blob=broken/'blobs'/archive.data['native_state']['sha256']
                    raw=blob.read_bytes();blob.write_bytes(b'X'+raw[1:])
                    with self.assertRaises(CheckpointRequestRejected):session._restore_checkpoint(broken)
                    outcome=session._restore_checkpoint(root/'saved')
                    self.assertTrue(outcome['native']['committed'])
                    self.assertEqual((outcome['native']['error'],outcome['native']['cleanup_error'],outcome['cleanup']),(0,0,[]))
                    self.assertEqual(session.pid,pid)
                    session._capture_checkpoint(root/'after-restore')
                    observed=finish(session,archive,Path(outcome['output_directory']),family=='custom')
                    self.assertEqual(observed,expected)
                    self.assertEqual(session.returncode,0)
                EVIDENCE.append(dict(kind='worker-restore',family=family,case=case,result=observed,rejections=3))

    def test_mutated_started_input_is_fatal_instead_of_recoverable(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            with fixture.fixture.working(root):
                lifecycle=fixture.prepare(root,'standard','combined')
                snapshot=lifecycle.snapshot;lifecycle.cleanup()
            with backend('standard').session(working_directory=root) as session:
                session._open_checkpoint(snapshot)
                path=root/'model.inp';path.write_bytes(path.read_bytes()+b'\n')
                with self.assertRaises(SessionError) as failed:session._capture_checkpoint(root/'rejected')
                self.assertNotIsInstance(failed.exception,CheckpointRequestRejected)
                self.assertFalse(failed.exception.checkpoint_committed)
                self.assertEqual(session.state,'FAILED');self.assertIsNotNone(session.returncode)
                self.assertFalse((root/'rejected').exists())
            EVIDENCE.append(dict(kind='fatal-input-change'))
