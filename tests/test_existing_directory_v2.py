from pathlib import Path
import json,os,subprocess,sys,tempfile,unittest
from unittest.mock import patch
import easysewer
from easysewer.runtime import _workspace as w,recovery as recovery
from easysewer.runtime import inspect_run_recovery,recover_run,retire_run_recovery
from easysewer.runtime._directory_tree import inspect_tree,DirectoryLimits
from easysewer.runtime._publication_directory import state


def tree(root,name,content):
 p=root/name;p.mkdir();(p/'nested').mkdir();(p/'empty').mkdir();(p/'nested/data').write_bytes(content);(p/'alias').hardlink_to(p/'nested/data');return p

CHILD=r"""
from pathlib import Path
import os,sys
sys.path[:0]=sys.argv[1:3]
from test_existing_directory_v2 import tree
from easysewer.runtime import _workspace as w,recovery as r
from easysewer.runtime._directory_tree import inspect_tree
root=Path(sys.argv[3]);stage=sys.argv[4]
target=tree(root,'target',b'old');source=tree(root,'source',b'new');(target/'old-only').write_bytes(b'old-only')
t=w.OutputTransaction('existing-directory-crash',overwrite=True);t.reserve(((target,True),))
if stage=='reserved':os._exit(48)
replace=os.replace;sync=r._Journal.sync;remove=w.remove_owned_tree

def replacing(src,dst):
 value=replace(src,dst)
 if stage=='backup' and Path(src)==target:os._exit(48)
 if stage=='published' and Path(dst)==target:os._exit(48)
 return value

def syncing(journal,transaction,**kw):
 value=sync(journal,transaction,**kw)
 if stage=='prepared' and kw.get('phase')=='publishing':os._exit(48)
 if stage=='committed' and kw.get('phase')=='committed':os._exit(48)
 if stage=='cleanup-authorized' and any(t.directory_cleanup for t in transaction.targets):os._exit(48)
 return value

def removing(path,**kw):
 if stage=='partial-cleanup' and Path(path).name.startswith('.easysewer-backup-'):
  (Path(path)/'alias').unlink();os._exit(48)
 return remove(path,**kw)
os.replace=replacing;r._Journal.sync=syncing;w.remove_owned_tree=removing
t.publish({target:source},expected_trees={source:inspect_tree(source)})
raise AssertionError('Expected exit')
"""

