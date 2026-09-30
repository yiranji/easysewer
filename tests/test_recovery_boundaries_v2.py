"""Reservation creation, release races, legacy journals and read budgets."""
import errno
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import easysewer
from easysewer.runtime import inspect_run_recovery, recover_run
from easysewer.runtime import recovery as r
from easysewer.runtime._workspace import OutputTransaction


CHILD = r'''
import os,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from easysewer.runtime import recovery as r
from easysewer.runtime._workspace import OutputTransaction
root=Path(sys.argv[2]);mode=sys.argv[3];target=root/'output';target.write_bytes(b'old')
sync=r._Journal.sync;opening=Path.open;unlink=Path.unlink;link=os.link
def syncing(j,t,**kw):
 if mode=='seed_written' and t.targets and t.targets[0].lock_identity is not None:os._exit(51)
 result=sync(j,t,**kw)
 if t.targets:
  if mode=='seed_planned' and t.targets[0].lock_identity is None:os._exit(51)
  if mode=='seed_journaled' and t.targets[0].lock_identity is not None:os._exit(51)
 if mode=='closed' and j.data['recovered']:os._exit(51)
 return result
def opened(p,*a,**kw):
 value=opening(p,*a,**kw)
 if mode=='seed_opened' and p.name.startswith('.easysewer-reserve-') and a and a[0]=='xb':os._exit(51)
 return value
def linked(a,b,*args,**kw):
 result=link(a,b,*args,**kw)
 if mode=='seed_linked':os._exit(51)
 return result
def unlinked(p,*a,**kw):
 value=unlink(p,*a,**kw)
 if mode=='seed_removed' and p.name.startswith('.easysewer-reserve-'):os._exit(51)
 return value
r._Journal.sync=syncing;Path.open=opened;os.link=linked;Path.unlink=unlinked
t=OutputTransaction('reservation-boundaries',overwrite=True);t.reserve(((target,False),));t.close()
raise AssertionError('Expected hard exit')
'''


