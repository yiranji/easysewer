"""Separate original evidence from mutable trees; native tails here are synthetic."""
from dataclasses import replace
from pathlib import Path
import hashlib,io,json,shutil,tempfile,unittest
from unittest.mock import patch
from easysewer.runtime import _preparation as prep
from easysewer.runtime import _checkpoint_container as storage
from easysewer.runtime._directory_tree import inspect_tree,DirectoryManifest
from easysewer.runtime.directory_resources import DirectoryAdapter
from easysewer.runtime.results import DirectoryArtifact,RunResult,FileArtifact
from easysewer.runtime._result_codec import Codec
from easysewer.runtime._checkpoint_context import write,read
from easysewer.io.interface_inspection import InterfaceInspection
from directory_roundtrip_fixture import prepared,row,schema
from test_checkpoint_container_v2 import snapshot,native_prefix,rewrite
from test_directory_checkpoint_v2 import record
from test_result_archive_v2 import failure_result

class MutableDirectoryTests(unittest.TestCase):
 def setup(self,root,shared=True):
  source=root/'source';source.mkdir();(source/'empty').mkdir();(source/'data').write_bytes(b'initial');(source/'removed').write_bytes(b'remove me')
  rows=(row('R','source'),row('W','source',access='read_write')) if shared else (row('W','source',access='read_write'),)
  _,model=prepared(root,rows);workspace=root/'work';workspace.mkdir()
  registry={'test:directory.format':DirectoryAdapter(inspector=lambda p,**k:InterfaceInspection(format=k['use'].format,status='validated'))}
  plans=prep.inventory(model,input_directory=root,working_directory=root,directory_adapters=registry)
  records,_,_=prep.stage(model,plans,workspace,'assets',checkpoint=lambda:None)
  value=snapshot(workspace,model=model,resources=records)
  return source,workspace,value
 def mutate(self,workspace,value):
  current=workspace/value.resources[0].relative_path
  (current/'data').write_bytes(b'current');(current/'removed').unlink();(current/'empty').rmdir();(current/'new-empty').mkdir();(current/'new').write_bytes(b'added')
  return current,inspect_tree(current)
 def artifacts(self,workspace,value,complete=False):
  output=[]
  for r in value.resources:
   for role,name in (('run:resource',r.initial_relative_path),('run:resource_state',r.relative_path)):
    output.append(DirectoryArtifact.from_path(workspace/name,role=role,owner=r.owner,field=r.field,complete=complete))
  return tuple(output)
 def save_checkpoint(self,root,value):
  with storage.Builder(root,value) as b:return b.finish(native_prefix(value,b.binding),())
 def failed(self,workspace,value):
  return replace(failure_result(),run_id=value.run_id,snapshot=value,backend=value.backend,directory_artifacts=self.artifacts(workspace,value))
 def success(self,workspace,value):
  from easysewer.runtime import EngineObjects,MassBalance
  from easysewer.validation import ValidationReport
  from easysewer.io.output_metadata import OutputMetadata,FLOW_UNITS
  from easysewer.io.report_document import ReportDocument
  from test_output_v2 import binary_output
  raw=binary_output(flow_units=FLOW_UNITS.index(value.units.flow_units));report=ReportDocument.from_bytes(b'report\r\n');meta=replace(OutputMetadata.from_stream(io.BytesIO(raw)),producer=value.backend.key,semantics=value.backend.output_semantics)
  files=[]
  for role,name,data in (('run:input','model.inp',value.input_bytes),('run:report','model.rpt',report.raw),('run:output','model.out',raw)):
   path=workspace/name;path.write_bytes(data);files.append(FileArtifact(role=role,path=str(path),sha256=hashlib.sha256(data).hexdigest(),size=len(data),complete=True))
  return RunResult(run_id=value.run_id,status='succeeded',diagnostics=ValidationReport(),native_completed=True,snapshot=value,backend=value.backend,artifacts=tuple(files),directory_artifacts=self.artifacts(workspace,value,True),engine_objects=EngineObjects(groups=meta.groups),mass_balance=MassBalance(runoff_percent=0.,flow_percent=0.,quality_percent=0.),output_metadata=meta,report_document=report)
 def test_shared_execution_keeps_two_independent_trees_and_original_unchanged(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);source,work,value=self.setup(root);a,b=value.resources
   self.assertEqual((a.relative_path,a.initial_relative_path,a.tree),(b.relative_path,b.initial_relative_path,b.tree));self.assertNotEqual(a.relative_path,a.initial_relative_path)
   initial=work/a.initial_relative_path;current=work/a.relative_path
   self.assertFalse((initial/'data').samefile(current/'data'));self.assertFalse((source/'data').samefile(initial/'data'))
   prep.verify_resources(value.resources,work,checkpoint=lambda:None,initial_execution=True)
   self.mutate(work,value);prep.verify_resources(value.resources,work,checkpoint=lambda:None)
   with self.assertRaises(ValueError):prep.verify_resources(value.resources,work,checkpoint=lambda:None,initial_execution=True)
   self.assertEqual(inspect_tree(source),a.tree);self.assertEqual(inspect_tree(initial),a.tree)
 def test_initial_tampering_and_conflicting_shared_evidence_are_rejected(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);source,work,value=self.setup(root);a,b=value.resources
   for resources in ((replace(a,initial_relative_path=None),b),(replace(a,access='read'),replace(b,access='read')),(a,replace(b,initial_relative_path='other'))):
    with self.assertRaises(ValueError):replace(value,resources=resources)
   for name in (a.relative_path,a.relative_path+'/inside','../escape','ASSETS/inputs/r0.dir'):
    with self.assertRaises(ValueError):replace(a,initial_relative_path=name)
   (work/a.initial_relative_path/'extra').mkdir()
   with self.assertRaises(ValueError):prep.verify_resources(value.resources,work,checkpoint=lambda:None)
   with self.assertRaises(ValueError):self.save_checkpoint(root/'bad',value)
   self.assertFalse((root/'bad').exists());self.assertEqual(inspect_tree(source),a.tree)
 def test_failed_archive_roundtrip_preserves_initial_and_current_after_source_deletion(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);source,work,value=self.setup(root);current,manifest=self.mutate(work,value);self.failed(work,value).save(root/'saved')
   self.assertEqual(json.loads((root/'saved/result.json').read_bytes())['schema_version'],'1.5')
   shutil.rmtree(work);shutil.rmtree(source);(root/'saved').rename(root/'moved');loaded=RunResult.load(root/'moved')
   for i,r in enumerate(value.resources):
    initial=loaded.directory_artifact('run:resource',owner=r.owner,field=r.field);state=loaded.directory_artifact('run:resource_state',owner=r.owner,field=r.field)
    self.assertEqual(initial.manifest,r.tree);self.assertEqual(state.manifest,manifest);self.assertEqual(initial.file('data').read_bytes(),b'initial');self.assertEqual(state.file('data').read_bytes(),b'current')
    initial.materialize(root/('initial'+str(i)));state.materialize(root/('current'+str(i)))
   loaded.save(root/'again');self.assertEqual(RunResult.load(root/'again').snapshot,value)
 def test_success_requires_both_evidence_roles_and_shared_current_manifest(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);source,work,value=self.setup(root);self.mutate(work,value);result=self.success(work,value);result.save(root/'success');self.assertTrue(RunResult.load(root/'success').succeeded)
   with self.assertRaisesRegex(ValueError,'final state'):replace(result,directory_artifacts=tuple(a for a in result.directory_artifacts if a.role!='run:resource_state'))
   altered=[]
   for a in result.directory_artifacts:
    if a.role=='run:resource_state' and a.owner==value.resources[1].owner:
     r=value.resources[1];a=DirectoryArtifact.from_path(work/r.initial_relative_path,role=a.role,owner=a.owner,field=a.field,complete=True)
    altered.append(a)
   with self.assertRaisesRegex(ValueError,'disagree'):replace(result,directory_artifacts=tuple(altered))
   with self.assertRaisesRegex(ValueError,'snapshot'):replace(result,directory_artifacts=tuple(a for a in result.directory_artifacts if a.role!='run:resource'))
 def test_checkpoint_relocation_restores_current_and_initial_and_preserves_execution_identity(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);source,work,value=self.setup(root);before=self.save_checkpoint(root/'before',value);current,manifest=self.mutate(work,value);after=self.save_checkpoint(root/'after',value)
   self.assertEqual(before.binding,after.binding);self.assertNotEqual(before.manifest,after.manifest);self.assertEqual(after.data['schema_version'],'1.2')
   shutil.rmtree(work);shutil.rmtree(source);(root/'after').rename(root/'moved')
   with patch('ctypes.CDLL',side_effect=AssertionError('No native loading')):
    loaded=storage.load(root/'moved');restored=loaded.materialize(root/'restored',schema=schema())
   r=value.resources[0];self.assertEqual(inspect_tree(root/'restored'/r.relative_path),manifest);self.assertEqual(inspect_tree(root/'restored'/r.initial_relative_path),r.tree)
   prep.verify_resources(restored.resources,Path(restored.execution_directory),checkpoint=lambda:None)
   rebuilt=self.save_checkpoint(root/'rebuilt',restored);self.assertEqual(rebuilt.binding,after.binding);self.assertEqual(rebuilt.data['directory_states'],after.data['directory_states'])
   before.materialize(root/'earlier',schema=schema());self.assertEqual(inspect_tree(root/'earlier'/r.relative_path),r.tree)
 def test_mutable_state_inventory_rejects_missing_duplicate_foreign_and_wrong_kind(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);_,work,value=self.setup(root);self.mutate(work,value)
   mutations=(lambda d:d.update(directory_states=[]),lambda d:d['directory_states'].append(d['directory_states'][0]),lambda d:d['directory_states'][0].update(relative_path='foreign'),lambda d:d['directory_states'][0].update(tree=42),lambda d:d.update(schema_version='1.1'))
   for i,mutate in enumerate(mutations):
    saved=root/('bad'+str(i));self.save_checkpoint(saved,value);rewrite(saved,mutate)
    with self.assertRaises((ValueError,TypeError)):storage.load(saved)
 def test_mutation_during_checkpoint_capture_and_after_binding_refuses_commit_then_retry(self):
  for phase in ('copy','after_binding'):
   with self.subTest(phase=phase),tempfile.TemporaryDirectory() as tmp:
    root=Path(tmp);source,work,value=self.setup(root);current,_=self.mutate(work,value);original=storage._Blobs.put_file;fired=[]
    def copy(blobs,path,*a,**k):
     result=original(blobs,path,*a,**k)
     if Path(path)==current/'data' and not fired:fired.append(True);(current/'late-empty').mkdir()
     return result
    with self.assertRaises(ValueError):
     if phase=='copy':
      with patch.object(storage._Blobs,'put_file',copy):self.save_checkpoint(root/'bad',value)
     else:
      with storage.Builder(root/'bad',value) as b:
       binding=b.binding;(current/'late-empty').mkdir();b.finish(native_prefix(value,binding),())
    self.assertFalse((root/'bad').exists());self.assertTrue((current/'late-empty').is_dir());self.assertEqual(inspect_tree(source),value.resources[0].tree)
    saved=self.save_checkpoint(root/'retry',value);self.assertTrue(saved.data['directory_states'])
 def test_current_capture_cancellation_and_limits_preserve_cause_and_sources(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);source,work,value=self.setup(root);current,_=self.mutate(work,value);error=OSError('current copy cancelled');error.__cause__=ValueError('cause');original=storage._Blobs.put_file;fired=[]
   def copy(blobs,path,*a,**k):
    if Path(path)==current/'data':fired.append(True);raise error
    return original(blobs,path,*a,**k)
   with patch.object(storage._Blobs,'put_file',copy),self.assertRaises(OSError) as caught:self.save_checkpoint(root/'cancel',value)
   self.assertIs(caught.exception,error);self.assertIsInstance(caught.exception.__cause__,ValueError);self.assertEqual(fired,[True]);self.assertFalse((root/'cancel').exists())
   with self.assertRaises(ValueError):
    with storage.Builder(root/'limited',value,limits=storage.Limits(total_bytes=1)):pass
   self.assertFalse((root/'limited').exists());self.assertEqual(inspect_tree(source),value.resources[0].tree)
 def test_context_record_codec_versions_reject_older_mutable_state_formats(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);_,work,value=self.setup(root);digest,_=write(root/'context',value);self.assertEqual(read(root/'context',digest),value);self.assertEqual(json.loads((root/'context/context.json').read_bytes())['version'],3)
   self.assertEqual(json.loads(record(root/'record',value))['schema_version'],'1.3')
   resource=value.resources[0];encoded=Codec(object(),result_version='1.5').encode(resource);self.assertEqual(Codec(object(),result_version='1.5').decode(encoded),resource)
   for version in ('1.0','1.1','1.2','1.3','1.4'):
    with self.assertRaises(ValueError):Codec(object(),result_version=version).encode(resource)
    with self.assertRaises(ValueError):Codec(object(),result_version=version).decode(encoded)
   self.failed(work,value).save(root/'archive');self.save_checkpoint(root/'checkpoint',value)

 def test_current_readers_reject_mislabeled_mutable_state_formats(self):
  # Synthetic edits of current output exercise current readers, not old releases.
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);_,work,value=self.setup(root);digest,_=write(root/'context',value)
   self.failed(work,value).save(root/'archive');self.save_checkpoint(root/'checkpoint',value)
   with patch('ctypes.CDLL',side_effect=AssertionError('Format checks must stay offline')):
    self.assertEqual(RunResult.load(root/'archive').snapshot,value)
    self.assertEqual(read(root/'context',digest),value)
    self.assertEqual(storage.load(root/'checkpoint').snapshot,value)
    formats=(
     ('archive','result.json','schema_version','1.5','1.4',RunResult.load),
     ('context','context.json','version',3,2,lambda path:read(path,hashlib.sha256((path/'context.json').read_bytes()).hexdigest())),
     ('checkpoint','checkpoint.json','schema_version','1.2','1.1',storage.load))
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

 def test_materialization_failure_cleans_only_new_root_and_retry_restores_both_trees(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);source,work,value=self.setup(root);_,manifest=self.mutate(work,value);saved=self.save_checkpoint(root/'saved',value);error=OSError('copy failed');error.__cause__=ValueError('primary');original=storage.Checkpoint.copy_blob;fired=[]
   def copy(obj,desc,target,**k):
    result=original(obj,desc,target,**k)
    if 'inputs' in Path(target).parts and not fired:fired.append(True);raise error
    return result
   with patch.object(storage.Checkpoint,'copy_blob',copy),self.assertRaises(OSError) as caught:saved.materialize(root/'failed',schema=schema())
   self.assertIs(caught.exception,error);self.assertFalse((root/'failed').exists());self.assertEqual(fired,[True]);self.assertEqual(inspect_tree(source),value.resources[0].tree)
   restored=saved.materialize(root/'retry',schema=schema());self.assertEqual(inspect_tree(Path(restored.execution_directory)/value.resources[0].relative_path),manifest)

 def test_output_copy_cannot_commit_after_mutable_directory_changes(self):
  from types import SimpleNamespace
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);source,work,value=self.setup(root);current,_=self.mutate(work,value);out=work/'output';out.write_bytes(b'output');original=storage._Blobs.put_file;fired=[]
   def copying(blobs,path,*a,**k):
    result=original(blobs,path,*a,**k)
    if Path(path)==out:fired.append(True);(current/'late-output-copy-change').mkdir()
    return result
   with self.assertRaises(ValueError):
    with storage.Builder(root/'saved',value) as b:
     with patch.object(storage._Blobs,'put_file',copying):b.finish(native_prefix(value,b.binding),(SimpleNamespace(index=0,role=0,text=False,path=out,size=6),))
   self.assertEqual(fired,[True]);self.assertFalse((root/'saved').exists());self.assertTrue((current/'late-output-copy-change').is_dir());self.assertEqual((source/'data').read_bytes(),b'initial')

if __name__=='__main__':unittest.main()
