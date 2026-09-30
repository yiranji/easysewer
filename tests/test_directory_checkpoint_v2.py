"""Directory storage/bootstrap evidence, not native restoration qualification."""
from dataclasses import replace
import hashlib,io,json,os,shutil,subprocess,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from easysewer.model import Ref
from easysewer.runtime.results import ResourceSnapshot
from easysewer.runtime._directory_tree import DirectoryManifest,DirectoryEntry,inspect_tree
from easysewer.runtime._checkpoint_container import Builder,Limits,load,execution_digest,_snapshot_files
from easysewer.runtime._checkpoint_context import read,write
from test_checkpoint_container_v2 import snapshot,native_prefix,rewrite


def resource(root):
 tree=root/'inputs/tree';tree.mkdir(parents=True);(tree/'empty').mkdir();(tree/'nested').mkdir();(tree/'nested/data').write_bytes(b'directory data');(tree/'zero').touch()
 return ResourceSnapshot(owner=Ref(collection='test:resources',key='R'),field=('file',),role='test:resource',format='test:tree',kind='directory',access='read',active=True,required=True,original_path='/original/tree',relative_path='inputs/tree',sha256=None,size=None,tree=inspect_tree(tree))


def record(root,value):
 from easysewer.runtime import Runner,EngineObjects,MassBalance
 from easysewer.io.output_metadata import OutputMetadata
 from easysewer.io.report_document import ReportDocument
 from test_output_v2 import binary_output
 (root/'assets').mkdir(parents=True)
 metadata=OutputMetadata.from_stream(io.BytesIO(binary_output()))
 Runner._execution_record(root,'assets',value,EngineObjects(groups=metadata.groups),metadata,MassBalance(runoff_percent=0.,flow_percent=0.,quality_percent=0.),{},None,ReportDocument.from_bytes(b'report\r\n'),(),{},())
 return (root/'assets/execution/execution.json').read_bytes()


