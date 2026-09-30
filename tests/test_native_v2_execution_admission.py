"""Real Runner crashes before worker identity exists, including delayed starts."""
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


EVIDENCE=[]
PARENT=r"""
import os,sys
from pathlib import Path
sys.path[:0]=[sys.argv[1],sys.argv[2]]
from easysewer.runtime import Runner,_process_session
from test_options_v2 import network
from test_runner_v2 import config
root=Path(sys.argv[3]);backend=sys.argv[4];mode=sys.argv[5]
bootstrap=r'''
import json,os,sys,time
from pathlib import Path
sys.path.insert(0,PACKAGE)
root=Path(ROOT);mode=MODE
from easysewer.runtime import _admission as a
from easysewer.runtime._worker_identity import capture
from easysewer.runtime._native_solver import NativeSolver
original_exit=os._exit
def wait(name):
 deadline=time.monotonic()+45
 while not (root/name).exists():
  if time.monotonic()>deadline:original_exit(78)
  time.sleep(.01)
def pause():
 (root/'identity.json').write_text(json.dumps(capture()))
 (root/'ready').write_text(mode)
 wait('release')
def delayed_exit(code):
 if code==74:wait('finish')
 original_exit(code)
os._exit=delayed_exit
admitted=False;real_enter=a.enter
def entering(record):
 global admitted
 if mode=='before-admission':pause()
 try:lease=real_enter(record)
 except BaseException:
  (root/'admission-denied').write_text('denied');raise
 admitted=True
 return lease
a.enter=entering
if mode=='opened':
 real_open=Path.open
 def opened(path,*args,**kw):
  stream=real_open(path,*args,**kw)
  if path.name.startswith('.easysewer-admission-') and args==('r+b',):pause()
  return stream
 Path.open=opened
initialize=NativeSolver.__init__
def initializing(self,*args,**kw):
 if admitted and mode in ('native-load','load-failure'):
  pause()
  if mode=='load-failure':
   (root/'load-failure').write_text('injected');raise OSError('injected library initialization failure')
 result=initialize(self,*args,**kw)
 if admitted:(root/'native-loaded').write_text('loaded')
 return result
NativeSolver.__init__=initializing
from easysewer.runtime._solver_worker import main
main()
'''.replace('PACKAGE',repr(sys.argv[1])).replace('ROOT',repr(str(root))).replace('MODE',repr(mode))
if mode=='pre-spawn':
 connection=_process_session._WorkerConnection
 def before_spawn(directory):
  if Path(directory).is_relative_to(root/'out'):os._exit(64)
  return connection(directory)
 _process_session._WorkerConnection=before_spawn
else:
 _process_session._worker_command=lambda:[sys.executable,'-I','-B','-u','-c',bootstrap]
model=network()
if backend=='easysewer:flexible-ponding':model.update_options(flow_routing='DYNWAVE',allow_ponding=True)
result=Runner().run(model,config(root/'out',backend=backend,overwrite=True,keep_failed_artifacts=False))
raise AssertionError(result.failure)
"""

RECOVER=r'''
import json,sys
sys.path.insert(0,sys.argv[1])
from easysewer.runtime import recover_run
v=recover_run(sys.argv[2])
print(json.dumps(dict(state=v.state,issues=v.issues,workspaces=v.workspaces,cleaned=v.cleaned_workspaces)))
'''


