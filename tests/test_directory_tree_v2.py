"""Directory-tree evidence: bytes, ownership, complete membership and failure."""
from dataclasses import replace
import hashlib,os,stat,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from easysewer.runtime import _directory_tree as tree
from easysewer.runtime._directory_tree import DirectoryLimits,DirectoryEntry,DirectoryManifest,inspect_tree,capture_tree,verify_tree
from easysewer.validation._cooperative import checkpoint_scope

class DirectoryTreeTests(unittest.TestCase):
 def make_source(self,parent):
  root=parent/'source';root.mkdir();(root/'empty').mkdir();(root/'nested').mkdir();(root/'nested'/'deeper').mkdir()
  (root/'nested'/'deeper'/'中文 space.bin').write_bytes(bytes(range(256))*513)
  (root/'zero').write_bytes(b'');(root/'data').write_bytes(b'original payload');return root

 def test_complete_capture_manifest_and_relocated_copy(self):
  with tempfile.TemporaryDirectory() as tmp:
   parent=Path(tmp);source=self.make_source(parent);before=inspect_tree(source);target=parent/'capture'
   actual=capture_tree(source,target,checkpoint=lambda:None)
   self.assertEqual(actual,before);self.assertEqual(verify_tree(target,before),before);self.assertEqual(inspect_tree(source),before)
   self.assertEqual(actual.total_bytes,256*513+len(b'original payload'));self.assertEqual(len(actual.entries),6)
   self.assertEqual(actual.sha256,before.sha256);self.assertEqual(len(actual.sha256),64)
   self.assertTrue((target/'empty').is_dir());self.assertEqual((target/'nested/deeper/中文 space.bin').read_bytes(),(source/'nested/deeper/中文 space.bin').read_bytes())
   relocated=parent/'relocated';target.rename(relocated);self.assertEqual(verify_tree(relocated,before),before)

 def test_empty_tree_evidence_and_membership_changes(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp)/'empty';root.mkdir();manifest=inspect_tree(root);self.assertEqual(manifest.entries,());self.assertEqual(manifest.total_bytes,0)
   target=Path(tmp)/'target';self.assertEqual(capture_tree(root,target),manifest)
   (target/'empty_child').mkdir()
   with self.assertRaises(ValueError):verify_tree(target,manifest)
   self.assertNotEqual(inspect_tree(target).sha256,manifest.sha256)

 def test_content_added_removed_renamed_and_empty_directories_are_bound(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=self.make_source(Path(tmp));expected=inspect_tree(root)
   for operation in ('change','add','remove','rename','remove_empty'):
    target=Path(tmp)/operation;capture_tree(root,target)
    if operation=='change':(target/'data').write_bytes(b'changed! payload')
    elif operation=='add':(target/'extra').write_bytes(b'new')
    elif operation=='remove':(target/'zero').unlink()
    elif operation=='rename':(target/'data').rename(target/'different')
    else:(target/'empty').rmdir()
    with self.assertRaises(ValueError):verify_tree(target,expected)
   self.assertEqual(inspect_tree(root),expected)

 def test_limits_are_exact_and_partial_capture_never_overwrites_existing(self):
  with tempfile.TemporaryDirectory() as tmp:
   parent=Path(tmp);source=self.make_source(parent);manifest=inspect_tree(source);exact=DirectoryLimits(total_bytes=manifest.total_bytes,entries=6,depth=3)
   self.assertEqual(inspect_tree(source,limits=exact),manifest)
   for limits in (replace(exact,total_bytes=exact.total_bytes-1),replace(exact,entries=5),replace(exact,depth=2)):
    with self.assertRaises(ValueError):capture_tree(source,parent/'unused',limits=limits)
    self.assertFalse((parent/'unused').exists())
   target=parent/'existing';target.mkdir();(target/'keep').write_bytes(b'old')
   with self.assertRaises(FileExistsError):capture_tree(source,target)
   self.assertEqual((target/'keep').read_bytes(),b'old');self.assertEqual(list(target.iterdir()),[target/'keep'])
   for bad in (0,-1,True,1.0):
    with self.assertRaises(ValueError):DirectoryLimits(entries=bad)

 def test_manifest_rejects_ambiguous_paths_duplicates_and_missing_parents(self):
  for path in ('','/root','../bad','a/../bad','a//b','a\\b','C:x','nul.txt','trailing.','a/COM1','new\nline','a?b'):
   with self.assertRaises(ValueError):DirectoryEntry(path=path,kind='directory')
  d=lambda p:DirectoryEntry(path=p,kind='directory')
  for rows in ((d('a/b'),),(d('b'),d('a')),(d('a'),d('a')),(d('A'),d('a'))):
   with self.assertRaises(ValueError):DirectoryManifest(entries=rows)
  with self.assertRaises(TypeError):DirectoryManifest(entries=[])
  with self.assertRaises(ValueError):DirectoryManifest(entries=(),contract='future')
  with self.assertRaises(ValueError):DirectoryEntry(path='a',kind='file',sha256='0'*64,size=True)
  with self.assertRaises(ValueError):DirectoryEntry(path='a',kind='directory',sha256='0'*64,size=0)

 def test_cancellation_identity_cause_input_unchanged_and_retry(self):
  with tempfile.TemporaryDirectory() as tmp:
   parent=Path(tmp);source=self.make_source(parent);before=inspect_tree(source);target=parent/'partial';calls=[];cause=ValueError('root cause');error=OSError('cancel directory');error.__cause__=cause
   def cancel():
    calls.append(1)
    if target.exists() and len(list(target.iterdir()))>=1:raise error
   with self.assertRaises(OSError) as caught:capture_tree(source,target,checkpoint=cancel)
   self.assertIs(caught.exception,error);self.assertIs(caught.exception.__cause__,cause);self.assertEqual(inspect_tree(source),before)
   self.assertTrue(target.is_dir())
   self.assertEqual(capture_tree(source,parent/'retry'),before)
   checks=[];interrupt=KeyboardInterrupt('stop inspection')
   def stop():
    checks.append(1)
    if len(checks)==5:raise interrupt
   with self.assertRaises(KeyboardInterrupt) as caught,checkpoint_scope(stop):inspect_tree(source)
   self.assertIs(caught.exception,interrupt);self.assertEqual(inspect_tree(source),before)

 def test_source_mutation_same_length_and_mtime_during_capture_is_rejected(self):
  with tempfile.TemporaryDirectory() as tmp:
   parent=Path(tmp);source=self.make_source(parent);original=tree.copy_input;changed=[]
   def copy(*args,**kwargs):
    result=original(*args,**kwargs)
    if not changed:
     path=source/'data';info=path.stat();path.write_bytes(b'changed! payload');os.utime(path,ns=(info.st_atime_ns,info.st_mtime_ns));changed.append(True)
    return result
   with patch.object(tree,'copy_input',copy),self.assertRaises(ValueError):capture_tree(source,parent/'partial')
   self.assertTrue(changed);self.assertEqual((source/'data').read_bytes(),b'changed! payload')

 def test_source_membership_mutation_after_copy_is_rejected(self):
  with tempfile.TemporaryDirectory() as tmp:
   parent=Path(tmp);source=self.make_source(parent);original=tree.copy_input;changed=[]
   def copy(*args,**kwargs):
    result=original(*args,**kwargs)
    if not changed:(source/'added').write_bytes(b'external');changed.append(True)
    return result
   with patch.object(tree,'copy_input',copy),self.assertRaises(ValueError):capture_tree(source,parent/'partial')
   self.assertEqual((source/'added').read_bytes(),b'external')

 def test_source_target_overlap_is_rejected_without_writes(self):
  with tempfile.TemporaryDirectory() as tmp:
   parent=Path(tmp);source=self.make_source(parent);before=inspect_tree(source)
   for target in (source,source/'inside',parent):
    with self.assertRaises(ValueError):capture_tree(source,target)
   self.assertEqual(inspect_tree(source),before)

 def test_case_collision_and_symbolic_link_rejected(self):
  with tempfile.TemporaryDirectory() as tmp:
   parent=Path(tmp);source=parent/'source';source.mkdir();(source/'A').write_bytes(b'a')
   if os.name!='nt':
    (source/'a').write_bytes(b'b')
    with self.assertRaises(ValueError):inspect_tree(source)
    (source/'a').unlink()
   external=parent/'external';external.write_bytes(b'outside');link=source/'link'
   try:link.symlink_to(external)
   except OSError as error:self.skipTest('Symbolic links unavailable: '+str(error))
   with self.assertRaises(ValueError):capture_tree(source,parent/'target')
   self.assertEqual(external.read_bytes(),b'outside');self.assertFalse((parent/'target').exists())

 def test_hardlinks_capture_internal_aliases_isolated_from_original(self):
  with tempfile.TemporaryDirectory() as tmp:
   parent=Path(tmp);source=parent/'source';source.mkdir();a=source/'a';a.write_bytes(b'shared');b=source/'b'
   try:b.hardlink_to(a)
   except OSError as error:self.skipTest('Hardlinks unavailable: '+str(error))
   manifest=capture_tree(source,parent/'target');self.assertEqual(manifest.total_bytes,12)
   (parent/'target/a').write_bytes(b'changed')
   self.assertEqual(a.read_bytes(),b'shared');self.assertEqual(b.read_bytes(),b'shared');self.assertEqual((parent/'target/b').read_bytes(),b'changed')
   self.assertTrue((parent/'target/a').samefile(parent/'target/b'));self.assertFalse(a.samefile(parent/'target/a'));self.assertTrue(manifest.has_hardlinks)

 def test_path_descriptor_ctime_difference_keeps_each_api_change_detection(self):
  from types import SimpleNamespace
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp)/'source';root.mkdir();(root/'data').write_bytes(b'bytes');expected=inspect_tree(root);original=os.fstat
   names=('st_dev','st_ino','st_mode','st_size','st_mtime_ns','st_ctime_ns')
   def different(fd):
    info=original(fd);values={n:getattr(info,n) for n in names};values['st_ctime_ns']+=123
    return SimpleNamespace(**values)
   with patch.object(tree.os,'fstat',different):self.assertEqual(inspect_tree(root),expected)
   calls=[]
   def changing(fd):
    info=different(fd);calls.append(1);info.st_ctime_ns+=len(calls);return info
   with patch.object(tree.os,'fstat',changing),self.assertRaisesRegex(ValueError,'changed during reading'):inspect_tree(root)

 @unittest.skipIf(os.name=='nt','POSIX special resource test')
 def test_fifo_rejected_without_opening(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp)/'source';root.mkdir();os.mkfifo(root/'fifo')
   with self.assertRaises(ValueError):inspect_tree(root)

if __name__=='__main__':unittest.main()
