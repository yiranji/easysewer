"""Directory publication uses full tree evidence and protects input membership."""
import json,os,subprocess,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import easysewer
from easysewer.runtime import _workspace as storage,inspect_run_recovery,recover_run
from easysewer.runtime._directory_tree import DirectoryLimits,inspect_tree
from test_output_ownership_v2 import fixed_stamps
from test_run_recovery_v2 import CHILD

class DirectoryPublicationTests(unittest.TestCase):
 def tree(self,root,name='source'):
  p=root/name;p.mkdir();(p/'empty').mkdir();(p/'data').write_bytes(b'original');return p
 def test_protected_tree_containment_and_reverse_missing_member_reject_before_locks(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);source=self.tree(root)
   cases=[dict(target=source/'new',directory=False,protected=(source,)),dict(target=root/'missing',directory=True,protected=(root/'missing/data',)),dict(target=root/'inactive/new',directory=False,protected_directories=(root/'inactive',))]
   for case in cases:
    target=case.pop('target');directory=case.pop('directory');t=storage.OutputTransaction('overlap',overwrite=True,**case)
    try:
     with self.assertRaisesRegex(ValueError,'aliases an input'):t.reserve(((target,directory),))
    finally:t.close()
    self.assertIsNone(t.journal);self.assertFalse(target.exists())
   self.assertEqual((source/'data').read_bytes(),b'original')
 def test_hardlink_to_input_member_is_rejected_before_overwrite(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);source=self.tree(root);alias=root/'alias';alias.hardlink_to(source/'data');t=storage.OutputTransaction('hardlink',overwrite=True,protected=(source,))
   try:
    with self.assertRaisesRegex(ValueError,'input resource member'):t.reserve(((alias,False),))
   finally:t.close()
   self.assertEqual(alias.read_bytes(),b'original');self.assertEqual((source/'data').read_bytes(),b'original');self.assertIsNone(t.journal)
 def test_new_input_member_alias_after_reservation_is_rechecked(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);source=self.tree(root);target=root/'output';target.write_bytes(b'old');new=root/'new';new.write_bytes(b'new');stamp=storage.fingerprint(target);t=storage.OutputTransaction('late-alias',overwrite=True,protected=(source,))
   try:
    with fixed_stamps({target:stamp}):
     t.reserve(((target,False),));(source/'new-alias').hardlink_to(target)
     with self.assertRaisesRegex(ValueError,'input resource member'):t.publish({target:new})
   finally:t.close()
   self.assertEqual(target.read_bytes(),b'old');self.assertEqual((source/'new-alias').read_bytes(),b'old');self.assertFalse(t.committed)
 def test_alias_budget_and_cancel_refuse_without_modifying_outputs(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);source=self.tree(root);target=root/'output';target.write_bytes(b'old')
   t=storage.OutputTransaction('budget',overwrite=True,protected=(source,),directory_limits=DirectoryLimits(entries=1))
   try:
    with self.assertRaisesRegex(ValueError,'budget'):t.reserve(((target,False),))
   finally:t.close()
   from easysewer.runtime import _directory_tree as trees
   original=trees.tree_file_identities;inside=[];calls=[];error=OSError('cancel identity scan');error.__cause__=ValueError('cause')
   def observing(*a,**k):
    inside.append(True)
    try:return original(*a,**k)
    finally:inside.pop()
   def stop():
    if inside:
     calls.append(True)
     if len(calls)==2:raise error
   t=storage.OutputTransaction('cancel',overwrite=True,protected=(source,))
   try:
    with patch.object(trees,'tree_file_identities',observing),self.assertRaises(OSError) as caught:t.reserve(((target,False),),checkpoint=stop)
    self.assertIs(caught.exception,error);self.assertIsInstance(caught.exception.__cause__,ValueError)
   finally:t.close()
   self.assertEqual(calls,[True,True]);self.assertEqual(target.read_bytes(),b'old')
 def test_preobserved_tree_rejects_added_or_removed_empty_directory(self):
  for change in ('add','remove'):
   with self.subTest(change=change),tempfile.TemporaryDirectory() as tmp:
    root=Path(tmp);source=self.tree(root);manifest=inspect_tree(source);files={source/'data':storage.digest_file(source/'data')};target=root/'published'
    if change=='add':(source/'extra').mkdir()
    else:(source/'empty').rmdir()
    t=storage.OutputTransaction('membership',overwrite=False)
    try:
     t.reserve(((target,True),))
     with self.assertRaises(ValueError):t.publish({target:source},expected_sources=files,expected_trees={source:manifest})
    finally:t.close()
    self.assertFalse(target.exists());self.assertFalse(t.committed);self.assertEqual((source/'data').read_bytes(),b'original')
 def test_complete_tree_publication_and_exact_evidence_coverage(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);source=self.tree(root);manifest=inspect_tree(source);target=root/'published';t=storage.OutputTransaction('success',overwrite=False)
   try:
    t.reserve(((target,True),))
    for evidence in ({},{root/'wrong':manifest}):
     with self.assertRaisesRegex(ValueError,'cover exactly'):t.publish({target:source},expected_trees=evidence)
    with self.assertRaises(TypeError):t.publish({target:source},expected_trees={source:None})
    t.publish({target:source},expected_trees={source:manifest})
   finally:t.close()
   self.assertTrue(t.committed);self.assertEqual(inspect_tree(target),manifest);self.assertEqual(inspect_tree(source),manifest);self.assertFalse(list(root.glob('.easysewer-*')))
 def test_source_membership_change_during_copy_is_detected_and_preserved(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);source=self.tree(root);manifest=inspect_tree(source);target=root/'published';original=storage.copy_input;changed=[]
   def copying(*a,**k):
    result=original(*a,**k)
    if not changed:changed.append(True);(source/'new-empty').mkdir()
    return result
   t=storage.OutputTransaction('copy-change',overwrite=False)
   try:
    t.reserve(((target,True),))
    with patch.object(storage,'copy_input',copying),self.assertRaises(ValueError):t.publish({target:source},expected_trees={source:manifest})
   finally:t.close()
   self.assertEqual(changed,[True]);self.assertFalse(target.exists());self.assertTrue((source/'new-empty').is_dir());self.assertEqual((source/'data').read_bytes(),b'original')
 def test_existing_output_directory_requires_explicit_overwrite_and_replaces_whole_tree(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp).resolve();target=self.tree(root,'existing');manifest=inspect_tree(target);source=self.tree(root,'replacement');(source/'data').write_bytes(b'new');(target/'old-only').write_bytes(b'old');t=storage.OutputTransaction('existing',overwrite=False)
   try:
    with self.assertRaises(FileExistsError):t.reserve(((target,True),))
   finally:t.close()
   self.assertEqual((target/'old-only').read_bytes(),b'old');t=storage.OutputTransaction('existing',overwrite=True)
   try:t.reserve(((target,True),));t.publish({target:source},expected_trees={source:inspect_tree(source)})
   finally:t.close()
   self.assertTrue(t.committed);self.assertFalse(t.issues);self.assertEqual(inspect_tree(target),inspect_tree(source));self.assertFalse((target/'old-only').exists());self.assertFalse(list(root.glob('.easysewer-*')))
 def test_actual_crash_recovery_keeps_verified_tree_or_rolls_back(self):
  child=CHILD.replace("t.publish({a:source,b:source,tree:assets})","from easysewer.runtime._directory_tree import inspect_tree\nt.publish({a:source,b:source,tree:assets},expected_trees={assets:inspect_tree(assets)})")
  self.assertNotEqual(child,CHILD)
  for stage in ('prepared','all_published','committed'):
   with self.subTest(stage=stage),tempfile.TemporaryDirectory() as tmp:
    root=Path(tmp).resolve();p=subprocess.run([sys.executable,'-I','-B','-c',child,str(Path(easysewer.__file__).parent.parent),str(root),stage],capture_output=True,text=True,timeout=45);self.assertEqual(p.returncode,37,p.stdout+p.stderr)
    journal,=root.glob('.easysewer-recovery-*.json');self.assertEqual(inspect_run_recovery(journal).state,'interrupted')
    result=recover_run(journal);self.assertEqual(result.state,'recovered',result.issues);committed=stage=='committed'
    self.assertEqual((root/'a').read_bytes(),b'new' if committed else b'old-a');self.assertEqual((root/'b').read_bytes(),b'new' if committed else b'old-b');self.assertEqual((root/'z').exists(),committed)
    if committed:self.assertTrue((root/'z/empty').is_dir());self.assertEqual((root/'z/data').read_bytes(),b'tree-data')
    self.assertFalse(list(root.glob('.easysewer-lock-*')));self.assertFalse(journal.exists());self.assertEqual((root/'source-tree/data').read_bytes(),b'tree-data')

if __name__=='__main__':unittest.main()
