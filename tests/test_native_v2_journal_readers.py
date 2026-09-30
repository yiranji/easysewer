"""Hold a separate public inspector through a real Runner journal publication."""
import json,os,queue,subprocess,sys,threading
from pathlib import Path
import tempfile,unittest
from unittest.mock import patch
import easysewer
from easysewer.runtime import Runner,recovery
from test_options_v2 import network
from test_runner_v2 import config

EVIDENCE=[]
CHILD=r'''
import json,os,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from easysewer.runtime import inspect_run_recovery
journal=Path(sys.argv[2]);st=journal.stat();identity=(st.st_dev,st.st_ino)
held=False
class Held:
 def __init__(self,stream):self.stream=stream
 def __enter__(self):return self
 def __exit__(self,*args):return self.stream.__exit__(*args)
 def __getattr__(self,key):return getattr(self.stream,key)
 def read(self,*args):
  global held
  if not held:
   held=True;print('READ_HELD',flush=True);assert sys.stdin.buffer.read(1)==b'1'
  return self.stream.read(*args)
def wrap(stream):
 st=os.fstat(stream.fileno())
 return Held(stream) if (st.st_dev,st.st_ino)==identity else stream
opening=Path.open;fdopen=os.fdopen
def opened(path,*args,**kwargs):return wrap(opening(path,*args,**kwargs))
def opened_fd(*args,**kwargs):return wrap(fdopen(*args,**kwargs))
Path.open=opened;os.fdopen=opened_fd
result=inspect_run_recovery(journal)
print(json.dumps(dict(state=result.state,phase=result.phase)),flush=True)
'''


@unittest.skipUnless(easysewer.get_native_capabilities()['swmm_solver'], 'Native solver unavailable')
class NativeJournalReaders(unittest.TestCase):
    def test_real_runner_updates_with_separate_inspector_and_same_complete_output(self):
        for backend in ('swmm:standard','easysewer:flexible-ponding'):
            with self.subTest(backend=backend),tempfile.TemporaryDirectory() as directory:
                root=Path(directory).resolve();model=network()
                if backend=='easysewer:flexible-ponding':model.update_options(flow_routing='DYNWAVE',allow_ponding=True)
                baseline=Runner().run(model,config(root,backend=backend));self.assertTrue(baseline.succeeded,baseline.failure)
                expected=baseline.output.read_bytes();child=None;started=False;publications=[]
                sync=recovery._Journal.sync
                def progress(value):
                    nonlocal child,started
                    journals=list(root.glob('.easysewer-recovery-*.json'))
                    if started or not journals:return
                    self.assertEqual(len(journals),1);started=True
                    child=subprocess.Popen([sys.executable,'-I','-B','-c',CHILD,
                        str(Path(easysewer.__file__).resolve().parent.parent),str(journals[0])],
                        stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
                    q=queue.Queue();threading.Thread(target=lambda:q.put(child.stdout.readline()),daemon=True).start()
                    self.assertEqual(q.get(timeout=30).strip(),b'READ_HELD')
                def syncing(journal,transaction,**options):
                    result=sync(journal,transaction,**options)
                    if child is not None and child.poll() is None:
                        child.stdin.write(b'1');child.stdin.flush()
                        stdout,stderr=child.communicate(timeout=30)
                        self.assertEqual(child.returncode,0,stderr)
                        observed=json.loads(stdout);self.assertEqual(observed['state'],'active')
                        publications.append(dict(phase=journal.data['phase'],inspection=observed))
                    return result
                try:
                    with patch.object(recovery._Journal,'sync',syncing):
                        result=Runner().run(model,config(root,backend=backend,overwrite=True),progress=progress)
                    self.assertTrue(result.succeeded,result.failure);self.assertTrue(publications)
                    self.assertEqual(result.output.read_bytes(),expected)
                    self.assertFalse(list(root.glob('.easysewer-*')))
                    EVIDENCE.append(dict(backend=backend,publications=publications,complete_output_equal=True))
                finally:
                    if child is not None and child.poll() is None:child.kill();child.communicate()
