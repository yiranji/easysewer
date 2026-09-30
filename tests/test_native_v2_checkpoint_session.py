"""Public Session save/restore with complete output mapping and worker containment."""
import hashlib
from dataclasses import replace
import os
from pathlib import Path
import re
import shutil
import tempfile
import unittest
from unittest.mock import patch

from easysewer.runtime import Checkpoint, CheckpointRejected, CheckpointSession, SessionError
import test_native_v2_checkpoint_lifecycle as fixture
from test_native_v2_checkpoint_rpc import backend

EVIDENCE=[]


def finish(session, custom):
    for _ in range(20000):
        if session.step(max_steps=3).finished:break
    else:raise AssertionError('Session did not finish')
    balance=session.end();session.report()
    result=session.execution_results() if custom else None
    session.close()
    files={}
    for item in session.checkpoint_outputs:
        raw=item.path.read_bytes()
        if item.role=='run:report':
            raw=re.sub(rb'(?m)^[ \t]*(?:Analysis (?:begun|ended)|Total elapsed time)[^\r\n]*',b'',raw)
        raw=raw.replace(os.fsencode(session.working_directory),b'<workspace>') if item.role in (
            'run:report','swmm:interface.OUTFLOWS','swmm:lid_report','easysewer:ponding-trace') else raw
        files[item.destination.relative_to(session.working_directory).as_posix()]=dict(
            role=item.role,sha256=hashlib.sha256(raw).hexdigest(),size=len(raw))
    return dict(files=files,balance=balance.raw_percentages,results=result)


@unittest.skipUnless(os.environ.get('EASYSEWER_CHECKPOINT_STANDARD') and os.environ.get('EASYSEWER_CHECKPOINT_CUSTOM'),
                     'Requires clean ABI2 candidate libraries')
