from pathlib import Path
from types import SimpleNamespace
import tempfile,unittest
from unittest.mock import patch
from easysewer.runtime._checkpoint_directory_restore import DirectoryRestore
from easysewer.runtime._checkpoint_lifecycle import CheckpointLifecycle,RestoreRejected
from easysewer.runtime._checkpoint_container import Builder,Limits,_decode_directory_states
from easysewer.runtime._directory_tree import inspect_tree
from easysewer.runtime._checkpoint_native import RestoreResult
import test_optional_mutable_directory_v2 as optional
import test_mixed_resource_graph_v2 as mixed
from test_checkpoint_container_v2 import native_prefix
from test_checkpoint_directory_inputs_v2 import Solver,working

class WritableRestoreTests(unittest.TestCase):
 def fixture(self,root,state='populated'):
  source,work,value,_=optional.OptionalMutableDirectoryTests().setup(root,False);current=work/value.resources[0].relative_path
  if state!='absent':current.mkdir()
  if state=='populated':(current/'data').write_bytes(b'saved');(current/'alias').hardlink_to(current/'data');(current/'empty').mkdir()
  with Builder(root/'saved',value) as b:saved=b.finish(native_prefix(value,b.binding),())
  return source,work,value,current,saved
 def owner(self,value,saved,*,committed=True,action=None):
  owner=CheckpointLifecycle(Solver(value),value);owner.state='STARTED';owner.binding=saved.binding;calls=[]
  def restore(*args,**kwargs):
   calls.append('restore')
   if action:action()
   return RestoreResult(0 if committed else 1,committed,0,())
  owner.api=SimpleNamespace(validate=lambda s:None,restore=restore);return owner,calls
 def test_absent_empty_populated_forests_restore_and_keep_source_untouched(self):
  for state in ('absent','empty','populated'):
   with self.subTest(state=state),tempfile.TemporaryDirectory() as tmp:
    root=Path(tmp).resolve();source,work,value,current,saved=self.fixture(root,state);current.mkdir(exist_ok=True);(current/'later').write_bytes(b'after');owner,calls=self.owner(value,saved)
    with working(work):outcome=owner.restore(saved)
    self.assertTrue(outcome.native.committed);self.assertFalse(outcome.cleanup);self.assertEqual(owner.state,'STARTED');self.assertEqual(current.exists(),state!='absent');self.assertFalse(source.exists())
    if state!='absent':self.assertEqual(inspect_tree(current),next(iter(_decode_directory_states(saved.data,value,None).values())))
    if state=='populated':self.assertTrue((current/'data').samefile(current/'alias'))
    self.assertFalse(list(work.rglob('retired-*')))
 def test_precommit_native_rejection_preserves_current_and_drops_private_stage(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp).resolve();_,work,value,current,saved=self.fixture(root);(current/'later').write_bytes(b'keep');before=inspect_tree(current);owner,calls=self.owner(value,saved,committed=False)
   with working(work),self.assertRaises(RestoreRejected) as caught:owner.restore(saved)
   self.assertFalse(caught.exception.checkpoint_committed);self.assertEqual(owner.state,'STARTED');self.assertEqual(inspect_tree(current),before);self.assertFalse(list(work.glob('.checkpoint-restore-*')))
 def test_cancel_during_preparation_preserves_current_and_exception(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp).resolve();_,work,value,current,saved=self.fixture(root);(current/'later').write_bytes(b'keep');before=inspect_tree(current);owner,calls=self.owner(value,saved);error=OSError('cancel tree preparation');original=DirectoryRestore.prepare
   def prepare(*args,**kwargs):
    def stop():raise error
    kwargs['checkpoint']=stop;return original(*args,**kwargs)
   with working(work),patch.object(DirectoryRestore,'prepare',side_effect=prepare),self.assertRaises(OSError) as caught:owner.restore(saved)
   self.assertIs(caught.exception,error);self.assertFalse(calls);self.assertFalse(error.checkpoint_committed);self.assertEqual(inspect_tree(current),before);self.assertFalse(list(work.glob('.checkpoint-restore-*')))
 def test_intervening_current_or_staged_mutation_rejects_before_apply(self):
  for which in ('current','staged','ancestor'):
   with self.subTest(which=which),tempfile.TemporaryDirectory() as tmp:
    root=Path(tmp).resolve();_,work,value,current,saved=self.fixture(root);folder=work/'restore';folder.mkdir();transaction=DirectoryRestore.prepare(value,saved,folder,limits=Limits(),checkpoint=lambda:None)
    if which=='ancestor':current.parent.rename(current.parent.with_name('old-parent'));current.parent.mkdir()
    else:(current if which=='current' else transaction.rows[0].prepared).joinpath('unbound').write_bytes(b'keep')
    with self.assertRaises((ValueError,OSError)):transaction.check()
    self.assertFalse(transaction.applied)
 def test_postcommit_publication_failure_marks_fatal_and_retains_both_trees(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp).resolve();_,work,value,current,saved=self.fixture(root);(current/'later').write_bytes(b'keep');owner,calls=self.owner(value,saved);original=Path.rename
   def rename(path,dest):
    if path.name=='directory-0':raise OSError('publish failed')
    return original(path,dest)
   with working(work),patch.object(Path,'rename',rename),self.assertRaises(OSError) as caught:owner.restore(saved)
   self.assertTrue(caught.exception.checkpoint_committed);self.assertEqual(owner.state,'FAILED');folder=owner.owned[-1][0];self.assertEqual((folder/'retired-0/later').read_bytes(),b'keep');self.assertEqual((folder/'directory-0/data').read_bytes(),b'saved')
 def test_native_commit_time_mutation_is_preserved_and_fatal(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp).resolve();_,work,value,current,saved=self.fixture(root);owner,calls=self.owner(value,saved,action=lambda:(current/'late').write_bytes(b'external'))
   with working(work),self.assertRaises(ValueError) as caught:owner.restore(saved)
   self.assertTrue(caught.exception.checkpoint_committed);self.assertEqual(owner.state,'FAILED');self.assertEqual((current/'late').read_bytes(),b'external')
 def test_retirement_cleanup_failure_is_reported_after_success(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp).resolve();_,work,value,current,saved=self.fixture(root);(current/'later').write_bytes(b'keep');owner,calls=self.owner(value,saved)
   with working(work),patch('easysewer.runtime._checkpoint_directory_restore.remove_owned_tree',side_effect=PermissionError('retain old tree')):outcome=owner.restore(saved)
   self.assertTrue(outcome.native.committed);self.assertEqual(owner.state,'STARTED');self.assertEqual(len(outcome.cleanup),1);self.assertEqual(outcome.cleanup[0].stage,'checkpoint_directory_cleanup');self.assertFalse((current/'later').exists());self.assertTrue((owner.owned[-1][0]/'retired-0/later').exists())
 def test_shared_file_directory_alias_forest_restores_topology(self):
  for kind in ('nested','alias','absent-parent','independent'):
   with self.subTest(kind=kind),tempfile.TemporaryDirectory() as tmp:
    root=Path(tmp).resolve();helper=mixed.MixedResourceGraphTests();source,external,work,value,group=helper.setup(root,kind);paths=helper.paths(work,value)
    if kind=='absent-parent':paths['F'].parent.mkdir(parents=True);paths['F'].write_bytes(b'present at save')
    saved=helper.helper().save(root/'saved',value);expected=inspect_tree(work/group.relative_path)
    if paths['F'].exists():paths['F'].unlink()
    paths['F'].write_bytes(b'after');(paths['W']/'extra').mkdir();owner,calls=self.owner(value,saved)
    with working(work):outcome=owner.restore(saved)
    self.assertTrue(outcome.native.committed);self.assertEqual(inspect_tree(work/group.relative_path),expected);self.assertEqual((source/'child/data').read_bytes(),b'initial')
    if kind in ('nested','alias'):self.assertTrue(paths['F'].samefile(paths['W']/'child/data'))
 def test_file_only_restore_keeps_previous_path_without_directory_plan(self):
  from test_checkpoint_container_v2 import snapshot
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp).resolve();value=snapshot(root)
   with Builder(root/'saved',value) as b:saved=b.finish(native_prefix(value,b.binding),())
   folder=root/'restore';folder.mkdir();self.assertIsNone(DirectoryRestore.prepare(value,saved,folder,limits=Limits(),checkpoint=lambda:None));self.assertFalse(list(folder.iterdir()))

if __name__=='__main__':unittest.main()