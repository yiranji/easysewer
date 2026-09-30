"""Recovery discovery across actual early process exits, without adopting writes."""
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
from easysewer.runtime import discover_run_recovery, recover_run
from easysewer.runtime import recovery as r
from easysewer.runtime._workspace import OutputTransaction


CHILD = r'''
import os,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from easysewer.runtime import recovery as r
from easysewer.runtime._workspace import OutputTransaction
root=Path(sys.argv[2]);mode=sys.argv[3]
initialize=r._Lease.__init__;replace=r.os.replace;opening=Path.open
def initialized(self,*args,**kwargs):
 initialize(self,*args,**kwargs)
 if kwargs.get('create') and mode=='lease':os._exit(66)
def replaced(source,target):
 if str(target).endswith('.json') and mode=='pending':os._exit(67)
 return replace(source,target)
def opened(path,*args,**kwargs):
 stream=opening(path,*args,**kwargs)
 if path.suffix=='.lease' and mode=='prelock' and args and args[0]=='x+b':
  (root/'ready').write_bytes(b'ready');sys.stdin.buffer.read(1)
 if path.name.endswith('.pending') and mode=='partial':os._exit(68)
 return stream
r._Lease.__init__=initialized;r.os.replace=replaced;Path.open=opened
t=OutputTransaction('initial-discovery',overwrite=True)
t.reserve(((root/'output',False),))
os._exit(69)
'''