class CheckpointSessionTests(unittest.TestCase):
    def test_startup_never_overwrites_existing_or_escaping_outputs(self):
        for mode in ('main','resource','escape'):
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as directory:
                root=Path(directory);work=root/'work';work.mkdir()
                with fixture.fixture.working(work):
                    owner=fixture.prepare(work,'standard','all');snapshot=owner.snapshot;owner.cleanup()
                output=next(v for v in snapshot.resources if v.active and v.access=='write' and v.kind=='file')
                target=work/'model.rpt' if mode=='main' else work/output.relative_path
                if mode=='escape':
                    target=root/'outside.bin'
                    source=snapshot.input_bytes.replace(output.relative_path.encode(),b'../outside.bin')
                    self.assertNotEqual(source,snapshot.input_bytes)
                    snapshot=replace(snapshot,input_bytes=source,input_sha256=hashlib.sha256(source).hexdigest())
                    (work/'model.inp').write_bytes(source)
                target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(b'KEEP EXISTING OUTPUT')
                with backend('standard').session(working_directory=work) as session:
                    with self.assertRaises(SessionError):session.open_checkpoint(snapshot)
                    self.assertEqual(session.state,'FAILED');self.assertIsNotNone(session.returncode)
                self.assertEqual(target.read_bytes(),b'KEEP EXISTING OUTPUT')
                self.assertFalse((work/'model.out').exists())
                if mode!='main':self.assertFalse((work/'model.rpt').exists())
                EVIDENCE.append(dict(kind='startup-output-protection',mode=mode))

    def test_malformed_success_after_real_restore_is_fatal_and_committed(self):
        for mode in ('directory','clock','commit'):
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as directory:
                root=Path(directory)
                with fixture.fixture.working(root):
                    owner=fixture.prepare(root,'standard','all');snapshot=owner.snapshot;owner.cleanup()
                with backend('standard').session(working_directory=root) as session:
                    session.open_checkpoint(snapshot);session.step(max_steps=3)
                    archive=session.save_checkpoint(root/'saved');rpc=session._rpc
                    def malformed(command,args,**kwargs):
                        value=rpc(command,args,**kwargs)
                        if command=='checkpoint_restore':
                            if mode=='directory':value['output_directory']=str(root.parent)
                            if mode=='clock':value['simulation_seconds']=float('nan')
                            if mode=='commit':value['native']['committed']=False
                        return value
                    with patch.object(session,'_rpc',side_effect=malformed):
                        with self.assertRaises(SessionError) as raised:session.restore_checkpoint(archive)
                    self.assertTrue(raised.exception.checkpoint_committed)
                    self.assertEqual(session.state,'FAILED');self.assertIsNotNone(session.returncode)
                EVIDENCE.append(dict(kind='committed-response-failure',mode=mode))

    def test_public_save_move_restore_and_output_mapping(self):
        for family,case in (('standard','all'),('standard','combined'),('custom','normal'),('custom','climate')):
            with self.subTest(family=family,case=case),tempfile.TemporaryDirectory() as directory:
                root=Path(directory);first=root/'first';first.mkdir()
                with fixture.fixture.working(first):
                    owner=fixture.prepare(first,family,case);snapshot=owner.snapshot;owner.cleanup()
                with backend(family).session(working_directory=first) as session:
                    self.assertIsInstance(session,CheckpointSession)
                    session.open_checkpoint(snapshot);session.step(max_steps=3)
                    archive=session.save_checkpoint(root/'saved')
                    self.assertIsInstance(archive,Checkpoint)
                    first_mapping=session.checkpoint_outputs
                    self.assertTrue(all(v.path==v.destination for v in first_mapping))
                    expected=finish(session,family=='custom')
                shutil.rmtree(first);(root/'saved').rename(root/'moved')
                archive=Checkpoint.load(root/'moved');snapshot=archive.materialize(root/'second')
                with backend(family).session(working_directory=root/'second') as session:
                    session.open_checkpoint(snapshot);pid=session.pid
                    session.step(max_steps=2)
                    later=session.save_checkpoint(root/'later')
                    for path in (root/'missing',):
                        with self.assertRaises(CheckpointRejected):session.restore_checkpoint(path)
                        self.assertEqual(session.pid,pid);self.assertEqual(session.state,'STARTED')
                    restored=session.restore_checkpoint(archive)
                    self.assertTrue(restored.committed);self.assertEqual(restored.cleanup,())
                    self.assertEqual(restored.simulation_seconds,archive.simulation_seconds)
                    self.assertEqual(restored.outputs,session.checkpoint_outputs)
                    self.assertTrue(all(v.path!=v.destination for v in restored.outputs))
                    old_mapping=restored.outputs
                    session.restore_checkpoint(later)
                    session.restore_checkpoint(archive)
                    self.assertNotEqual(old_mapping,session.checkpoint_outputs)
                    observed=finish(session,family=='custom')
                self.assertEqual(observed,expected)
                EVIDENCE.append(dict(kind='public-session',family=family,case=case,result=observed))

    def test_loaded_checkpoint_replacement_is_rejected_without_losing_session(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            with fixture.fixture.working(root):
                owner=fixture.prepare(root,'standard','all');snapshot=owner.snapshot;owner.cleanup()
            with backend('standard').session(working_directory=root) as session:
                session.open_checkpoint(snapshot);session.step(max_steps=3)
                saved=session.save_checkpoint(root/'saved');(root/'saved').rename(root/'old')
                session.step(max_steps=2);session.save_checkpoint(root/'saved');pid=session.pid
                with self.assertRaises(CheckpointRejected) as raised:session.restore_checkpoint(saved)
                self.assertFalse(raised.exception.checkpoint_committed)
                self.assertEqual(session.pid,pid);self.assertEqual(session.state,'STARTED')
                session.step(max_steps=2)
            EVIDENCE.append(dict(kind='archive-replaced'))

    def test_bootstrap_response_or_cleanup_failure_reaps_started_worker(self):
        for mode in ('response','cleanup','identities'):
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as directory:
                root=Path(directory)
                with fixture.fixture.working(root):
                    owner=fixture.prepare(root,'standard','all');snapshot=owner.snapshot;owner.cleanup()
                with backend('standard').session(working_directory=root) as session:
                    target='easysewer.runtime.'+('_checkpoint_context.read_outputs' if mode=='response' else '_workspace.remove_owned_tree')
                    if mode=='identities':
                        with self.assertRaises(SessionError):session.open_checkpoint(snapshot,expected=(('swmm:nodes',()),))
                    else:
                        with patch(target,side_effect=OSError('injected bootstrap '+mode)):
                            with self.assertRaises(SessionError) as raised:session.open_checkpoint(snapshot)
                            self.assertIn('injected bootstrap '+mode,str(raised.exception))
                    self.assertEqual(session.state,'FAILED');self.assertIsNotNone(session.returncode)
                    self.assertIsNotNone(session.failure)
                if mode!='cleanup':self.assertFalse(list(root.glob('.checkpoint-context-*')))
                EVIDENCE.append(dict(kind='bootstrap-parent-failure',mode=mode))
