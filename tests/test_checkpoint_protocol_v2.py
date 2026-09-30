"""Strict rejection envelopes and primary/commit preservation under transport loss."""
import unittest
from dataclasses import replace
from pathlib import Path
import sys
from unittest.mock import patch

from easysewer.runtime import SessionError
from easysewer.runtime._process_session import CheckpointRequestRejected
import test_backend_v2 as fixture
from test_checkpoint_container_v2 import snapshot


EXTRA = '''
    if stage in ('checkpoint_restore','step') and mode.startswith('checkpoint:'):
        mode=mode.split(':',1)[1]
        failure=dict(stage=stage,code=101,message='checkpoint failure')
        response=dict(id=q['id'],ok=False,rejected=True,state='STARTED',committed=False,failure=failure,cleanup=[])
        if mode in ('primary','committed-crash'):
            print(json.dumps(dict(id=q['id'],event='failure',failure=failure,checkpoint_committed=mode=='committed-crash')),flush=True)
            if mode=='committed-crash':os._exit(23)
        if mode=='commit':response['committed']=True
        if mode=='bool':response['committed']=0
        if mode=='state':response['state']='ENDED'
        if mode=='success':response.update(ok=True,value=None)
        if mode=='extra':response['unexpected']=1
        print(json.dumps(response),flush=True)
        mode='ok';continue
'''

STARTUP_FAILURE_WORKER = '''import os,platform,struct,sys,time
sys.path.insert(0,sys.argv[1])
from easysewer.runtime import _native_solver as n
class Fake:
 def __init__(self,library,**kw):
  self.open_attempted=self.start_attempted=self.ended=self.closed=False
  self.metadata=dict(library=library,sha256='f'*64,engine_version=52004,platform=platform.system(),architecture=platform.machine(),abi='cdecl:'+str(struct.calcsize('P')*8))
 def open(self,paths):self.open_attempted=True;return dict(groups=[],warnings=0,flow_units=0)
 def start(self,value):self.start_attempted=True;raise n.NativeCallFailure('start',101,'startup cause')
 def cleanup(self,**kw):
  if sys.argv[2]=='crash':os._exit(29)
  time.sleep(60)
n.NativeSolver=Fake
from easysewer.runtime._solver_worker import main
main()
'''


class CheckpointProtocolTests(unittest.TestCase):
    def test_startup_failure_arrives_before_cleanup_crashes_or_hangs(self):
        import easysewer
        package_root=str(Path(easysewer.__file__).resolve().parent.parent)
        for mode in ('crash','hang'):
            with self.subTest(mode=mode),fixture.BackendContractTests().fixture() as (root,backend):
                value=snapshot(root);(root/'model.inp').write_bytes(value.input_bytes)
                command=[sys.executable,'-I','-B','-u','-c',STARTUP_FAILURE_WORKER,package_root,mode]
                with patch('easysewer.runtime._process_session._worker_command',return_value=command):
                    with backend.session(working_directory=root,call_timeout=5) as session:
                        with self.assertRaises(SessionError) as raised:
                            session._open_checkpoint(replace(value,backend=session.info))
                        self.assertEqual((raised.exception.failure.stage,raised.exception.failure.code),('start',101))
                        self.assertEqual(session.state,'FAILED');self.assertIsNotNone(session.returncode)
                        self.assertFalse(list(root.glob('.checkpoint-context-*')))

    def test_only_explicit_uncommitted_rejection_keeps_worker_alive(self):
        source=fixture.FAKE_WORKER.replace("    if mode=='hang:'+stage:",EXTRA+"    if mode=='hang:'+stage:")
        with patch.object(fixture,'FAKE_WORKER',source):
            for mode in ('valid','commit','bool','state','success','extra','primary','wrong-command','committed-crash'):
                with self.subTest(mode=mode),fixture.BackendContractTests().fixture('checkpoint:'+mode) as (root,backend):
                    with backend.session(working_directory=root) as session:
                        fixture.BackendContractTests().open(session);session.start()
                        command='step' if mode=='wrong-command' else 'checkpoint_restore'
                        with self.assertRaises(SessionError) as raised:session._rpc(command,{})
                        error=raised.exception
                        if mode=='valid':
                            self.assertIsInstance(error,CheckpointRequestRejected)
                            self.assertEqual(session.state,'STARTED');self.assertIsNone(session.returncode)
                            self.assertIsNone(session.failure);self.assertTrue(session.step().finished)
                        else:
                            self.assertNotIsInstance(error,CheckpointRequestRejected)
                            self.assertEqual(session.state,'FAILED');self.assertIsNotNone(session.returncode)
                            if mode in ('primary','committed-crash'):
                                self.assertEqual(error.failure.code,101)
                                self.assertEqual(error.checkpoint_committed,mode=='committed-crash')
