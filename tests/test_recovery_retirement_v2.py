"""Explicit retain decisions, durable release intents and interrupted retirement."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import easysewer
from easysewer.runtime import discover_run_recovery, inspect_run_recovery, recover_run, retire_run_recovery
from easysewer.runtime import recovery as r
from easysewer.runtime import _workspace as w
from test_run_recovery_v2 import CHILD


RETIRE_CHILD = r'''
import os,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from easysewer.runtime import recovery as r
mode=sys.argv[3];write=r._write;unlink=Path.unlink
def writing(path,data):
 result=write(path,data);retirement=data.get('retirement')
 if retirement is not None:
  rows=retirement['reservations']
  if mode=='pause' and not rows:
   path.with_suffix('.paused').write_bytes(b'ready');sys.stdin.buffer.read(1)
  if mode=='decision' and not rows:os._exit(71)
  if mode=='intent' and any(row['state']=='pending' for row in rows):os._exit(72)
  if mode=='record' and any(row['state']=='released' for row in rows):os._exit(74)
  if mode=='complete' and retirement['complete']:os._exit(75)
 return result
def unlinking(path,*a,**kw):
 result=unlink(path,*a,**kw)
 if mode=='unlink' and path.name.startswith('.easysewer-lock-'):os._exit(73)
 return result
r._write=writing;Path.unlink=unlinking
r.retire_run_recovery(sys.argv[2],reason='retain current artifacts')
if mode=='pause':os._exit(76)
raise AssertionError('Expected process exit')
'''


class RecoveryRetirementTests(unittest.TestCase):
    def child(self, root, stage='published'):
        process=subprocess.run([sys.executable,'-I','-B','-c',CHILD,
            str(Path(easysewer.__file__).resolve().parent.parent),str(root),stage],
            capture_output=True,timeout=30)
        self.assertEqual(process.returncode,37,process.stderr)
        journal,=root.glob('.easysewer-recovery-*.json')
        return journal

    def preserved(self, root):
        return {str(p.relative_to(root)):(r._identity(p),None if p.is_dir() else p.read_bytes())
            for p in root.rglob('*')
            if not p.name.startswith(('.easysewer-recovery-','.easysewer-lock-'))}

    def transaction(self, root, *, extra=()):
        (root/'output').write_bytes(b'old')
        t=w.OutputTransaction('retire',overwrite=True)
        t.reserve(((root/'output',False),*extra))
        return t

    def test_retirement_preserves_outputs_backups_partial_copies_and_workspaces(self):
        for stage in ('published','copying','committed','prepared','backup'):
            with self.subTest(stage=stage),tempfile.TemporaryDirectory() as directory:
                root=Path(directory).resolve();journal=self.child(root,stage)
                (root/'a').write_bytes(b'external decision')
                for p in root.glob('.easysewer-backup-*'):p.write_bytes(b'edited backup retained')
                data=json.loads(journal.read_bytes());data['failure']=['publish','OSError','original failure']
                data['issues']=['original cleanup issue'];r._write(journal,data)
                before=self.preserved(root)
                result=retire_run_recovery(journal,reason='retain current artifacts')
                self.assertEqual(result.state,'retired');self.assertEqual(result.retirement_reason,'retain current artifacts')
                self.assertEqual(result.failure,('publish','OSError','original failure'))
                self.assertIn('original cleanup issue',result.issues)
                self.assertEqual(self.preserved(root),before)
                self.assertFalse(list(root.glob('.easysewer-lock-*')))
                self.assertTrue(journal.exists());self.assertTrue(journal.with_suffix('.lease').exists())
                self.assertEqual(json.loads(journal.read_bytes())['version'],6)
                self.assertTrue(result.retained_artifacts)
                receipt=journal.read_bytes()
                self.assertEqual(inspect_run_recovery(journal).state,'retired')
                self.assertEqual(discover_run_recovery(root)[0].state,'retired')
                self.assertEqual(recover_run(journal).state,'retired')
                self.assertEqual(journal.read_bytes(),receipt);self.assertEqual(self.preserved(root),before)

    def test_existing_recovery_conflict_can_be_retired_without_restoring_external_edits(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();journal=self.child(root)
            (root/'a').write_bytes(b'external')
            self.assertEqual(recover_run(journal).state,'conflicted')
            before=self.preserved(root)
            self.assertEqual(retire_run_recovery(journal,reason='keep external edit').state,'retired')
            self.assertEqual(self.preserved(root),before)
            t=w.OutputTransaction('new-run',overwrite=True)
            try:t.reserve(((root/'a',False),));self.assertEqual((root/'a').read_bytes(),b'external')
            finally:t.close()

    def test_active_owner_refuses_retirement_without_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();t=self.transaction(root);before=t.journal.path.read_bytes()
            try:
                with self.assertRaises(OSError):retire_run_recovery(t.journal.path,reason='keep')
                self.assertEqual(t.journal.path.read_bytes(),before)
                self.assertTrue(t.targets[0].lock.exists())
            finally:t.close()

    def test_live_retirement_excludes_competing_recovery_and_retirement(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();journal=self.child(root);before=self.preserved(root)
            p=subprocess.Popen([sys.executable,'-I','-B','-c',RETIRE_CHILD,
                str(Path(easysewer.__file__).resolve().parent.parent),str(journal),'pause'],
                stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
            try:
                deadline=time.monotonic()+15
                while not journal.with_suffix('.paused').exists() and p.poll() is None and time.monotonic()<deadline:
                    time.sleep(.01)
                self.assertTrue(journal.with_suffix('.paused').exists());self.assertIsNone(p.poll())
                self.assertEqual(inspect_run_recovery(journal).state,'active')
                self.assertEqual(discover_run_recovery(root)[0].state,'busy-or-inaccessible')
                with self.assertRaises(OSError):recover_run(journal)
                with self.assertRaises(OSError):retire_run_recovery(journal,reason='retain current artifacts')
                self.assertEqual(self.preserved(root),before)
                _,errors=p.communicate(b'x',timeout=30);self.assertEqual(p.returncode,76,errors)
                self.assertEqual(recover_run(journal).state,'retired');self.assertEqual(self.preserved(root),before)
            finally:
                if p.poll() is None:p.kill()
                p.communicate(timeout=10)

    def test_reason_and_read_budget_are_validated_before_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();t=self.transaction(root);t.journal.lease.close()
            before=t.journal.path.read_bytes()
            for reason in ('','  ',None,1):
                with self.assertRaises((ValueError,TypeError)):retire_run_recovery(t.journal.path,reason=reason)
                self.assertEqual(t.journal.path.read_bytes(),before)
            with self.assertRaises(ValueError):retire_run_recovery(t.journal.path,reason='keep',max_bytes=1)
            self.assertEqual(t.journal.path.read_bytes(),before)
            retire_run_recovery(t.journal.path,reason='keep');receipt=t.journal.path.read_bytes()
            with self.assertRaises(ValueError):retire_run_recovery(t.journal.path,reason='different decision')
            self.assertEqual(t.journal.path.read_bytes(),receipt)

    def test_decision_write_failure_cannot_release_any_reservation(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();t=self.transaction(root);t.journal.lease.close()
            before=t.journal.path.read_bytes()
            with patch.object(r,'_write',side_effect=OSError('full disk')):
                with self.assertRaises(OSError):retire_run_recovery(t.journal.path,reason='keep')
            self.assertEqual(t.journal.path.read_bytes(),before);self.assertTrue(t.targets[0].lock.exists())

    def test_release_failure_remains_retryable_without_rollback(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();journal=self.child(root);before=self.preserved(root)
            unlink=Path.unlink
            def failing(path,*a,**kw):
                if path.name.startswith('.easysewer-lock-'):raise PermissionError('temporarily held')
                return unlink(path,*a,**kw)
            with patch.object(Path,'unlink',failing):result=retire_run_recovery(journal,reason='keep')
            self.assertEqual(result.state,'retiring');self.assertTrue(result.issues)
            self.assertEqual(inspect_run_recovery(journal).state,'retiring')
            self.assertEqual(discover_run_recovery(root)[0].state,'retiring')
            self.assertEqual(recover_run(journal).state,'retired')
            self.assertEqual(self.preserved(root),before)

    def test_foreign_reservations_are_retained_and_reported_in_terminal_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();t=self.transaction(root);t.journal.lease.close()
            lock=t.targets[0].lock;lock.write_bytes(b'foreign reservation')
            result=retire_run_recovery(t.journal.path,reason='preserve conflict')
            self.assertEqual(result.state,'retired');self.assertEqual(result.retained_reservations,(str(lock),))
            self.assertTrue(result.issues);self.assertEqual(lock.read_bytes(),b'foreign reservation')
            self.assertEqual(discover_run_recovery(root)[0].issues,result.issues)
            self.assertEqual(recover_run(t.journal.path).retained_reservations,(str(lock),))

    def test_replacement_after_release_intent_is_never_removed(self):
        for legacy in (False,True):
            with self.subTest(legacy=legacy),tempfile.TemporaryDirectory() as directory:
                root=Path(directory).resolve();t=self.transaction(root);t.journal.lease.close();lock=t.targets[0].lock
                if legacy:
                    data=json.loads(t.journal.path.read_bytes());data['version']=1
                    for target in data['targets']:target.pop('reservation_source');target.pop('lock_content')
                    r._write(t.journal.path,data)
                original=r._write;changed=[]
                def writing(path,data):
                    value=original(path,data)
                    if any(row['state']=='pending' for row in data.get('retirement',{}).get('reservations',[])) and not changed:
                        payload=json.loads(lock.read_bytes());payload['external']='changed after intent'
                        lock.write_text(json.dumps(payload));changed.append(lock.read_bytes())
                    return value
                with patch.object(r,'_write',writing):result=retire_run_recovery(t.journal.path,reason='keep')
                self.assertEqual(result.state,'retired');self.assertEqual(result.retained_reservations,(str(lock),))
                self.assertEqual(lock.read_bytes(),changed[0])

    def test_retired_receipt_never_touches_new_run_reservation(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();t=self.transaction(root);t.journal.lease.close()
            result=retire_run_recovery(t.journal.path,reason='keep');receipt=t.journal.path.read_bytes()
            later=w.OutputTransaction('later',overwrite=True)
            try:
                later.reserve(((root/'output',False),));lock=later.targets[0].lock;raw=lock.read_bytes()
                self.assertEqual(recover_run(t.journal.path),result)
                self.assertEqual(lock.read_bytes(),raw);self.assertEqual(t.journal.path.read_bytes(),receipt)
            finally:later.close()

    def test_real_hard_exit_at_every_retirement_boundary_resumes_only_retirement(self):
        for mode,code in (('decision',71),('intent',72),('unlink',73),('record',74),('complete',75)):
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as directory:
                root=Path(directory).resolve();journal=self.child(root);before=self.preserved(root)
                p=subprocess.run([sys.executable,'-I','-B','-c',RETIRE_CHILD,
                    str(Path(easysewer.__file__).resolve().parent.parent),str(journal),mode],capture_output=True,timeout=30)
                self.assertEqual(p.returncode,code,p.stderr)
                self.assertEqual(json.loads(journal.read_bytes())['version'],6)
                self.assertEqual(self.preserved(root),before)
                self.assertEqual(recover_run(journal).state,'retired')
                self.assertEqual(self.preserved(root),before)
                self.assertFalse(list(root.glob('.easysewer-lock-*')))

    def test_new_reservation_after_unrecorded_unlink_is_retained(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();journal=self.child(root);before=self.preserved(root)
            p=subprocess.run([sys.executable,'-I','-B','-c',RETIRE_CHILD,
                str(Path(easysewer.__file__).resolve().parent.parent),str(journal),'unlink'],capture_output=True,timeout=30)
            self.assertEqual(p.returncode,73,p.stderr)
            later=w.OutputTransaction('later',overwrite=True)
            try:
                later.reserve(((root/'a',False),));lock=later.targets[0].lock;raw=lock.read_bytes()
                result=recover_run(journal)
                self.assertEqual(result.state,'retired');self.assertIn(str(lock),result.retained_reservations)
                self.assertEqual(lock.read_bytes(),raw);self.assertEqual(self.preserved(root),before)
            finally:later.close()

    def test_legacy_journals_upgrade_only_for_explicit_retirement(self):
        for version in (1,2,3,4,5):
            with self.subTest(version=version),tempfile.TemporaryDirectory() as directory:
                root=Path(directory).resolve();journal=self.child(root);data=json.loads(journal.read_bytes())
                data['version']=version
                if version<2:
                    for t in data['targets']:t.pop('reservation_source');t.pop('lock_content')
                for item in data['workspaces']:
                    if version<5:item.pop('admission')
                    if version<4:
                        for key in ('cleanup_on_crash','execution','worker'):item.pop(key)
                    if version<3:
                        for key in ('cleanup_requested','cleanup_content','cleaned'):item.pop(key)
                r._write(journal,data);before=self.preserved(root);raw=journal.read_bytes()
                self.assertEqual(inspect_run_recovery(journal).state,'interrupted');self.assertEqual(journal.read_bytes(),raw)
                self.assertEqual(retire_run_recovery(journal,reason='keep legacy artifacts').state,'retired')
                self.assertEqual(json.loads(journal.read_bytes())['version'],6)
                self.assertEqual(self.preserved(root),before)

    def test_changed_parent_is_preserved_and_retirement_receipt_remains_readable(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();nested=root/'z-nested';nested.mkdir();(nested/'output').write_bytes(b'old nested')
            t=self.transaction(root,extra=((nested/'output',False),));t.journal.lease.close()
            saved=root/'saved'
            self.assertTrue(nested.resolve().is_relative_to(root));self.assertTrue(saved.resolve().is_relative_to(root))
            nested.rename(saved);nested.mkdir();(nested/'output').write_bytes(b'foreign directory')
            foreign=nested/t.targets[1].lock.name;foreign.write_bytes(b'foreign reservation')
            before=self.preserved(root)
            result=retire_run_recovery(t.journal.path,reason='keep changed parent')
            self.assertEqual(result.state,'retired');self.assertIn(str(foreign),result.retained_reservations)
            self.assertEqual(foreign.read_bytes(),b'foreign reservation');self.assertEqual(self.preserved(root),before)
            self.assertEqual(inspect_run_recovery(t.journal.path).state,'retired')

    def test_active_execution_admission_and_workspace_are_preserved(self):
        from easysewer.runtime import _admission
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();t=self.transaction(root);workspace=w.Workspace(root,'retained-worker')
            t.track_workspace(workspace,cleanup_on_crash=True);record=t.prepare_execution(workspace)
            (workspace.root/'keep').write_bytes(b'worker evidence')
            guard=_admission.enter(record);t.journal.lease.close()
            try:
                with patch.object(_admission,'revoke',side_effect=AssertionError('must not revoke retained execution')):
                    result=retire_run_recovery(t.journal.path,reason='retain uncertain workspace')
                self.assertEqual(result.state,'retired');self.assertIn(str(workspace.root),result.retained_artifacts)
                self.assertEqual((workspace.root/'keep').read_bytes(),b'worker evidence')
                self.assertEqual(_admission._read(guard,record),b'A')
            finally:guard.close()

    def test_malformed_retirement_record_is_rejected_without_effects(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();journal=self.child(root)
            with patch.object(r,'_retire_locked',return_value=None):retire_run_recovery(journal,reason='keep')
            original=json.loads(journal.read_bytes());before=self.preserved(root)
            for field,value in (('complete',True),('reason',''),('reservations',[{}]),('issues',[1])):
                data=json.loads(json.dumps(original));data['retirement'][field]=value;r._write(journal,data)
                raw=journal.read_bytes()
                with self.assertRaises(ValueError):recover_run(journal)
                self.assertEqual(journal.read_bytes(),raw);self.assertEqual(self.preserved(root),before)


if __name__=='__main__':unittest.main()