class ExistingDirectoryTests(unittest.TestCase):
 def child(self,root,stage):
  result=subprocess.run([sys.executable,'-I','-B','-c',CHILD,str(Path(easysewer.__file__).parent.parent),str(Path(__file__).parent),str(root),stage],capture_output=True,text=True,timeout=45);self.assertEqual(result.returncode,48,result.stdout+result.stderr);journal,=root.glob('.easysewer-recovery-*.json');return journal
 def test_original_content_membership_and_same_stamp_changes_are_not_overwritten(self):
  from test_output_ownership_v2 import fixed_stamps
  for change in ('bytes','empty','hardlink'):
   with self.subTest(change=change),tempfile.TemporaryDirectory() as tmp:
    root=Path(tmp).resolve();target=tree(root,'target',b'old');source=tree(root,'source',b'new');stamp=w.fingerprint(target);t=w.OutputTransaction('change',overwrite=True)
    try:
     with fixed_stamps({target:stamp}):
      t.reserve(((target,True),))
      if change=='bytes':(target/'nested/data').write_bytes(b'EXT')
      elif change=='empty':(target/'extra-empty').mkdir()
      else:(target/'alias').unlink();(target/'alias').write_bytes(b'old')
      changed=state(target)
      with self.assertRaises((ValueError,RuntimeError)):t.publish({target:source})
      self.assertEqual(state(target),changed)
    finally:t.close()
    self.assertFalse(t.committed);self.assertEqual((source/'nested/data').read_bytes(),b'new')
 def test_reservation_budget_and_cancel_preserve_existing_tree(self):
  for mode in ('bytes','entries','cancel'):
   with self.subTest(mode=mode),tempfile.TemporaryDirectory() as tmp:
    root=Path(tmp).resolve();target=tree(root,'target',b'old');before=state(target);limits=DirectoryLimits(total_bytes=1) if mode=='bytes' else DirectoryLimits(entries=1) if mode=='entries' else DirectoryLimits();t=w.OutputTransaction('budget',overwrite=True,directory_limits=limits);calls=[];error=OSError('cancel original capture');error.__cause__=RuntimeError('cause')
    def check():
     if t.targets:
      calls.append(True)
      if len(calls)==3:raise error
    try:
     with self.assertRaises((ValueError,OSError)) as caught:t.reserve(((target,True),),checkpoint=check if mode=='cancel' else lambda:None)
     if mode=='cancel':self.assertIs(caught.exception,error);self.assertIsInstance(caught.exception.__cause__,RuntimeError)
    finally:t.close()
    self.assertEqual(state(target),before);self.assertFalse(list(root.glob('.easysewer-*')))
 def test_real_process_exits_restore_or_keep_committed_complete_tree(self):
  for stage in ('reserved','prepared','backup','published','committed','cleanup-authorized','partial-cleanup'):
   with self.subTest(stage=stage),tempfile.TemporaryDirectory() as tmp:
    root=Path(tmp).resolve();journal=self.child(root,stage);self.assertEqual(json.loads(journal.read_bytes())['version'],7);self.assertEqual(inspect_run_recovery(journal).state,'interrupted');result=recover_run(journal);self.assertEqual(result.state,'recovered',result.issues);committed=stage in ('committed','cleanup-authorized','partial-cleanup');target=root/'target';self.assertEqual((target/'nested/data').read_bytes(),b'new' if committed else b'old');self.assertTrue((target/'alias').samefile(target/'nested/data'));self.assertTrue((target/'empty').is_dir());self.assertEqual((target/'old-only').exists(),not committed);self.assertFalse(list(root.glob('.easysewer-*')))
 def test_partial_backup_cleanup_rejects_added_or_changed_surviving_content(self):
  for change in ('bytes','extra','root'):
   with self.subTest(change=change),tempfile.TemporaryDirectory() as tmp:
    root=Path(tmp).resolve();journal=self.child(root,'partial-cleanup');data=json.loads(journal.read_bytes());record=data['targets'][0];backup=Path(record['backup']);target_before=state(root/'target')
    if change=='bytes':(backup/'nested/data').write_bytes(b'EXT')
    elif change=='extra':(backup/'external').write_bytes(b'keep')
    else:backup.rename(root/'retained-backup');backup.mkdir();(backup/'external').write_bytes(b'keep')
    before=state(backup);result=recover_run(journal);self.assertEqual(result.state,'conflicted');self.assertEqual(state(backup),before);self.assertEqual(state(root/'target'),target_before);self.assertTrue(Path(record['lock']).exists())
    if change=='bytes':(backup/'nested/data').write_bytes(b'old')
    elif change=='extra':(backup/'external').unlink()
    else:(backup/'external').unlink();backup.rmdir();(root/'retained-backup').rename(backup)
    self.assertEqual(recover_run(journal).state,'recovered');self.assertEqual((root/'target/nested/data').read_bytes(),b'new')
 def test_partial_normal_cleanup_is_durable_and_retryable(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp).resolve();target=tree(root,'target',b'old');source=tree(root,'source',b'new');t=w.OutputTransaction('partial',overwrite=True);t.reserve(((target,True),));remove=w.remove_owned_tree
   def fail(path,**kw):
    if Path(path).name.startswith('.easysewer-backup-'):(Path(path)/'alias').unlink();raise OSError('partial backup cleanup')
    return remove(path,**kw)
   with patch.object(w,'remove_owned_tree',fail):t.publish({target:source})
   self.assertTrue(t.committed);self.assertTrue(t.issues);journal=t.journal.path;t.close();self.assertTrue(journal.exists());self.assertEqual(recover_run(journal).state,'recovered');self.assertEqual((target/'nested/data').read_bytes(),b'new')
 def test_rollback_moves_new_tree_aside_before_partial_cleanup(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp).resolve();target=tree(root,'target',b'old');source=tree(root,'source',b'new');original=state(target);t=w.OutputTransaction('rollback',overwrite=True);t.reserve(((target,True),));verify=t._verify_published;remove=w.remove_owned_tree;calls=[]
   def reject(item,**kw):
    result=verify(item,**kw);calls.append(True)
    if len(calls)==1:raise OSError('fail before commit')
    return result
   def fail(path,**kw):
    if Path(path).name.startswith('.easysewer-publish-') and (Path(path)/'alias').exists():(Path(path)/'alias').unlink();raise OSError('partial temporary cleanup')
    return remove(path,**kw)
   with patch.object(t,'_verify_published',reject),patch.object(w,'remove_owned_tree',fail),self.assertRaisesRegex(OSError,'fail before commit'):t.publish({target:source})
   self.assertFalse(t.committed);self.assertEqual(state(target),original);journal=t.journal.path
   with patch.object(w,'remove_owned_tree',side_effect=OSError('still held')):t.close()
   self.assertEqual(recover_run(journal).state,'recovered');self.assertEqual(state(target),original)
 def test_failed_cleanup_authorization_flush_never_permits_later_memory_only_deletion(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp).resolve();target=tree(root,'target',b'old');source=tree(root,'source',b'new');original=state(target);t=w.OutputTransaction('flush',overwrite=True);t.reserve(((target,True),));verify=t._verify_published;sync=t.journal.sync;calls=[]
   def reject(item,**kw):
    result=verify(item,**kw);calls.append(True)
    if len(calls)==1:raise OSError('publication refused')
    return result
   def fail(transaction,**kw):
    if any(x.directory_cleanup for x in transaction.targets):raise OSError('cleanup authorization not flushed')
    return sync(transaction,**kw)
   try:
    with patch.object(t,'_verify_published',reject),patch.object(t.journal,'sync',fail):
     with self.assertRaisesRegex(OSError,'publication refused'):t.publish({target:source})
     temporary=t.targets[0].temporary;before=state(temporary);self.assertFalse(t.targets[0].directory_cleanup);t._clean_temporaries();self.assertEqual(state(temporary),before);self.assertFalse(t.targets[0].directory_cleanup);self.assertEqual(state(target),original)
   finally:t.close()
   self.assertFalse(temporary.exists());self.assertEqual(recover_run(t.journal.path).state,'recovered');self.assertEqual(state(target),original)
 def test_retirement_uses_new_version_and_retains_directory_backup(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp).resolve();journal=self.child(root,'published');data=json.loads(journal.read_bytes());backup=Path(data['targets'][0]['backup']);before=state(backup);target=state(root/'target');result=retire_run_recovery(journal,reason='retain reviewed directory evidence');self.assertEqual(result.state,'retired');self.assertEqual(json.loads(journal.read_bytes())['version'],8);self.assertEqual(state(backup),before);self.assertEqual(state(root/'target'),target);self.assertEqual(recover_run(journal).state,'retired');self.assertEqual(state(backup),before)
if __name__=='__main__':unittest.main()
