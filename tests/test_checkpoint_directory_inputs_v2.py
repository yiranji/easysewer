from pathlib import Path
from contextlib import contextmanager
from types import SimpleNamespace
import os,shutil,tempfile,unittest
from unittest.mock import patch
from easysewer.runtime._checkpoint_directory_inputs import DirectoryInputs
from easysewer.runtime._checkpoint_lifecycle import CheckpointLifecycle
from easysewer.runtime._checkpoint_container import _snapshot_trees
from directory_roundtrip_fixture import prepared,row,schema
import test_mixed_resource_graph_v2 as mixed_fixture
from test_checkpoint_container_v2 import snapshot

@contextmanager
def working(root):
 old=Path.cwd();os.chdir(root)
 try:yield
 finally:os.chdir(old)

class Solver:
 def __init__(self,value):
  self.metadata={n:getattr(value.backend,n) for n in ('sha256','engine_version','platform','architecture','abi')};self.open_attempted=False;self.start_attempted=False;self.closed=False;self.calls=[];self.action=None
 def open(self,args):self.calls.append('open');return {}
 def start(self,save):self.calls.append('start')
 def step(self,count):
  self.calls.append('step')
  if self.action:self.action()
  return {'finished':False}
 def end(self):self.calls.append('end');return {}
 def report(self):self.calls.append('report')
 def cleanup(self,*args,**kwargs):self.closed=True;return ()

class API:
 def __init__(self,*args,**kwargs):pass
 def inputs(self):return ()

class DirectoryInputLedgerTests(unittest.TestCase):
 def fixture(self,root,absent=False):
  path=root/'source'
  if not absent:path.mkdir();(path/'data').write_bytes(b'initial');(path/'alias').hardlink_to(path/'data');(path/'empty').mkdir()
  value,_=prepared(root,(row('D','source',required=not absent),));(root/'model.inp').write_bytes(value.input_bytes);return path,value
 def test_readonly_changes_are_rejected_without_rehashing_at_boundaries(self):
  for change in ('added','empty-added','empty-removed','file-removed','content','alias-broken','equal-replaced','rename'):
   with self.subTest(change=change),tempfile.TemporaryDirectory() as tmp:
    root=Path(tmp).resolve();path,value=self.fixture(root);ledger=DirectoryInputs.read(value)
    with patch('easysewer.runtime._directory_tree._file_digest',side_effect=AssertionError('boundary must not reread content')):ledger.check()
    if change=='added':(path/'new').write_bytes(b'new')
    elif change=='empty-added':(path/'other').mkdir()
    elif change=='empty-removed':(path/'empty').rmdir()
    elif change=='file-removed':(path/'alias').unlink()
    elif change=='content':(path/'data').write_bytes(b'changed')
    elif change in ('alias-broken','equal-replaced'):target=path/('alias' if change=='alias-broken' else 'data');raw=target.read_bytes();target.unlink();target.write_bytes(raw)
    else:(path/'empty').rename(path/'renamed')
    with self.assertRaises((ValueError,OSError)):ledger.check()
 def test_optional_absence_and_ancestor_identity_are_bound(self):
  for change in ('created','ancestor-replaced','file-ancestor'):
   with self.subTest(change=change),tempfile.TemporaryDirectory() as tmp:
    root=Path(tmp).resolve();parent=root/'parent';parent.mkdir();value,_=prepared(root,(row('D','parent/missing',required=False),));ledger=DirectoryInputs.read(value);ledger.check()
    if change=='created':(parent/'missing').mkdir()
    else:
     parent.rename(root/'original-parent')
     if change=='ancestor-replaced':parent.mkdir()
     else:parent.write_bytes(b'not a directory')
    with self.assertRaises((ValueError,OSError)):ledger.check()
 def test_sibling_outputs_do_not_invalidate_missing_directory_parent(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp).resolve();value,_=prepared(root,(row('D','parent/missing',required=False),));ledger=DirectoryInputs.read(value);(root/'unrelated').write_bytes(b'output');(root/'parent').mkdir();ledger.check()
 def test_mutable_current_forests_can_change_but_initial_evidence_cannot(self):
  for kind in ('nested','alias','absent'):
   with self.subTest(kind=kind),tempfile.TemporaryDirectory() as tmp:
    root=Path(tmp).resolve();fixture=mixed_fixture.MixedResourceGraphTests();_,_,work,value,group=fixture.setup(root,kind);ledger=DirectoryInputs.read(value);paths=fixture.paths(work,value);paths['F'].parent.mkdir(parents=True,exist_ok=True);paths['F'].write_bytes(b'current');(paths['W']/'new').mkdir();ledger.check();initial=work/group.initial_relative_path;(initial/'extra').write_bytes(b'bad')
    with self.assertRaises((ValueError,OSError)):ledger.check()
 def test_admission_rejects_changed_bytes_and_respects_original_checkpoint_exception(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp).resolve();path,value=self.fixture(root);error=OSError('cancelled during directory admission')
   def stop():raise error
   with self.assertRaises(OSError) as found:DirectoryInputs.read(value,checkpoint=stop)
   self.assertIs(found.exception,error);(path/'data').write_bytes(b'changed')
   with self.assertRaises(ValueError):DirectoryInputs.read(value)
 def test_file_only_execution_has_no_directory_ledger(self):
  with tempfile.TemporaryDirectory() as tmp:self.assertIsNone(DirectoryInputs.read(snapshot(Path(tmp))))
 def test_lifecycle_checks_before_and_after_step_and_before_end(self):
  for phase in ('before-step','after-step','before-end'):
   with self.subTest(phase=phase),tempfile.TemporaryDirectory() as tmp:
    root=Path(tmp).resolve();path,value=self.fixture(root);solver=Solver(value)
    with working(root),patch('easysewer.runtime._checkpoint_lifecycle.NativeCheckpoint',API):
     lifecycle=CheckpointLifecycle(solver,value,schema=schema());lifecycle.open_start();self.assertEqual(set(lifecycle.originals),{'model.inp'});self.assertIsNotNone(lifecycle.directory_inputs)
     if phase=='after-step':solver.action=lambda:(path/'extra').mkdir()
     else:(path/'extra').mkdir()
     with self.assertRaises((ValueError,OSError)):(lifecycle.end if phase=='before-end' else lifecycle.step)()
     self.assertEqual(lifecycle.state,'FAILED');self.assertEqual(solver.calls.count('step'),int(phase=='after-step'));self.assertNotIn('end',solver.calls)
 def test_missing_schema_rejects_before_native_open_and_remains_explicit_gap(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp).resolve();_,value=self.fixture(root);solver=Solver(value)
   with working(root):
    with self.assertRaises(Exception):CheckpointLifecycle(solver,value).open_start()
   self.assertFalse(solver.calls)

if __name__=='__main__':unittest.main()
