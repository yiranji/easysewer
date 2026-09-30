"""Fresh recovery processes observe actual orphaned bundled workers."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

import easysewer
from easysewer import get_native_capabilities
from easysewer.runtime._worker_identity import has_exited


EVIDENCE = []
CHILD = r"""
import json,os,sys
from pathlib import Path
sys.path[:0]=[sys.argv[1],sys.argv[2]]
from easysewer.runtime import Runner
from easysewer.runtime import _process_session
from easysewer.runtime.runner import _SessionUse
from test_options_v2 import network
from test_runner_v2 import config
root=Path(sys.argv[3]);backend=sys.argv[4];mode=sys.argv[5]
# Delay EOF retirement, without changing the real native solver or its RPCs.
# The observer releases this worker even if a recovery assertion fails.
bootstrap='''
import os,sys,time
from pathlib import Path
sys.path.insert(0,PACKAGE)
root=Path(ROOT)
original_exit=os._exit
def delayed_exit(code):
 if code==74:
  (root/'waiting').write_text('EOF')
  deadline=time.monotonic()+45
  while not (root/'release').exists() and time.monotonic()<deadline:time.sleep(.01)
 original_exit(code)
os._exit=delayed_exit
from easysewer.runtime._solver_worker import main
main()
'''.replace('PACKAGE',repr(sys.argv[1])).replace('ROOT',repr(str(root)))
_process_session._worker_command=lambda:[sys.executable,'-I','-B','-u','-c',bootstrap]
enter=_SessionUse.__enter__
def entered(use):
 session=enter(use)
 # Observer-only receipt. Recovery must obtain identity exclusively from its journal.
 temporary=root/'identity.pending'
 temporary.write_text(json.dumps(session.worker_identity))
 temporary.replace(root/'identity.json')
 if mode=='startup-gap':os._exit(63)
 return session
_SessionUse.__enter__=entered
model=network()
if backend=='easysewer:flexible-ponding':model.update_options(flow_routing='DYNWAVE',allow_ponding=True)
def progress(value):
 if value.phase=='running':os._exit(62)
Runner().run(model,config(root/'out',backend=backend,overwrite=True,
    keep_failed_artifacts=mode=='retain'),progress=progress)
raise AssertionError('Expected parent exit')
"""

RECOVER = r'''
import json,sys
sys.path.insert(0,sys.argv[1])
from easysewer.runtime import recover_run
value=recover_run(sys.argv[2])
print(json.dumps(dict(state=value.state,issues=value.issues,workspaces=value.workspaces,
                     cleaned=value.cleaned_workspaces)))
'''


@unittest.skipUnless(get_native_capabilities()['swmm_solver'], 'Native solver unavailable')
class NativeWorkerIdentityTests(unittest.TestCase):
    def wait_for(self, predicate, message):
        deadline=time.monotonic()+15
        while not predicate() and time.monotonic()<deadline:time.sleep(.02)
        self.assertTrue(predicate(),message)

    def recover(self, journal):
        process=subprocess.run([sys.executable,'-I','-B','-c',RECOVER,
            str(Path(easysewer.__file__).resolve().parent.parent),str(journal)],
            capture_output=True,timeout=30)
        self.assertEqual(process.returncode,0,process.stderr)
        return json.loads(process.stdout)

    def exercise(self, backend, mode):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder).resolve();output=root/'out';output.mkdir()
            (output/'model.out').write_bytes(b'previous')
            identity=None
            try:
                parent=subprocess.run([sys.executable,'-I','-B','-c',CHILD,
                    str(Path(easysewer.__file__).resolve().parent.parent),str(Path(__file__).parent),
                    str(root),backend,mode],capture_output=True,timeout=30)
                self.assertEqual(parent.returncode,63 if mode=='startup-gap' else 62,parent.stderr)
                identity=json.loads((root/'identity.json').read_bytes())
                self.wait_for((root/'waiting').exists,'Worker did not reach delayed EOF exit')
                self.assertFalse(has_exited(identity))
                journal,=output.glob('.easysewer-recovery-*.json')
                item,=json.loads(journal.read_bytes())['workspaces'];workspace=Path(item['path'])
                self.assertEqual(item['execution'],'starting' if mode=='startup-gap' else 'active')
                self.assertEqual(item['worker'],None if mode=='startup-gap' else identity)
                self.assertIsNotNone(item['admission'])
                # Remove the observer receipt: fresh recovery has no inherited handle,
                # parent session object or alternate worker metadata to rely on.
                (root/'identity.json').unlink()
                first=self.recover(journal)
                self.assertTrue(workspace.is_dir());self.assertEqual(first['cleaned'],[])
                self.assertFalse(has_exited(identity),'Recovery terminated an active worker')
                if mode=='retain':
                    self.assertEqual(first['state'],'recovered',first['issues'])
                    self.assertFalse(journal.exists())
                else:
                    self.assertEqual(first['state'],'conflicted')
                    issue='worker is still active'
                    self.assertTrue(any(issue in v for v in first['issues']),first['issues'])
                    self.assertTrue(journal.exists())
                (root/'release').write_text('release')
                self.wait_for(lambda:has_exited(identity),'Worker did not retire after release')
                final=first if mode=='retain' else self.recover(journal)
                if mode in ('cleanup','startup-gap'):
                    self.assertEqual(final['state'],'recovered',final['issues'])
                    self.assertEqual(final['cleaned'],[str(workspace)])
                    self.assertFalse(workspace.exists());self.assertFalse(journal.exists())
                else:
                    self.assertTrue(workspace.is_dir())
                self.assertEqual((output/'model.out').read_bytes(),b'previous')
                EVIDENCE.append(dict(backend=backend,mode=mode,parent_exit=parent.returncode,
                    active_recovery=first['state'],exit_recovery=final['state'],
                    worker_exit_confirmed=True,workspace_removed=not workspace.exists(),
                    old_output_preserved=True,fresh_recovery_process=True))
            finally:
                (root/'release').write_text('release')
                if identity is None and (root/'identity.json').exists():
                    identity=json.loads((root/'identity.json').read_bytes())
                if identity is not None:
                    self.wait_for(lambda:has_exited(identity),'Fixture worker remains active')

    def test_fresh_recovery_obeys_active_worker_and_retention_policy(self):
        for backend in ('swmm:standard','easysewer:flexible-ponding'):
            for mode in ('cleanup','retain'):
                with self.subTest(backend=backend,mode=mode):self.exercise(backend,mode)

    def test_startup_identity_gap_recovers_through_durable_execution_admission(self):
        for backend in ('swmm:standard','easysewer:flexible-ponding'):
            with self.subTest(backend=backend):self.exercise(backend,'startup-gap')


if __name__=='__main__':unittest.main()