class RecoveryDiscoveryTests(unittest.TestCase):
    def snapshot(self, root, *, exclude=()):
        return {p.name: (r._identity(p), p.read_bytes()) for p in root.iterdir()
                if p.is_file() and p not in exclude}

    def transaction(self, root):
        target = root/'output'
        target.write_bytes(b'previous')
        t = OutputTransaction('discovery', overwrite=True)
        t.reserve(((target, False),))
        return t

    def child(self, root, mode):
        result = subprocess.run([sys.executable, '-I', '-B', '-c', CHILD,
            str(Path(easysewer.__file__).resolve().parent.parent), str(root), mode],
            capture_output=True, timeout=30)
        self.assertEqual(result.returncode, {'lease':66, 'pending':67, 'partial':68, 'journal':69}[mode],
                         result.stderr)

    def test_actual_initial_exits_are_discoverable_without_adopting_pending_data(self):
        for mode in ('lease', 'pending', 'partial', 'journal'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                (root/'output').write_bytes(b'previous')
                self.child(root, mode)
                before = self.snapshot(root)
                item, = discover_run_recovery(root)
                self.assertEqual(self.snapshot(root), before)
                self.assertEqual(item.state, 'recoverable' if mode=='journal' else 'incomplete')
                self.assertEqual(set(item.files), {str(root/n) for n in before
                    if n.startswith('.easysewer-recovery-')})
                self.assertEqual(Path(item.journal).exists(), mode=='journal')
                if mode=='journal':
                    self.assertEqual(item.recovery.run_id, 'initial-discovery')
                    self.assertEqual(recover_run(item.journal).state, 'recovered')
                    self.assertEqual(discover_run_recovery(root), ())
                else:
                    self.assertIsNone(item.recovery)
                    self.assertTrue(item.issues)
                    if mode=='pending':
                        pending, = root.glob('*.pending')
                        self.assertEqual(json.loads(pending.read_bytes())['targets'], [])
                    retry = OutputTransaction('retry', overwrite=True)
                    try:retry.reserve(((root/'output', False),))
                    finally:retry.close()
                    self.assertEqual(self.snapshot(root), before)
                self.assertEqual((root/'output').read_bytes(), b'previous')

    def test_live_owner_with_pending_write_is_not_inspected_as_abandoned(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve();t = self.transaction(root)
            pending = t.journal.path.with_name(t.journal.path.name+'.'+'a'*32+'.pending')
            pending.write_bytes(b'not yet complete')
            try:
                before = self.snapshot(root, exclude=(t.journal.lease.path,))
                item, = discover_run_recovery(root)
                self.assertEqual(item.state, 'busy-or-inaccessible')
                self.assertIsNone(item.recovery)
                self.assertEqual(self.snapshot(root, exclude=(t.journal.lease.path,)), before)
                self.assertEqual(r._identity(t.journal.lease.path), t.journal.lease.identity)
                t.journal.lease.stream.seek(0)
                self.assertEqual(t.journal.lease.stream.read(), b'0')
            finally:
                pending.unlink();t.close()

    def test_live_initial_lease_is_busy_but_unlocked_initial_lease_is_only_incomplete(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve();path=root/('.easysewer-recovery-'+'a'*32+'.lease')
            lease = r._Lease(path, create=True)
            try:self.assertEqual(discover_run_recovery(root)[0].state, 'busy-or-inaccessible')
            finally:lease.close()
            self.assertEqual(discover_run_recovery(root)[0].state, 'incomplete')
            self.assertEqual(path.read_bytes(), b'0')

    def test_actual_live_initial_writer_without_lock_is_not_declared_abandoned(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();(root/'output').write_bytes(b'previous')
            p=subprocess.Popen([sys.executable,'-I','-B','-c',CHILD,
                str(Path(easysewer.__file__).resolve().parent.parent),str(root),'prelock'],
                stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
            try:
                deadline=time.monotonic()+15
                while not (root/'ready').exists() and p.poll() is None and time.monotonic()<deadline:
                    time.sleep(.01)
                self.assertTrue((root/'ready').exists());self.assertIsNone(p.poll())
                before=self.snapshot(root);item,=discover_run_recovery(root)
                self.assertEqual(item.state,'incomplete');self.assertIsNone(item.recovery)
                self.assertEqual(self.snapshot(root),before);self.assertIsNone(p.poll())
                _,errors=p.communicate(b'x',timeout=30)
                self.assertEqual(p.returncode,69,errors)
                self.assertEqual(recover_run(item.journal).state,'recovered')
                self.assertEqual((root/'output').read_bytes(),b'previous')
            finally:
                if p.poll() is None:p.kill()
                p.communicate(timeout=10)

    def test_missing_lease_invalid_journal_and_replaced_identity_remain_visible(self):
        for mode in ('missing', 'invalid', 'identity'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve();t=self.transaction(root);t.journal.lease.close()
                if mode=='missing':t.journal.lease.path.unlink()
                elif mode=='invalid':t.journal.path.write_bytes(b'{')
                else:
                    data=json.loads(t.journal.path.read_bytes());data['lease_identity']=[0,0]
                    r._write(t.journal.path,data)
                before=self.snapshot(root);item,=discover_run_recovery(root)
                self.assertEqual(item.state,'unavailable');self.assertTrue(item.issues)
                self.assertIsNone(item.recovery);self.assertEqual(self.snapshot(root),before)

    def test_pending_only_or_multiple_pending_files_are_not_promoted(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();stem='.easysewer-recovery-'+'a'*32+'.json.'
            for token in ('b','c'):(root/(stem+token*32+'.pending')).write_bytes(b'{}')
            before=self.snapshot(root);item,=discover_run_recovery(root)
            self.assertEqual(item.state,'incomplete');self.assertEqual(len(item.files),2)
            self.assertIsNone(item.recovery);self.assertEqual(self.snapshot(root),before)

    def test_canonical_journal_alone_is_authority_and_budget_is_per_journal(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();t=self.transaction(root);t.journal.lease.close()
            pending=t.journal.path.with_name(t.journal.path.name+'.'+'b'*32+'.pending')
            pending.write_bytes(b'not JSON, must not be parsed or adopted')
            before=self.snapshot(root)
            item,=discover_run_recovery(root,max_bytes=1)
            self.assertEqual(item.state,'unavailable');self.assertIn('max_bytes',item.issues[0])
            item,=discover_run_recovery(root)
            self.assertEqual(item.state,'recoverable');self.assertEqual(item.recovery.phase,'reserving')
            self.assertEqual(self.snapshot(root),before)

    def test_matching_directories_and_symlinks_are_not_followed(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();fake=root/('.easysewer-recovery-'+'a'*32+'.lease')
            fake.mkdir();(fake/'keep').write_bytes(b'keep')
            self.assertEqual(discover_run_recovery(root)[0].state,'unavailable')
            self.assertEqual((fake/'keep').read_bytes(),b'keep')
            fake2=root/('.easysewer-recovery-'+'b'*32+'.lease');outside=root/'outside'
            outside.write_bytes(b'never open this as a lease')
            try:fake2.symlink_to(outside)
            except OSError as error:self.skipTest(f'Symlinks unavailable: {error}')
            self.assertEqual([v.state for v in discover_run_recovery(root)],['unavailable','unavailable'])
            self.assertEqual(outside.read_bytes(),b'never open this as a lease')

    def test_unrelated_and_nested_names_are_ignored_and_budget_validated_even_when_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();(root/'nested').mkdir()
            name='.easysewer-recovery-'+'a'*32+'.lease'
            (root/'nested'/name).write_bytes(b'0')
            (root/(name+'.backup')).write_bytes(b'keep')
            self.assertEqual(discover_run_recovery(root),())
            for budget in (True,1.5,'10'):
                with self.assertRaises(TypeError):discover_run_recovery(root,max_bytes=budget)
            for budget in (0,-1,sys.maxsize):
                with self.assertRaises(ValueError):discover_run_recovery(root,max_bytes=budget)
            with self.assertRaises(NotADirectoryError):discover_run_recovery(root/(name+'.backup'))
            with self.assertRaises(FileNotFoundError):discover_run_recovery(root/'missing')

    def test_discovery_race_is_reported_and_does_not_authorize_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();t=self.transaction(root);t.journal.lease.close()
            original=r._Lease
            def changed(path):
                lease=original(path)
                t.journal.path.unlink()
                return lease
            with patch.object(r,'_Lease',side_effect=changed):item,=discover_run_recovery(root)
            self.assertEqual(item.state,'unavailable');self.assertIsNone(item.recovery)
            self.assertEqual((root/'output').read_bytes(),b'previous')

    def test_invalid_group_does_not_hide_an_independent_valid_run(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();t=self.transaction(root);t.journal.lease.close()
            fake=root/('.easysewer-recovery-'+'a'*32+'.json');fake.write_bytes(b'{')
            before=self.snapshot(root);items=discover_run_recovery(root)
            self.assertEqual(len(items),2)
            self.assertEqual({item.state for item in items},{'recoverable','unavailable'})
            valid=next(item for item in items if item.state=='recoverable')
            self.assertEqual(valid.journal,str(t.journal.path))
            self.assertEqual(self.snapshot(root),before)


if __name__=='__main__':unittest.main()