class RecoveryBoundaryTests(unittest.TestCase):
    def child(self, root, mode):
        result = subprocess.run([sys.executable, '-I', '-B', '-c', CHILD,
            str(Path(easysewer.__file__).resolve().parent.parent), str(root), mode],
            capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 51, result.stderr)
        journal, = root.glob('.easysewer-recovery-*.json')
        return journal

    def transaction(self, root):
        target = root/'output';target.write_bytes(b'old')
        transaction = OutputTransaction('boundaries', overwrite=True)
        transaction.reserve(((target, False),))
        return transaction, target

    def test_hard_exits_around_atomic_reservation_creation_and_close(self):
        for mode in ('seed_planned', 'seed_written', 'seed_journaled', 'seed_linked', 'seed_removed', 'closed'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve();journal = self.child(root, mode)
                self.assertEqual(inspect_run_recovery(journal).state, 'interrupted')
                result = recover_run(journal)
                self.assertEqual(result.state, 'recovered', result.issues)
                self.assertEqual((root/'output').read_bytes(), b'old')
                self.assertFalse(list(root.glob('.easysewer-*')))
                retry = OutputTransaction('new-attempt', overwrite=True)
                try:retry.reserve(((root/'output', False),))
                finally:retry.close()

    def test_unfinished_private_source_cannot_block_a_new_run(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve();journal = self.child(root, 'seed_opened')
            self.assertFalse(list(root.glob('.easysewer-lock-*')))
            self.assertEqual(recover_run(journal).state, 'conflicted')
            seed, = root.glob('.easysewer-reserve-*')
            self.assertEqual(seed.read_bytes(), b'')
            retry = OutputTransaction('independent-attempt', overwrite=True)
            try:
                retry.reserve(((root/'output', False),))
                self.assertEqual(recover_run(journal).state, 'conflicted')
                self.assertTrue(retry.targets[0].lock.exists())
            finally:retry.close()
            seed.unlink()  # Explicitly resolve an incomplete test-owned private file.
            self.assertEqual(recover_run(journal).state, 'recovered')
            self.assertEqual((root/'output').read_bytes(), b'old')

    def test_replacement_lock_after_recovery_record_is_preserved(self):
        for mode in ('replaced', 'edited', 'legacy-edited'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve();t,target = self.transaction(root)
                journal = t.journal.path;lock = t.targets[0].lock;t.journal.lease.close()
                replacement = b'foreign reservation'
                if mode == 'legacy-edited':
                    legacy=json.loads(journal.read_bytes());legacy['version']=1
                    for v in legacy['targets']:v.pop('reservation_source');v.pop('lock_content')
                    r._write(journal,legacy)
                    marker=json.loads(lock.read_bytes());marker['external']='preserve this edit'
                    replacement=json.dumps(marker).encode()
                original = r._write;changed = []
                def write(path, data):
                    value = original(path, data)
                    if data['recovered'] and not changed:
                        if mode == 'replaced':lock.unlink()
                        lock.write_bytes(replacement);changed.append(True)
                    return value
                with patch.object(r, '_write', write):result = recover_run(journal)
                self.assertEqual(result.state, 'conflicted');self.assertTrue(result.issues)
                self.assertEqual(lock.read_bytes(), replacement)
                self.assertEqual(target.read_bytes(), b'old')
                lock.unlink()
                self.assertEqual(recover_run(journal).state, 'recovered')

    def test_missing_lock_replaced_during_early_recovery_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve();journal = self.child(root, 'seed_planned')
            lock = Path(json.loads(journal.read_bytes())['targets'][0]['lock'])
            original = r._write;changed = []
            def write(path, data):
                value = original(path, data)
                if data['recovered'] and not changed:
                    lock.write_bytes(b'foreign reservation');changed.append(True)
                return value
            with patch.object(r, '_write', write):result = recover_run(journal)
            self.assertEqual(result.state, 'conflicted')
            self.assertEqual(lock.read_bytes(), b'foreign reservation')
            lock.unlink();self.assertEqual(recover_run(journal).state, 'recovered')

    def test_normal_close_also_retains_replaced_or_edited_locks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve();t,target = self.transaction(root);lock = t.targets[0].lock
            original = r._Journal.sync;changed = []
            def sync(journal, transaction, **kwargs):
                value = original(journal, transaction, **kwargs)
                if journal.data['recovered'] and not changed:
                    lock.write_bytes(b'foreign reservation');changed.append(True)
                return value
            with patch.object(r._Journal, 'sync', sync):t.close()
            self.assertTrue(t.issues);self.assertEqual(lock.read_bytes(), b'foreign reservation')
            self.assertEqual(inspect_run_recovery(t.journal.path).state, 'interrupted')
            lock.unlink();self.assertEqual(recover_run(t.journal.path).state, 'recovered')
            self.assertEqual(target.read_bytes(), b'old')

    def test_link_failure_keeps_primary_error_and_cleans_owned_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve();target=root/'output';target.write_bytes(b'old')
            t = OutputTransaction('unsupported-hardlinks', overwrite=True)
            error = OSError(errno.EOPNOTSUPP, 'hard links unavailable')
            try:
                with patch('easysewer.runtime._workspace.os.link', side_effect=error):
                    with self.assertRaises(OSError) as caught:t.reserve(((target, False),))
                self.assertIs(caught.exception, error)
            finally:t.close()
            self.assertEqual(target.read_bytes(), b'old')
            self.assertFalse(list(root.glob('.easysewer-*')))

    def test_changed_private_source_is_rejected_before_destination_lock_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();target=root/'output';target.write_bytes(b'old')
            t=OutputTransaction('changed-source',overwrite=True);original=r._Journal.sync;changed=[]
            def sync(journal,transaction,**kwargs):
                value=original(journal,transaction,**kwargs)
                if transaction.targets and transaction.targets[0].lock_identity is not None and not changed:
                    transaction.targets[0].reservation_source.write_bytes(b'foreign edit');changed.append(True)
                return value
            try:
                with patch.object(r._Journal,'sync',sync), patch('easysewer.runtime._workspace.os.link') as link:
                    with self.assertRaisesRegex(ValueError,'reservation was removed or replaced'):
                        t.reserve(((target,False),))
                    link.assert_not_called()
            finally:t.close()
            self.assertFalse(list(root.glob('.easysewer-lock-*')))
            self.assertEqual(t.targets[0].reservation_source.read_bytes(),b'foreign edit')
            self.assertEqual(target.read_bytes(),b'old')
            t.targets[0].reservation_source.unlink()
            self.assertEqual(recover_run(t.journal.path).state,'recovered')

    def test_explicit_read_budget_has_exact_boundary_and_validates_before_io(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve();t,target=self.transaction(root);journal=t.journal.path;t.journal.lease.close()
            original = journal.read_bytes();size = len(original)
            for call in (inspect_run_recovery, recover_run):
                with self.assertRaisesRegex(ValueError, 'max_bytes'):call(journal,max_bytes=size-1)
                self.assertEqual(journal.read_bytes(), original)
                for invalid in (0, -1, sys.maxsize, True, 1.5, '10', None):
                    with self.assertRaises((TypeError, ValueError)):call(root/'missing',max_bytes=invalid)
            self.assertEqual(inspect_run_recovery(journal,max_bytes=size).state,'interrupted')
            self.assertEqual(recover_run(journal,max_bytes=size).state,'recovered')
            self.assertEqual(target.read_bytes(),b'old')

    def test_version1_inspection_is_read_only_and_recovery_migrates_without_loss(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve();t,target=self.transaction(root);journal=t.journal.path;t.journal.lease.close()
            data=json.loads(journal.read_bytes());data['version']=1
            for v in data['targets']:v.pop('reservation_source');v.pop('lock_content')
            r._write(journal,data);original=journal.read_bytes()
            self.assertEqual(inspect_run_recovery(journal).state,'interrupted')
            self.assertEqual(journal.read_bytes(),original)
            write=r._write;once=[]
            def interrupted(path,value):
                write(path,value)
                if value['recovered'] and not once:
                    once.append(True);raise OSError('interrupt after legacy migration')
            with patch.object(r,'_write',interrupted):value=recover_run(journal)
            self.assertEqual(value.state,'conflicted')
            migrated=json.loads(journal.read_bytes());self.assertEqual(migrated['version'],5)
            self.assertIsNone(migrated['targets'][0]['reservation_source'])
            self.assertIsNone(migrated['targets'][0]['lock_content'])
            self.assertEqual(recover_run(journal).state,'recovered')
            self.assertEqual(target.read_bytes(),b'old')


if __name__ == '__main__':unittest.main()
