"""Initial absence, later creation/deletion, and lossless nullable directory state."""
from pathlib import Path
from dataclasses import replace
import hashlib,json,shutil,tempfile,unittest
from unittest.mock import patch
from easysewer.runtime import _preparation as prep
from easysewer.runtime import _checkpoint_container as storage
from easysewer.runtime._directory_state import verify_absent,observe
from easysewer.runtime._directory_tree import inspect_tree,DirectoryManifest
from easysewer.runtime.directory_resources import DirectoryAdapter
from easysewer.runtime.results import DirectoryArtifact,RunResult
from easysewer.runtime._result_codec import Codec
from easysewer.runtime._checkpoint_context import write,read
from easysewer.io.interface_inspection import InterfaceInspection
from directory_roundtrip_fixture import prepared,row,schema
from test_checkpoint_container_v2 import snapshot,native_prefix,rewrite
import test_mutable_directory_v2 as mutable_fixture
from test_directory_checkpoint_v2 import record

class OptionalMutableDirectoryTests(unittest.TestCase):
 def setup(self,root,existing=False):
  source=root/'source'
  if existing:source.mkdir();(source/'data').write_bytes(b'initial')
  _,model=prepared(root,(row('R','source',required=False),row('W','source',access='read_write',required=False)))
  work=root/'work';work.mkdir();registry={'test:directory.format':DirectoryAdapter(inspector=lambda p,**k:InterfaceInspection(format=k['use'].format,status='validated'))}
  plans=prep.inventory(model,input_directory=root,working_directory=root,directory_adapters=registry)
  resources,outputs,issues=prep.stage(model,plans,work,'assets',checkpoint=lambda:None)
  self.assertFalse(outputs)
  return source,work,snapshot(work,model=model,resources=resources),issues
 def save(self,path,value):
  with storage.Builder(path,value) as b:return b.finish(native_prefix(value,b.binding),())
 def artifacts(self,work,value,complete=False):
  return tuple(DirectoryArtifact.from_path(work/path,role=role,owner=r.owner,field=r.field,complete=complete,allow_absent=True)
   for r in value.resources for role,path in (('run:resource',r.initial_relative_path),('run:resource_state',r.relative_path)))
 def failed(self,work,value):
  t=mutable_fixture.MutableDirectoryTests();t.artifacts=self.artifacts;return t.failed(work,value)
 def test_missing_shared_optional_source_has_explicit_absence_without_creation(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);source,work,value,issues=self.setup(root);a,b=value.resources
   self.assertIsNone(a.tree);self.assertIsNone(b.tree);self.assertIsNotNone(a.initial_relative_path);self.assertEqual(a.initial_relative_path,b.initial_relative_path);self.assertEqual(a.relative_path,b.relative_path)
   self.assertEqual([v.code for v in issues],['run.optional_resource_missing']*2)
   for path in (source,work/a.initial_relative_path,work/a.relative_path):self.assertFalse(path.exists())
   prep.verify_resources(value.resources,work,checkpoint=lambda:None,initial_execution=True)
   for bad in (replace(b,initial_relative_path=None),):
    with self.assertRaises(ValueError):replace(value,resources=(a,bad))
   with self.assertRaises(ValueError):replace(a,required=True)
   with self.assertRaises(ValueError):replace(a,active=False)
 def test_absent_empty_populated_states_restore_distinctly_and_keep_execution_identity(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);source,work,value,_=self.setup(root);r=value.resources[0];current=work/r.relative_path;seen=[]
   for state in ('absent','empty','populated'):
    if state=='empty':current.mkdir()
    elif state=='populated':(current/'created').write_bytes(b'created in run');(current/'nested-empty').mkdir()
    saved=self.save(root/('saved-'+state),value);seen.append(saved)
    self.assertEqual(saved.data['schema_version'],'1.3');restored=saved.materialize(root/('restored-'+state),schema=schema());target=Path(restored.execution_directory)
    self.assertFalse((target/r.initial_relative_path).exists());self.assertEqual((target/r.relative_path).exists(),state!='absent')
    if state!='absent':self.assertEqual(inspect_tree(target/r.relative_path),inspect_tree(current))
    self.assertEqual((target/'model.inp').read_bytes(),value.input_bytes)
   self.assertEqual(len({s.binding for s in seen}),1);self.assertEqual(len({s.manifest for s in seen}),3);self.assertFalse(source.exists())
 def test_originally_existing_directory_can_be_removed_and_restored_absent(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);source,work,value,_=self.setup(root,True);r=value.resources[0];current=work/r.relative_path;(current/'data').unlink();current.rmdir()
   prep.verify_resources(value.resources,work,checkpoint=lambda:None)
   saved=self.save(root/'saved',value);self.assertEqual(saved.data['schema_version'],'1.3');self.assertIsNone(saved.data['directory_states'][0]['tree'])
   restored=saved.materialize(root/'restored',schema=schema());target=Path(restored.execution_directory)
   self.assertFalse((target/r.relative_path).exists());self.assertEqual(inspect_tree(target/r.initial_relative_path),r.tree);self.assertEqual(inspect_tree(source),r.tree)
 def test_absence_artifact_is_not_empty_directory_and_rejects_later_presence(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);_,work,value,_=self.setup(root);r=value.resources[0];path=work/r.relative_path
   with self.assertRaises(FileNotFoundError):DirectoryArtifact.from_path(path,role='run:resource_state',owner=r.owner,field=r.field,complete=False)
   artifact=self.artifacts(work,value)[1];self.assertIsNone(artifact.manifest);self.assertEqual(artifact.files,());artifact.verify()
   rebuilt=artifact.materialize(root/'absent');self.assertIsNone(rebuilt.manifest);self.assertFalse((root/'absent').exists())
   path.mkdir()
   with self.assertRaises(ValueError):artifact.verify()
   empty=DirectoryArtifact.from_path(path,role=artifact.role,owner=r.owner,field=r.field,complete=False);self.assertEqual(empty.manifest,DirectoryManifest(entries=()));self.assertNotEqual(empty.manifest,artifact.manifest)
   with self.assertRaises(ValueError):artifact.materialize(path)
   self.assertTrue(path.is_dir())
 def test_result_archive_and_success_contract_preserve_both_absence_transitions(self):
  for initial in (False,True):
   with self.subTest(initial=initial),tempfile.TemporaryDirectory() as tmp:
    root=Path(tmp);source,work,value,_=self.setup(root,initial);r=value.resources[0];current=work/r.relative_path
    if initial:(current/'data').unlink();current.rmdir()
    else:current.mkdir();(current/'new').write_bytes(b'new')
    t=mutable_fixture.MutableDirectoryTests();t.artifacts=self.artifacts;success=t.success(work,value);success.save(root/'saved');loaded=RunResult.load(root/'saved')
    self.assertTrue(loaded.succeeded);self.assertEqual(json.loads((root/'saved/result.json').read_bytes())['schema_version'],'1.6')
    for resource in value.resources:
     a=loaded.directory_artifact('run:resource',owner=resource.owner,field=resource.field);b=loaded.directory_artifact('run:resource_state',owner=resource.owner,field=resource.field)
     self.assertEqual(a.manifest is None,not initial);self.assertEqual(b.manifest is None,initial)
    loaded.save(root/'again');self.assertEqual(RunResult.load(root/'again').snapshot,value)
    with self.assertRaisesRegex(ValueError,'resource snapshot'):replace(success,directory_artifacts=tuple(a for a in success.directory_artifacts if a.role!='run:resource'))
 def test_failed_absent_archive_materializes_no_directory_and_can_be_resaved(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);_,work,value,_=self.setup(root);self.failed(work,value).save(root/'saved');loaded=RunResult.load(root/'saved')
   for i,a in enumerate(loaded.directory_artifacts):
    self.assertIsNone(a.manifest);self.assertIsNone(a.path);self.assertFalse(a.complete);a.materialize(root/str(i));self.assertFalse((root/str(i)).exists())
   loaded.save(root/'again');again=RunResult.load(root/'again');self.assertEqual(again,loaded)
   self.assertEqual([a.original_path for a in again.directory_artifacts],[a.original_path for a in loaded.directory_artifacts])
   again.save(root/'twice');self.assertEqual((root/'again/result.json').read_bytes(),(root/'twice/result.json').read_bytes())
 def test_initial_absence_tampering_blocks_capture_and_preexecution_presence_is_checked(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);source,work,value,_=self.setup(root);r=value.resources[0];current=work/r.relative_path;initial=work/r.initial_relative_path;current.mkdir()
   prep.verify_resources(value.resources,work,checkpoint=lambda:None)
   with self.assertRaises(ValueError):prep.verify_resources(value.resources,work,checkpoint=lambda:None,initial_execution=True)
   initial.mkdir()
   with self.assertRaises(ValueError):prep.verify_resources(value.resources,work,checkpoint=lambda:None)
   with self.assertRaises(ValueError):self.save(root/'bad',value)
   self.assertFalse((root/'bad').exists());self.assertTrue(initial.is_dir());self.assertTrue(current.is_dir());self.assertFalse(source.exists())
 def test_permission_wrong_parent_and_cancellation_do_not_become_absence(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);target=root/'missing';error=PermissionError('inaccessible');error.__cause__=ValueError('cause');original=Path.lstat
   def lstat(path,*a,**k):
    if path==target:raise error
    return original(path,*a,**k)
   with patch.object(Path,'lstat',lstat),self.assertRaises(PermissionError) as caught:observe(target,allow_absent=True)
   self.assertIs(caught.exception,error);self.assertIsInstance(caught.exception.__cause__,ValueError)
   (root/'file').write_bytes(b'keep')
   with self.assertRaises((ValueError,NotADirectoryError)):verify_absent(root/'file/child')
   cause=OSError('cancel absence observation')
   def stop():raise cause
   with self.assertRaises(OSError) as caught:observe(target,allow_absent=True,checkpoint=stop)
   self.assertIs(caught.exception,cause);self.assertEqual((root/'file').read_bytes(),b'keep')
   with self.assertRaises(TypeError):observe(target,allow_absent=1)
 def test_shared_source_presence_change_between_consumers_is_not_mixed(self):
  for existed in (False,True):
   with self.subTest(existed=existed),tempfile.TemporaryDirectory() as tmp:
    root=Path(tmp);source=root/'source'
    if existed:source.mkdir();(source/'data').write_bytes(b'old')
    _,model=prepared(root,(row('R','source',required=False),row('W','source',access='read_write',required=False)))
    work=root/'work';work.mkdir();registry={'test:directory.format':DirectoryAdapter(inspector=lambda p,**k:InterfaceInspection(format=k['use'].format,status='validated'))}
    plans=prep.inventory(model,input_directory=root,working_directory=root,directory_adapters=registry);fired=[]
    def change():
     if not fired and model.collection('test:directory')['R'].file.path.startswith('assets/'):
      fired.append(True)
      if existed:(source/'data').unlink();source.rmdir()
      else:source.mkdir();(source/'external').write_bytes(b'external')
    with self.assertRaises(ValueError):prep.stage(model,plans,work,'assets',checkpoint=change)
    self.assertEqual(fired,[True]);self.assertEqual(source.exists(),not existed)
    if not existed:self.assertEqual((source/'external').read_bytes(),b'external')
 def test_new_versions_reject_silent_legacy_absence(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);_,work,value,_=self.setup(root);r=value.resources[0];digest,_=write(root/'context',value);self.assertEqual(read(root/'context',digest),value);self.assertEqual(json.loads((root/'context/context.json').read_bytes())['version'],4);self.assertEqual(json.loads(record(root/'record',value))['schema_version'],'1.4')
   artifact=self.artifacts(work,value)[0]
   for item in (r,artifact):
    encoded=Codec(object(),result_version='1.6').encode(item)
    for version in ('1.4','1.5'):
     with self.assertRaises(ValueError):Codec(object(),result_version=version).encode(item)
     with self.assertRaises(ValueError):Codec(object(),result_version=version).decode(encoded)
   self.failed(work,value).save(root/'archive');self.save(root/'checkpoint',value)
   rewrite(root/'checkpoint',lambda d:d.update(schema_version='1.2'))
   with self.assertRaises((ValueError,TypeError)):storage.load(root/'checkpoint')
 def test_current_readers_reject_mislabeled_explicit_absence_formats(self):
  # Synthetic edits of current output exercise current readers, not old releases.
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);_,work,value,_=self.setup(root);digest,_=write(root/'context',value)
   self.failed(work,value).save(root/'archive');self.save(root/'checkpoint',value)
   with patch('ctypes.CDLL',side_effect=AssertionError('Format checks must stay offline')):
    self.assertEqual(RunResult.load(root/'archive').snapshot,value)
    self.assertEqual(read(root/'context',digest),value)
    self.assertEqual(storage.load(root/'checkpoint').snapshot,value)
    formats=(
     ('archive','result.json','schema_version','1.6','1.5',RunResult.load),
     ('context','context.json','version',4,3,lambda path:read(path,hashlib.sha256((path/'context.json').read_bytes()).hexdigest())),
     ('checkpoint','checkpoint.json','schema_version','1.3','1.2',storage.load))
    for name,filename,key,version,older,load in formats:
     data=json.loads((root/name/filename).read_bytes());self.assertEqual(data[key],version)
     for label in (older,99 if name=='context' else '99.0'):
      with self.subTest(format=name,label=label):
       altered=root/(name+'-'+str(label));shutil.copytree(root/name,altered)
       if name=='checkpoint':rewrite(altered,lambda data:data.update(schema_version=label))
       else:
        changed=dict(data);changed[key]=label;(altered/filename).write_text(json.dumps(changed),encoding='utf-8')
       # The context digest/checkpoint commit matches the edited bytes, so the
       # reader must reject the format rather than merely a stale checksum.
       with self.assertRaises(ValueError):load(altered)

 def test_absence_changes_during_output_capture_refuse_commit_and_retry(self):
  from types import SimpleNamespace
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);source,work,value,_=self.setup(root);current=work/value.resources[0].relative_path;out=work/'output';out.write_bytes(b'out');original=storage._Blobs.put_file;fired=[]
   def copying(blobs,path,*a,**k):
    result=original(blobs,path,*a,**k)
    if Path(path)==out:fired.append(True);current.mkdir()
    return result
   with self.assertRaises(ValueError):
    with storage.Builder(root/'bad',value) as b:
     with patch.object(storage._Blobs,'put_file',copying):b.finish(native_prefix(value,b.binding),(SimpleNamespace(index=0,role=0,text=False,path=out,size=3),))
   self.assertEqual(fired,[True]);self.assertFalse((root/'bad').exists());self.assertTrue(current.is_dir());self.assertFalse(source.exists());self.save(root/'retry',value)

if __name__=='__main__':unittest.main()
