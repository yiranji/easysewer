"""Directory evidence roundtrip without original paths or native execution."""
from dataclasses import replace
import json,shutil,tempfile,unittest
from pathlib import Path
from easysewer.runtime.results import DirectoryArtifact,ResourceSnapshot,RunResult
from easysewer.runtime._directory_tree import DirectoryManifest,inspect_tree
from easysewer.runtime._result_codec import Codec
from easysewer.model import Ref
from test_result_archive_v2 import failure_result

class DirectoryArtifactTests(unittest.TestCase):
 def source(self,parent):
  p=parent/'source';p.mkdir();(p/'empty').mkdir();(p/'nested').mkdir();(p/'nested/data.bin').write_bytes(b'complete bytes');(p/'zero').write_bytes(b'');return p
 def artifact(self,path,complete=False):
  return DirectoryArtifact.from_path(path,role='run:resource',owner=Ref(collection='test:resources',key='R'),field=('file',),complete=complete,declared_path='declared/original')
 def test_failed_result_archive_relocation_materialization_and_empty_dirs(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);source=self.source(root);artifact=self.artifact(source);result=replace(failure_result(),directory_artifacts=(artifact,))
   result.save(root/'archive');envelope=json.loads((root/'archive/result.json').read_bytes());self.assertEqual(envelope['schema_version'],'1.4')
   self.assertTrue(source.resolve().is_relative_to(root.resolve()));shutil.rmtree(source);(root/'archive').rename(root/'moved')
   loaded=RunResult.load(root/'moved');directory=loaded.directory_artifact('run:resource',owner=artifact.owner,field=('file',))
   self.assertIsNone(directory.path);self.assertEqual(directory.original_path,artifact.original_path);self.assertEqual(directory.manifest,artifact.manifest)
   self.assertEqual(directory.file('nested/data.bin').read_bytes(),b'complete bytes');self.assertEqual(directory.file('zero').read_bytes(),b'')
   restored=directory.materialize(root/'restored');self.assertEqual(inspect_tree(restored.path),artifact.manifest);self.assertTrue((root/'restored/empty').is_dir());restored.verify()
   self.assertFalse(directory.complete);self.assertEqual(loaded.failure,result.failure)
   loaded.save(root/'saved_again');self.assertEqual(RunResult.load(root/'saved_again').directory_artifact('run:resource').manifest,artifact.manifest)
 def test_live_source_member_changes_and_empty_directory_additions_reject_save(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);source=self.source(root);artifact=self.artifact(source);result=replace(failure_result(),directory_artifacts=(artifact,))
   (source/'extra-empty').mkdir()
   with self.assertRaises(ValueError):result.save(root/'bad')
   self.assertFalse((root/'bad').exists());self.assertTrue((source/'extra-empty').is_dir())
 def test_manifest_and_member_invariants_are_checked_without_filesystem_io(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);artifact=self.artifact(self.source(root));first=artifact.files[0]
   with self.assertRaises(ValueError):replace(artifact,files=artifact.files[:-1])
   with self.assertRaises(ValueError):replace(artifact,files=(('wrong',first[1]),*artifact.files[1:]))
   with self.assertRaises(ValueError):replace(artifact,files=((first[0],replace(first[1],sha256='0'*64)),*artifact.files[1:]))
   with self.assertRaises(ValueError):replace(artifact,complete=True)
   with self.assertRaises(ValueError):replace(failure_result(),directory_artifacts=(artifact,artifact))
   with self.assertRaises(ValueError):replace(failure_result(),directory_artifacts=(self.artifact(root/'source',complete=True),))
   with self.assertRaises(KeyError):artifact.file('empty')
   with self.assertRaises(ValueError):artifact.file('../outside')
 def test_existing_materialization_and_overlap_are_rejected(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);source=self.source(root);artifact=self.artifact(source);target=root/'existing';target.mkdir();(target/'keep').write_bytes(b'old')
   with self.assertRaises(FileExistsError):artifact.materialize(target)
   self.assertEqual((target/'keep').read_bytes(),b'old')
   with self.assertRaises(ValueError):artifact.materialize(source/'nested'/'new')
   self.assertEqual(inspect_tree(source),artifact.manifest)
 def test_materialization_cancel_keeps_exception_and_source_then_retry(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);source=self.source(root);artifact=self.artifact(source);target=root/'partial';cause=ValueError('cause');error=OSError('cancel');error.__cause__=cause
   def stop():
    if target.exists():raise error
   with self.assertRaises(OSError) as caught:artifact.materialize(target,checkpoint=stop)
   self.assertIs(caught.exception,error);self.assertIs(caught.exception.__cause__,cause);self.assertTrue(target.is_dir());self.assertEqual(inspect_tree(source),artifact.manifest)
   self.assertEqual(artifact.materialize(root/'retry').manifest,artifact.manifest)
 def test_snapshot_tree_and_legacy_codec_explicit_migration(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);manifest=inspect_tree(self.source(root));resource=ResourceSnapshot(owner=Ref(collection='test:resources',key='R'),field=('file',),role='test:resource',format='test:tree',kind='directory',access='read',active=True,required=True,original_path=str(root/'source'),relative_path='assets/tree',sha256=None,size=None,tree=manifest)
   self.assertEqual(Codec(object(),result_version='1.4').decode(Codec(object(),result_version='1.4').encode(resource)),resource)
   for version in ('1.0','1.1','1.2','1.3'):
    codec=Codec(object(),result_version=version)
    with self.assertRaises(ValueError):codec.encode(resource)
    with self.assertRaises(ValueError):codec.decode(Codec(object(),result_version='1.4').encode(resource))
    plain=replace(resource,tree=None,kind='file');self.assertEqual(codec.decode(codec.encode(plain)),plain)
    self.assertNotIn('tree',codec.encode(plain)['fields'])
   with self.assertRaises(ValueError):replace(resource,kind='file')
   with self.assertRaises(ValueError):replace(resource,access='write')
   with self.assertRaises(ValueError):replace(resource,sha256=manifest.sha256,size=manifest.total_bytes)
 def test_current_archive_directory_free_bytes_and_directory_version_boundary(self):
  # Same-version repeatability and lossless reserialization, not an old-writer oracle.
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);plain=failure_result();plain.save(root/'first');plain.save(root/'repeat')
   RunResult.load(root/'first').save(root/'roundtrip')
   def contents(folder):return {p.relative_to(folder).as_posix():p.read_bytes() for p in folder.rglob('*') if p.is_file()}
   for name in ('repeat','roundtrip'):self.assertEqual(contents(root/'first'),contents(root/name))
   data=json.loads((root/'first/result.json').read_bytes());self.assertEqual(data['schema_version'],'1.3')
   self.assertNotIn('directory_artifacts',data['result']['fields']);self.assertNotIn('directory_group_artifacts',data['result']['fields'])
   artifact=self.artifact(self.source(root));replace(plain,directory_artifacts=(artifact,)).save(root/'directory')
   path=root/'directory/result.json';raw=path.read_bytes();data=json.loads(raw);self.assertEqual(data['schema_version'],'1.4')
   for version in ('1.3','99.0'):
    with self.subTest(version=version):
     data['schema_version']=version;path.write_text(json.dumps(data),encoding='utf-8')
     with self.assertRaises(ValueError):RunResult.load(root/'directory')
   path.write_bytes(raw);self.assertEqual(RunResult.load(root/'directory').directory_artifact('run:resource').manifest,artifact.manifest)
 def test_changed_archived_member_is_detected(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);artifact=self.artifact(self.source(root));replace(failure_result(),directory_artifacts=(artifact,)).save(root/'archive')
   restored=RunResult.load(root/'archive').directory_artifact('run:resource');Path(restored.file('nested/data.bin').path).write_bytes(b'changed')
   with self.assertRaises(ValueError):restored.verify()
   with self.assertRaises(ValueError):RunResult.load(root/'archive')
if __name__=='__main__':unittest.main()
