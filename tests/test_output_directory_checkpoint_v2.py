from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import hashlib,json,shutil,tempfile,unittest
from unittest.mock import patch
from easysewer.model import Ref
from easysewer.runtime import Runner,CheckpointOutput
from easysewer.runtime.results import ResourceSnapshot
from easysewer.runtime._checkpoint_container import Builder,Limits,load
from easysewer.runtime._checkpoint_directory_outputs import decode,roots
from easysewer.runtime._checkpoint_directory_restore import DirectoryRestore
from easysewer.runtime._checkpoint_lifecycle import CheckpointLifecycle
from easysewer.runtime._directory_tree import inspect_tree
from test_checkpoint_container_v2 import snapshot,native_prefix,rewrite
from test_checkpoint_directory_inputs_v2 import Solver,working

class OutputDirectoryCheckpointTests(unittest.TestCase):
 def fixture(self,root,state='populated'):
  work=root/'work';work.mkdir();target=work/'out'
  def resource(key,path,kind='directory',access='write'):
   return ResourceSnapshot(owner=Ref(collection='test:directory',key=key),field=('file',),role='test:output',format='test:format',kind=kind,access=access,active=True,required=True,original_path=None,relative_path=path,sha256=None,size=None)
  resources=(resource('O','out'),resource('N','out/sub'),resource('F','out/sub/lid','file'))
  value=snapshot(work,resources=resources);outputs=();locations=dict(outputs=[],trace=None)
  if state!='absent':target.mkdir()
  if state=='populated':
   (target/'sub').mkdir();(target/'sub/lid').write_bytes(b'native prefix');(target/'side').write_bytes(b'side');(target/'alias').hardlink_to(target/'side');(target/'empty').mkdir();outputs=(SimpleNamespace(index=0,role=5,text=True,path=target/'sub/lid',size=13),);locations['outputs']=[dict(index=0,role=5,text=True,relative_path='out/sub/lid')]
  return work,target,value,outputs,locations
 def save(self,root,value,outputs,locations):
  with Builder(root,value) as b:return b.finish(native_prefix(value,b.binding),outputs,output_locations=locations)
 def test_capture_complete_output_tree_and_independent_bound_preparation(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp).resolve();work,target,value,outputs,locations=self.fixture(root);saved=self.save(root/'saved',value,outputs,locations);self.assertEqual(saved.data['schema_version'],'1.7');states,bindings=decode(saved.data,value);self.assertEqual(states,{'out':inspect_tree(target)});self.assertEqual(bindings['outputs'],[dict(index=0,relative_path='out/sub/lid')]);(target/'side').write_bytes(b'later');folder=work/'restore';folder.mkdir();plan=DirectoryRestore.prepare(value,saved,folder,limits=Limits(),checkpoint=lambda:None);path=folder/'output-0';plan.bind_streams(bindings,{0:path},None,checkpoint=lambda:None);self.assertTrue(path.samefile(plan.rows[0].prepared/'sub/lid'));self.assertFalse(path.samefile(target/'sub/lid'));self.assertFalse(path.samefile(saved.directory/'blobs'/hashlib.sha256(b'native prefix').hexdigest()));plan.apply();self.assertTrue(path.samefile(target/'sub/lid'));path.write_bytes(b'continued');self.assertEqual((target/'sub/lid').read_bytes(),b'continued');self.assertEqual((target/'side').read_bytes(),b'side');self.assertFalse(plan.cleanup())
 def test_absent_and_empty_trees_restore_explicit_state(self):
  for state in ('absent','empty'):
   with self.subTest(state=state),tempfile.TemporaryDirectory() as tmp:
    root=Path(tmp).resolve();work,target,value,outputs,locations=self.fixture(root,state);saved=self.save(root/'saved',value,outputs,locations);target.mkdir(exist_ok=True);(target/'later').write_bytes(b'later');folder=work/'restore';folder.mkdir();plan=DirectoryRestore.prepare(value,saved,folder,limits=Limits(),checkpoint=lambda:None);plan.bind_streams(dict(outputs=[],trace=None),{},None,checkpoint=lambda:None);plan.apply();self.assertEqual(target.exists(),state=='empty');self.assertFalse(plan.cleanup())
 def test_recommitted_malformed_tree_and_stream_inventory_rejected(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp).resolve();_,_,value,outputs,locations=self.fixture(root);saved=self.save(root/'saved',value,outputs,locations)
   def change(name,data):
    if name=='base':data['base_schema_version']='1.7'
    elif name=='root':data['output_directory_states'][0]['relative_path']='elsewhere'
    elif name=='duplicate-root':data['output_directory_states']*=2
    elif name=='index':data['output_directory_bindings']['outputs'][0]['index']=True
    elif name=='path':data['output_directory_bindings']['outputs'][0]['relative_path']='out/side'
    elif name=='duplicate-stream':data['output_directory_bindings']['outputs']*=2
    elif name=='missing-member':data['output_directory_states'][0]['tree']=None
    elif name=='extra':data['unexpected']=True
   for mode in ('base','root','duplicate-root','index','path','duplicate-stream','missing-member','extra'):
    destination=root/mode;shutil.copytree(saved.directory,destination);rewrite(destination,lambda data:change(mode,data))
    with self.subTest(mode=mode),self.assertRaises((ValueError,TypeError)):load(destination)
 def test_declared_member_kind_and_input_overlap_reject(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp).resolve();_,target,value,outputs,locations=self.fixture(root);bad=replace(value,resources=value.resources+(replace(value.resources[1],owner=Ref(collection='test:directory',key='Bad'),relative_path='out/side'),))
   with self.assertRaisesRegex(ValueError,'member kind'):self.save(root/'bad',bad,outputs,locations)
   for path in ('out','out/child','OUT/child'):
    added=replace(value.resources[2],owner=Ref(collection='test:directory',key='Input'),relative_path=path,access='read',required=False,active=False)
    with self.subTest(path=path),self.assertRaisesRegex(ValueError,'overlaps'):roots(replace(value,resources=value.resources+(added,)))
 def test_capture_requires_stream_binding_and_keeps_work_on_failure(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp).resolve();work,target,value,outputs,locations=self.fixture(root);before=inspect_tree(target)
   with self.assertRaisesRegex(ValueError,'startup stream locations'):
    with Builder(root/'missing',value) as b:b.finish(native_prefix(value,b.binding),outputs)
   self.assertFalse((root/'missing').exists());self.assertEqual(inspect_tree(target),before)
   (work/'different').write_bytes(b'native prefix');wrong=(SimpleNamespace(index=0,role=5,text=True,path=work/'different',size=13),)
   with self.assertRaisesRegex(ValueError,'binding differs'):self.save(root/'alias',value,wrong,locations)
   self.assertFalse((root/'alias').exists());self.assertEqual(inspect_tree(target),before)
 def test_capture_cancel_and_mutation_remove_only_owned_partial(self):
  for mode in ('cancel','mutation'):
   with self.subTest(mode=mode),tempfile.TemporaryDirectory() as tmp:
    root=Path(tmp).resolve();work,target,value,outputs,locations=self.fixture(root);from easysewer.runtime import _checkpoint_container as container
    original=container._Blobs.put_file;error=OSError('cancel copying output tree')
    def put(blobs,path,*args,**kwargs):
     result=original(blobs,path,*args,**kwargs)
     if Path(path)==target/'sub/lid':
      if mode=='cancel':raise error
      (target/'added').write_bytes(b'keep')
     return result
    with patch.object(container._Blobs,'put_file',put),self.assertRaises((ValueError,OSError)) as caught:self.save(root/'bad',value,outputs,locations)
    if mode=='cancel':self.assertIs(caught.exception,error)
    self.assertFalse((root/'bad').exists());self.assertEqual((target/'sub/lid').read_bytes(),b'native prefix');self.save(root/'retry',value,outputs,locations)
 def test_missing_bindings_are_rejected_before_native_commit(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp).resolve();work,target,value,outputs,locations=self.fixture(root);saved=self.save(root/'saved',value,outputs,locations);rewrite(saved.directory,lambda data:data['output_directory_bindings'].update(outputs=[]));changed=load(saved.directory);owner=CheckpointLifecycle(Solver(value),value);owner.state='STARTED';owner.binding=changed.binding;owner.output_locations=locations;calls=[];owner.api=SimpleNamespace(validate=lambda s:None,restore=lambda *a,**k:calls.append('restore'));before=inspect_tree(target)
   with working(work),self.assertRaisesRegex(ValueError,'matching output-directory'):owner.restore(changed)
   self.assertFalse(calls);self.assertFalse(list(work.glob('.checkpoint-restore-*')));self.assertEqual(inspect_tree(target),before)
 def test_runner_bound_output_can_grow_but_cannot_be_replaced(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp).resolve();(root/'tree').mkdir();(root/'private').mkdir();target=root/'tree/output';target.write_bytes(b'prefix');source=root/'private/output';source.hardlink_to(target);item=CheckpointOutput(role='test',path=source,destination=target,directory=root/'tree');stamp=Runner._checkpoint_output_stamp(item,root,lambda:None);source.write_bytes(b'continued bytes');Runner._adopt_checkpoint_outputs((item,),{target:stamp},root,lambda:None);target.unlink();target.write_bytes(b'continued bytes')
   with self.assertRaisesRegex(ValueError,'no longer matches'):Runner._adopt_checkpoint_outputs((item,),{target:stamp},root,lambda:None)
 def test_runner_rejects_unbound_alias_duplicate_stream_and_changed_parent(self):
  for mode in ('unbound','duplicate','parent'):
   with self.subTest(mode=mode),tempfile.TemporaryDirectory() as tmp:
    root=Path(tmp).resolve();(root/'tree').mkdir();(root/'private').mkdir();target=root/'tree/output';target.write_bytes(b'prefix');source=root/'private/output';source.hardlink_to(target);item=CheckpointOutput(role='test',path=source,destination=target,directory=root/'tree');stamp=Runner._checkpoint_output_stamp(item,root,lambda:None);items=(item,)
    if mode=='unbound':items=(replace(item,directory=None),)
    elif mode=='duplicate':items=(item,item)
    else:(root/'tree').rename(root/'retired');(root/'tree').mkdir();target.hardlink_to(source)
    with self.assertRaises(ValueError):Runner._adopt_checkpoint_outputs(items,{target:stamp},root,lambda:None)
 def test_no_output_tree_keeps_old_format(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp).resolve();plain=snapshot(root)
   with Builder(root/'plain',plain) as b:result=b.finish(native_prefix(plain,b.binding),())
   self.assertEqual(result.data['schema_version'],'1.0');self.assertNotIn('output_directory_states',result.data)
 def test_current_reader_rejects_mislabeled_and_incomplete_output_directory_formats(self):
  # These are malformed current manifests, not historical package evidence.
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp).resolve();_,target,value,outputs,locations=self.fixture(root);saved=self.save(root/'saved',value,outputs,locations)
   before=inspect_tree(target)
   self.assertEqual(saved.data['schema_version'],'1.7')
   with patch('ctypes.CDLL',side_effect=AssertionError('Format checks must stay offline')):
    valid=load(saved.directory);self.assertEqual(valid.snapshot,value)
    self.assertEqual(decode(valid.data,value),decode(saved.data,value))
    for mode in ('base-version','unknown-version','missing-base','missing-states','missing-bindings'):
     with self.subTest(mode=mode):
      destination=root/mode;shutil.copytree(saved.directory,destination)
      def change(data):
       if mode=='base-version':data['schema_version']=data['base_schema_version']
       elif mode=='unknown-version':data['schema_version']='99.0'
       else:del data[{'missing-base':'base_schema_version','missing-states':'output_directory_states','missing-bindings':'output_directory_bindings'}[mode]]
      # Recommit the edit to reach format validation after integrity checks.
      rewrite(destination,change)
      with self.assertRaises(ValueError):load(destination)
   self.assertEqual(inspect_tree(target),before)
   self.assertEqual(load(saved.directory).manifest,saved.manifest)


if __name__=='__main__':unittest.main()
