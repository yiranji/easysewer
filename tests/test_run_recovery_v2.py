"""Actual process exits at output transaction boundaries and safe retry."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import easysewer
from easysewer.runtime import inspect_run_recovery, recover_run
from easysewer.runtime import _workspace as storage
from easysewer.runtime import recovery


CHILD = r'''
import os,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from easysewer.runtime import _workspace as w
from easysewer.runtime import recovery as r
root=Path(sys.argv[2]);stage=sys.argv[3]
a=root/'a';b=root/'b';tree=root/'z'
a.write_bytes(b'old-a');b.write_bytes(b'old-b')
source=root/'source';source.write_bytes(b'new')
assets=root/'source-tree';assets.mkdir();(assets/'empty').mkdir();(assets/'data').write_bytes(b'tree-data')
t=w.OutputTransaction('actual-crash',overwrite=True)
t.reserve(((a,False),(b,False),(tree,True)))
workspace=w.Workspace(root,'actual-crash');t.track_workspace(workspace)
(workspace.path/'evidence').write_bytes(b'private partial evidence')
if stage=='reserved':os._exit(37)
if stage=='active':
 print(t.journal.path,flush=True);sys.stdin.readline();os._exit(37)
replace=os.replace;sync=r._Journal.sync;copy=w.copy_input;unlink=Path.unlink
def replacing(src,dst):
 value=replace(src,dst)
 if stage=='backup' and Path(src)==a:os._exit(37)
 if stage=='published' and Path(dst)==a:os._exit(37)
 if stage=='all_published' and Path(dst)==tree:os._exit(37)
 return value
def syncing(journal,transaction,**kw):
 value=sync(journal,transaction,**kw)
 if stage=='prepared' and kw.get('phase')=='publishing':os._exit(37)
 if stage=='committed' and kw.get('phase')=='committed':os._exit(37)
 if stage=='closed' and kw.get('phase')=='committed' and journal.data['recovered']:os._exit(37)
 return value
def copying(src,dst,**kw):
 if stage=='copying' and Path(dst).name.startswith('.easysewer-publish-'):
  Path(dst).write_bytes(b'partial');os._exit(37)
 return copy(src,dst,**kw)
def unlinking(p,*a,**kw):
 value=unlink(p,*a,**kw)
 if stage=='cleanup' and p.name.startswith('.easysewer-backup-'):os._exit(37)
 return value
os.replace=replacing;r._Journal.sync=syncing;w.copy_input=copying;Path.unlink=unlinking
t.publish({a:source,b:source,tree:assets})
t.close()
raise AssertionError('Expected hard exit')
'''


class RunRecoveryTests(unittest.TestCase):
    def child(self, root, stage, *, active=False):
        args = [sys.executable, '-I', '-B', '-c', CHILD,
                str(Path(easysewer.__file__).resolve().parent.parent), str(root), stage]
        if active:
            return subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True)
        value = subprocess.run(args, capture_output=True, text=True, timeout=30)
        self.assertEqual(value.returncode, 37, value.stdout+value.stderr)
        journal, = root.glob('.easysewer-recovery-*.json')
        return journal

    def test_actual_hard_exits_rollback_or_complete_committed_cleanup(self):
        for stage in ('reserved', 'prepared', 'backup', 'published', 'all_published', 'committed', 'cleanup', 'closed'):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve(); journal = self.child(root, stage)
                observed = inspect_run_recovery(journal)
                self.assertEqual(observed.state, 'interrupted')
                result = recover_run(journal)
                self.assertEqual(result.state, 'recovered', result.issues)
                committed = stage in ('committed', 'cleanup', 'closed')
                self.assertEqual((root/'a').read_bytes(), b'new' if committed else b'old-a')
                self.assertEqual((root/'b').read_bytes(), b'new' if committed else b'old-b')
                self.assertEqual((root/'z').exists(), committed)
                if committed:
                    self.assertEqual((root/'z/data').read_bytes(), b'tree-data')
                    self.assertTrue((root/'z/empty').is_dir())
                self.assertFalse(list(root.glob('.easysewer-lock-*')))
                self.assertFalse(list(root.glob('.easysewer-backup-*')))
                self.assertFalse(list(root.glob('.easysewer-publish-*')))
                self.assertFalse(journal.exists())
                self.assertFalse(journal.with_suffix('.lease').exists())
                self.assertEqual(len(result.workspaces), 1)
                self.assertEqual((Path(result.workspaces[0])/'evidence').read_bytes(), b'private partial evidence')
                # The same destinations can be reserved again after recovery.
                t = storage.OutputTransaction('retry', overwrite=True)
                try:t.reserve(((root/'a', False), (root/'b', False)))
                finally:t.close()

    def test_active_owner_refused_then_os_termination_releases_lease(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve(); child = self.child(root, 'active', active=True)
            try:
                journal = Path(child.stdout.readline().strip())
                self.assertEqual(inspect_run_recovery(journal).state, 'active')
                before = journal.read_bytes()
                with self.assertRaises(OSError):recover_run(journal)
                self.assertEqual(journal.read_bytes(), before)
                child.terminate(); child.wait(timeout=15)
                self.assertEqual(recover_run(journal).state, 'recovered')
                self.assertEqual((root/'a').read_bytes(), b'old-a')
            finally:
                if child.poll() is None:child.kill();child.wait(timeout=15)
                child.stdin.close();child.stdout.close();child.stderr.close()

    def test_external_target_or_backup_changes_preserved_and_retryable(self):
        for modified in ('target', 'backup'):
            with self.subTest(modified=modified), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve(); journal = self.child(root, 'published')
                data = json.loads(journal.read_bytes()); a = next(t for t in data['targets'] if Path(t['path']).name == 'a')
                target = root/'a' if modified == 'target' else Path(a['backup'])
                original = target.read_bytes(); target.write_bytes(b'EXT')
                result = recover_run(journal)
                self.assertEqual(result.state, 'conflicted'); self.assertTrue(result.issues)
                self.assertEqual(target.read_bytes(), b'EXT'); self.assertTrue(Path(a['lock']).exists())
                self.assertTrue(Path(a['backup']).exists())
                target.write_bytes(original)
                result = recover_run(journal)
                self.assertEqual(result.state, 'recovered', result.issues)
                self.assertEqual((root/'a').read_bytes(), b'old-a')

    def test_incomplete_copy_retained_with_diagnostic_until_explicitly_resolved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve(); journal = self.child(root, 'copying')
            temporary, = root.glob('.easysewer-publish-*')
            value = recover_run(journal)
            self.assertEqual(value.state, 'conflicted')
            self.assertTrue(any('Incomplete or changed temporary' in s for s in value.issues))
            self.assertEqual(temporary.read_bytes(), b'partial')
            self.assertEqual((root/'a').read_bytes(), b'old-a')
            temporary.unlink()  # Explicit decision about this test-owned partial copy.
            self.assertEqual(recover_run(journal).state, 'recovered')

    def test_recovery_can_itself_exit_after_restore_and_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve(); journal = self.child(root, 'all_published')
            script = r'''
import os,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from easysewer.runtime import recover_run
replace=os.replace
def replacing(src,dst):
 value=replace(src,dst)
 if Path(src).name.startswith('.easysewer-backup-'):os._exit(38)
 return value
os.replace=replacing
recover_run(sys.argv[2])
'''
            process = subprocess.run([sys.executable, '-I', '-B', '-c', script,
                str(Path(easysewer.__file__).resolve().parent.parent), str(journal)], capture_output=True, timeout=30)
            self.assertEqual(process.returncode, 38, process.stderr)
            self.assertEqual(recover_run(journal).state, 'recovered')
            self.assertEqual((root/'a').read_bytes(), b'old-a')
            self.assertEqual((root/'b').read_bytes(), b'old-b')
            self.assertFalse((root/'z').exists())

    def test_changed_lock_and_lease_and_invalid_paths_are_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve(); journal = self.child(root, 'published')
            raw = journal.read_bytes(); data = json.loads(raw)
            data['targets'][0]['backup'] = str(root/'source')
            journal.write_text(json.dumps(data))
            with self.assertRaisesRegex(ValueError, 'artifact location'):recover_run(journal)
            journal.write_bytes(raw)
            lease = journal.with_suffix('.lease'); original_lease = lease.with_suffix('.saved')
            lease.rename(original_lease); lease.write_bytes(b'0')
            with self.assertRaisesRegex(ValueError, 'lease identity changed'):recover_run(journal)
            lease.unlink(); original_lease.rename(lease)
            target = json.loads(raw)['targets'][0]; lock = Path(target['lock']); saved = lock.read_bytes()
            lock.write_bytes(b'{}')
            self.assertEqual(recover_run(journal).state, 'conflicted')
            self.assertEqual((root/'a').read_bytes(), b'new')
            lock.write_bytes(saved)
            self.assertEqual(recover_run(journal).state, 'recovered')

    def test_pid_reuse_does_not_override_the_os_lease(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve(); journal = self.child(root, 'reserved')
            data = json.loads(journal.read_bytes()); data['pid'] = os.getpid()
            journal.write_text(json.dumps(data))
            self.assertEqual(inspect_run_recovery(journal).state, 'interrupted')
            self.assertEqual(recover_run(journal).state, 'recovered')

    def test_malformed_evidence_is_rejected_before_any_target_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve(); journal = self.child(root, 'all_published')
            raw = journal.read_bytes(); states = []
            for change in ('version', 'missing', 'identity', 'content', 'temporary'):
                data = json.loads(raw)
                if change == 'version':data['version'] = True
                if change == 'missing':del data['targets'][0]['expected_content']
                if change == 'identity':data['targets'][0]['expected'][0] = 1.0
                if change == 'content':data['targets'][0]['prepared_content'][0][4] = True
                if change == 'temporary':data['targets'][0]['temporary'] = None
                states.append(json.dumps(data).encode())
            states.append(raw.replace(b'"version":', b'"version":1,"version":', 1))
            for bad in states:
                with self.subTest(data=bad[:50]):
                    journal.write_bytes(bad)
                    with self.assertRaises(ValueError):recover_run(journal)
                    self.assertEqual((root/'a').read_bytes(), b'new')
                    self.assertEqual((root/'b').read_bytes(), b'new')
                    self.assertEqual((root/'z/data').read_bytes(), b'tree-data')
                    self.assertEqual(len(list(root.glob('.easysewer-lock-*'))), 3)
            journal.write_bytes(raw)
            self.assertEqual(recover_run(journal).state, 'recovered')

    def test_journal_io_failure_preserves_primary_error_and_rolls_back(self):
        for phase in ('publishing', 'committed'):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve(); target = root/'a'; source = root/'source'
                target.write_bytes(b'old'); source.write_bytes(b'new')
                t = storage.OutputTransaction('journal-io', overwrite=True)
                original = recovery._write; error = OSError('journal write failure'); fired = []
                def writing(path, data):
                    if data['phase'] == phase and not fired:
                        fired.append(True); raise error
                    return original(path, data)
                try:
                    t.reserve(((target, False),))
                    with patch.object(recovery, '_write', side_effect=writing):
                        with self.assertRaises(OSError) as caught:t.publish({target: source})
                    self.assertIs(caught.exception, error)
                finally:t.close()
                self.assertFalse(t.committed); self.assertEqual(target.read_bytes(), b'old')
                self.assertFalse(list(root.glob('.easysewer-*')))

    def test_external_file_at_planned_backup_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve(); target = root/'a'; source = root/'source'
            target.write_bytes(b'old'); source.write_bytes(b'new')
            transaction = storage.OutputTransaction('backup-collision', overwrite=True)
            original = recovery._Journal.sync; backups = []
            def syncing(journal, current, **kwargs):
                result = original(journal, current, **kwargs)
                if kwargs.get('phase') == 'publishing':
                    backup = current.targets[0].backup
                    backup.write_bytes(b'EXTERNAL'); backups.append(backup)
                return result
            try:
                transaction.reserve(((target, False),))
                with patch.object(recovery._Journal, 'sync', syncing):
                    with self.assertRaisesRegex(FileExistsError, 'Backup destination appeared'):
                        transaction.publish({target: source})
            finally:transaction.close()
            self.assertFalse(transaction.committed)
            self.assertEqual(target.read_bytes(), b'old')
            self.assertEqual(backups[0].read_bytes(), b'EXTERNAL')
            self.assertEqual(recover_run(transaction.journal.path).state, 'conflicted')
            self.assertEqual(backups[0].read_bytes(), b'EXTERNAL')
            backups[0].unlink()  # Explicitly resolve this test-owned foreign file.
            self.assertEqual(recover_run(transaction.journal.path).state, 'recovered')
            self.assertEqual(target.read_bytes(), b'old')


if __name__ == '__main__':unittest.main()