class DirectoryCheckpointTests(unittest.TestCase):
 def test_directory_checkpoint_storage_relocation_contains_all_members(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);original=root/'original';original.mkdir();res=resource(original);value=snapshot(original,resources=(res,))
   with Builder(root/'saved',value) as builder:stored=builder.finish(native_prefix(value,builder.binding),())
   self.assertEqual(stored.data['schema_version'],'1.1');self.assertEqual(stored.snapshot.resources[0].tree,res.tree)
   shutil.rmtree(original);(root/'saved').rename(root/'moved')
   with patch('ctypes.CDLL',side_effect=AssertionError('No native state loading')):
    moved=load(root/'moved');self.assertEqual(moved.binding,stored.binding)
    for relative,desc in _snapshot_files(moved.snapshot).items():
     target=root/('copy-'+Path(relative).name);moved.copy_blob(desc,target);self.assertEqual(hashlib.sha256(target.read_bytes()).hexdigest(),desc['sha256'])
   self.assertEqual([e.path for e in moved.snapshot.resources[0].tree.entries if e.kind=='directory'],['empty','nested'])
   with self.assertRaisesRegex(ValueError,'directory adapter'):moved.materialize(root/'not-enabled')
   self.assertFalse((root/'not-enabled').exists())
 def test_empty_directory_membership_changes_binding_and_is_not_silent(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);res=resource(root);value=snapshot(root,resources=(res,));first=execution_digest(value,[])
   changed=replace(res,tree=DirectoryManifest(entries=tuple(e for e in res.tree.entries if e.path!='empty')))
   self.assertNotEqual(execution_digest(replace(value,resources=(changed,)),[]),first)
   self.assertEqual(execution_digest(replace(value,execution_directory='/moved',backend=replace(value.backend,library='/new')),[]),first)
   with self.assertRaises(ValueError):
    with Builder(root/'bad',replace(value,resources=(changed,))):pass
   self.assertFalse((root/'bad').exists());self.assertTrue((root/'inputs/tree/empty').is_dir())
 def test_capture_changes_during_copy_cancel_limits_and_retry(self):
  for mode in ('added-empty','changed-file','cancel','limit'):
   with self.subTest(mode=mode),tempfile.TemporaryDirectory() as tmp:
    root=Path(tmp);res=resource(root);value=snapshot(root,resources=(res,));error=OSError('cancel capture');error.__cause__=ValueError('cause');real=None;triggered=[]
    from easysewer.runtime import _checkpoint_container as storage
    real=storage._Blobs.put_file
    def changed(blobs,path,*args,**kwargs):
     result=real(blobs,path,*args,**kwargs)
     if Path(path).as_posix().endswith('nested/data'):
      triggered.append(True)
      if mode=='added-empty':(root/'inputs/tree/added').mkdir()
      elif mode=='changed-file':Path(path).write_bytes(b'changed content')
      elif mode=='cancel':raise error
     return result
    with patch.object(storage._Blobs,'put_file',changed),self.assertRaises((OSError,ValueError)) as caught:
     with Builder(root/'bad',value,limits=Limits(total_bytes=1) if mode=='limit' else Limits()):pass
    if mode!='limit':self.assertEqual(triggered,[True])
    if mode=='cancel':self.assertIs(caught.exception,error);self.assertIsInstance(caught.exception.__cause__,ValueError)
    self.assertFalse((root/'bad').exists());self.assertTrue((root/'inputs/tree').is_dir())
    current=replace(res,tree=inspect_tree(root/'inputs/tree'));value=replace(value,resources=(current,))
    with Builder(root/'retry',value) as builder:saved=builder.finish(native_prefix(value,builder.binding),())
    self.assertEqual(saved.snapshot.resources[0].tree,current.tree)
 def test_shared_and_conflicting_tree_identity_and_portable_paths(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);res=resource(root);value=snapshot(root,resources=(res,));shared=replace(res,owner=Ref(collection='test:resources',key='S'))
   self.assertEqual(_snapshot_files(replace(value,resources=(res,shared))),_snapshot_files(value))
   leaf=next(e for e in res.tree.entries if e.kind=='file')
   file=replace(res,tree=None,kind='file',relative_path=res.relative_path+'/'+leaf.path,sha256=leaf.sha256,size=leaf.size,owner=shared.owner)
   self.assertEqual(_snapshot_files(replace(value,resources=(res,file))),_snapshot_files(value))
   variants=[replace(shared,relative_path='INPUTS/TREE'),replace(shared,relative_path='inputs/tree/nested'),replace(shared,tree=DirectoryManifest(entries=())),replace(file,relative_path='inputs/tree/unlisted'),replace(file,relative_path='inputs'),replace(res,relative_path='../outside'),replace(res,tree=None),replace(shared,relative_path='INPUTS/other'),replace(file,relative_path='INPUTS/another-file')]
   for other in variants:
    with self.subTest(other=other),self.assertRaises(ValueError):_snapshot_files(replace(value,resources=(res,other)))
 def test_bootstrap_roundtrip_versions_tampering_and_old_reader_refusal(self):
  old=os.environ.get('EASYSEWER_DIRECTORY_OLD_PACKAGE');tests=os.environ.get('EASYSEWER_DIRECTORY_OLD_TESTS')
  if not old or not tests:self.skipTest('Old package not configured')
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);res=resource(root);value=snapshot(root,resources=(res,));digest,_=write(root/'context',value)
   self.assertEqual(read(root/'context',digest),value);data=json.loads((root/'context/context.json').read_bytes());self.assertEqual(data['version'],2)
   with Builder(root/'saved',value) as builder:builder.finish(native_prefix(value,builder.binding),())
   code="import sys;from pathlib import Path;sys.path[:0]=sys.argv[1:3];from easysewer.runtime._checkpoint_context import read;from easysewer.runtime._checkpoint_container import load;root=Path(sys.argv[3]);\nfor operation in (lambda:read(root/'context',sys.argv[4]),lambda:load(root/'saved')):\n try:operation()\n except ValueError as error:assert 'Unsupported' in str(error),str(error)\n else:raise AssertionError('Old reader accepted new directory version')"
   p=subprocess.run([sys.executable,'-I','-B','-c',code,old,tests,str(root),digest],capture_output=True,text=True);self.assertEqual(p.returncode,0,p.stdout+p.stderr)
   rewrite(root/'saved',lambda d:d.update(schema_version='1.0'))
   with self.assertRaises(ValueError):load(root/'saved')
   path=root/'context/context.json';data['version']=1;path.write_text(json.dumps(data));new=hashlib.sha256(path.read_bytes()).hexdigest()
   with self.assertRaises(ValueError):read(root/'context',new)
 def test_execution_record_has_explicit_directory_version_and_all_members(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);res=resource(root);value=snapshot(root,resources=(res,));data=json.loads(record(root/'record',value))
   self.assertEqual(data['schema_version'],'1.2');tree=data['resources'][0]['tree'];self.assertEqual(tree['contract'],res.tree.contract);self.assertEqual([e['path'] for e in tree['entries']],[e.path for e in res.tree.entries])
 def test_real_old_package_file_only_record_context_and_checkpoint_exact_bytes(self):
  old=os.environ.get('EASYSEWER_DIRECTORY_OLD_PACKAGE');tests=os.environ.get('EASYSEWER_DIRECTORY_OLD_TESTS')
  if not old or not tests:self.skipTest('Old package not configured')
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);(root/'input').write_bytes(b'ordinary resource')
   res=ResourceSnapshot(owner=Ref(collection='test:resources',key='R'),field=('file',),role='test:resource',format='test:file',kind='file',access='read',active=True,required=True,original_path='/old/input',relative_path='input',sha256=hashlib.sha256(b'ordinary resource').hexdigest(),size=17)
   value=snapshot(root,resources=(res,));raw=record(root/'current-record',value);self.assertEqual(json.loads(raw)['schema_version'],'1.1');self.assertNotIn('tree',json.loads(raw)['resources'][0])
   digest,_=write(root/'current-context',value)
   with Builder(root/'current-checkpoint',value) as builder:builder.finish(native_prefix(value,builder.binding),())
   # This module cannot be imported under the old package: its directory types do not exist.
   # Execute its generic record helper only, against genuinely old runtime classes.
   import inspect
   helper=inspect.getsource(record)
   code="import sys,io,json,hashlib;from pathlib import Path;sys.path[:0]=sys.argv[1:3];from easysewer.model import Ref;from easysewer.runtime.results import ResourceSnapshot;from easysewer.runtime._checkpoint_context import write;from easysewer.runtime._checkpoint_container import Builder;from test_checkpoint_container_v2 import snapshot,native_prefix;root=Path(sys.argv[3]);\n"+helper+"\nres=ResourceSnapshot(owner=Ref(collection='test:resources',key='R'),field=('file',),role='test:resource',format='test:file',kind='file',access='read',active=True,required=True,original_path='/old/input',relative_path='input',sha256=hashlib.sha256(b'ordinary resource').hexdigest(),size=17);value=snapshot(root,resources=(res,));record(root/'old-record',value);write(root/'old-context',value)\nwith Builder(root/'old-checkpoint',value) as builder:builder.finish(native_prefix(value,builder.binding),())"
   p=subprocess.run([sys.executable,'-I','-B','-c',code,old,tests,str(root)],capture_output=True,text=True);self.assertEqual(p.returncode,0,p.stdout+p.stderr)
   for kind in ('record','context','checkpoint'):
    def contents(folder):return {p.relative_to(folder).as_posix():p.read_bytes() for p in folder.rglob('*') if p.is_file()}
    self.assertEqual(contents(root/('current-'+kind)),contents(root/('old-'+kind)),kind)

if __name__=='__main__':unittest.main()