@unittest.skipUnless(get_native_capabilities()['swmm_solver'],'Native solver unavailable')
class NativeExecutionAdmissionTests(unittest.TestCase):
    def wait_for(self, predicate, message):
        deadline=time.monotonic()+20
        while not predicate() and time.monotonic()<deadline:time.sleep(.02)
        self.assertTrue(predicate(),message)

    def recover(self, journal):
        child=subprocess.run([sys.executable,'-I','-B','-c',RECOVER,
            str(Path(easysewer.__file__).resolve().parent.parent),str(journal)],capture_output=True,timeout=30)
        self.assertEqual(child.returncode,0,child.stderr)
        return json.loads(child.stdout)

    def exercise(self, backend, mode):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder).resolve();out=root/'out';out.mkdir();(out/'model.out').write_bytes(b'previous')
            parent=subprocess.Popen([sys.executable,'-I','-B','-c',PARENT,
                str(Path(easysewer.__file__).resolve().parent.parent),str(Path(__file__).parent),
                str(root),backend,mode],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
            identity=None
            try:
                if mode=='pre-spawn':
                    _,error=parent.communicate(timeout=20);self.assertEqual(parent.returncode,64,error)
                else:
                    self.wait_for(lambda:(root/'ready').exists() or parent.poll() is not None,'Worker did not reach startup boundary')
                    self.assertIsNone(parent.poll(),'Parent exited before startup boundary')
                    identity=json.loads((root/'identity.json').read_bytes());self.assertFalse(has_exited(identity))
                    parent.kill();parent.communicate(timeout=15)
                journal,=out.glob('.easysewer-recovery-*.json')
                item,=json.loads(journal.read_bytes())['workspaces']
                self.assertEqual(item['execution'],'starting');self.assertIsNone(item['worker'])
                self.assertIsNotNone(item['admission']);self.assertFalse(item['admission']['revoked'])
                workspace=Path(item['path'])
                first=self.recover(journal)
                if mode in ('native-load','load-failure'):
                    self.assertEqual(first['state'],'conflicted',first['issues'])
                    self.assertTrue(any('worker is still active' in v for v in first['issues']))
                    retained,=json.loads(journal.read_bytes())['workspaces']
                    self.assertFalse(retained['admission']['revoked']);self.assertTrue(workspace.exists())
                else:
                    self.assertIn(first['state'],('recovered','conflicted'),first['issues'])
                    if journal.exists():
                        retained,=json.loads(journal.read_bytes())['workspaces']
                        self.assertTrue(retained['admission']['revoked'])
                if identity is not None:
                    (root/'release').write_text('release')
                    marker='admission-denied' if mode in ('before-admission','opened') else ('load-failure' if mode=='load-failure' else 'native-loaded')
                    self.wait_for((root/marker).exists,'Worker did not complete guarded startup attempt')
                    if mode in ('before-admission','opened'):self.assertFalse((root/'native-loaded').exists())
                    (root/'finish').write_text('finish')
                    self.wait_for(lambda:has_exited(identity),'Worker did not exit after parent termination')
                final=self.recover(journal) if journal.exists() else first
                self.assertEqual(final['state'],'recovered',final['issues'])
                self.assertFalse(workspace.exists());self.assertEqual((out/'model.out').read_bytes(),b'previous')
                self.assertFalse(list(out.glob('.easysewer-*')))
                EVIDENCE.append(dict(backend=backend,mode=mode,parent_exit=parent.returncode,
                    worker_identity_absent_from_journal=True,first_recovery=first['state'],
                    recovered=True,workspace_removed=True,old_output_preserved=True,
                    late_start_denied=mode in ('before-admission','opened')))
            finally:
                (root/'release').write_text('release');(root/'finish').write_text('finish')
                if parent.poll() is None:parent.kill();parent.communicate(timeout=15)
                if identity is None and (root/'identity.json').exists():identity=json.loads((root/'identity.json').read_bytes())
                if identity is not None:self.wait_for(lambda:has_exited(identity),'Fixture worker remains active')
                parent.stdout.close();parent.stderr.close()

    def test_parent_exit_before_spawn_has_durable_recoverable_admission(self):
        for backend in ('swmm:standard','easysewer:flexible-ponding'):
            with self.subTest(backend=backend):self.exercise(backend,'pre-spawn')

    def test_delayed_start_and_incomplete_native_load_after_parent_termination(self):
        for backend in ('swmm:standard','easysewer:flexible-ponding'):
            for mode in ('before-admission','opened','native-load','load-failure'):
                with self.subTest(backend=backend,mode=mode):self.exercise(backend,mode)


if __name__=='__main__':unittest.main()
