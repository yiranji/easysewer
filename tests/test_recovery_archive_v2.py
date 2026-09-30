"""Initial artifact archiving with real process exits and delayed creators."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import zipfile

import easysewer
from easysewer.runtime import archive_run_recovery_files, discover_run_recovery, recover_run
from easysewer.runtime import recovery as r, _recovery_archive as a
from easysewer.runtime._workspace import OutputTransaction
from test_recovery_discovery_v2 import CHILD


ARCHIVER = r'''
import os,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from easysewer.runtime import archive_run_recovery_files
from easysewer.runtime import _recovery_archive as a
path=Path(sys.argv[2]);mode=sys.argv[3];link=os.link;unlink=Path.unlink;rename=os.rename;activate=a._activate
def linked(source,target,*args,**kwargs):
 result=link(source,target,*args,**kwargs)
 if mode=='zip' and str(target).endswith('.zip'):os._exit(81)
 if mode=='guard' and Path(target)==path.with_suffix('.lease'):os._exit(84)
 return result
def stopped():
 if mode=='revoked':os._exit(83)
 if mode=='pause':path.with_suffix('.ready').write_bytes(b'ready');sys.stdin.buffer.read(1)
def unlinked(source,*args,**kwargs):
 result=unlink(source,*args,**kwargs)
 if Path(source)==path.with_suffix('.lease'):stopped()
 if mode=='pending' and Path(source).name==path.name+'.'+'b'*32+'.pending':os._exit(85)
 return result
def renamed(source,target,*args,**kwargs):
 result=rename(source,target,*args,**kwargs)
 if Path(source)==path.with_suffix('.lease'):stopped()
 return result
def activating(*args,**kwargs):
 if mode=='prepared':os._exit(82)
 return activate(*args,**kwargs)
os.link=linked;Path.unlink=unlinked;os.rename=renamed;a._activate=activating
result=archive_run_recovery_files(path,reason='preserve initial artifacts')
if mode=='pause' and result.state=='archived':os._exit(86)
raise AssertionError(result)
'''


class RecoveryArchiveTests(unittest.TestCase):
    def initial(self, root, *, empty=False):
        path=root/('.easysewer-recovery-'+'a'*32+'.json')
        lease=path.with_suffix('.lease')
        lease.write_bytes(b'' if empty else b'0')
        pending=path.with_name(path.name+'.'+'b'*32+'.pending')
        pending.write_bytes(b'partial or opaque original evidence')
        (root/'output').write_bytes(b'original output')
        return path,pending

    def members(self, archive):
        with zipfile.ZipFile(archive) as saved:
            return {name:saved.read(name) for name in saved.namelist() if name!='manifest.json'}

    def invoke_child(self, script, root_or_journal, mode):
        return subprocess.run([sys.executable,'-I','-B','-c',script,
            str(Path(easysewer.__file__).resolve().parent.parent),str(root_or_journal),mode],
            capture_output=True,timeout=40)

    def test_archive_initial_files_preserves_bytes_and_new_runs(self):
        for empty in (False,True):
            with self.subTest(empty=empty),tempfile.TemporaryDirectory() as directory:
                root=Path(directory).resolve();path,pending=self.initial(root,empty=empty)
                old_id=r._identity(path.with_suffix('.lease'));raw=pending.read_bytes()
                result=archive_run_recovery_files(path,reason='preserve initial artifacts')
                self.assertEqual(result.state,'archived');self.assertEqual(result.retained_files,())
                saved=self.members(result.archive)
                self.assertEqual(saved[pending.name],raw);self.assertEqual(saved[path.with_suffix('.lease').name],b'' if empty else b'0')
                self.assertNotEqual(r._identity(path.with_suffix('.lease')),old_id)
                self.assertFalse(pending.exists());self.assertFalse(path.exists())
                self.assertEqual(discover_run_recovery(root)[0].archive,result)
                before={p.name:p.read_bytes() for p in root.iterdir()}
                self.assertEqual(archive_run_recovery_files(path,reason='preserve initial artifacts'),result)
                self.assertEqual({p.name:p.read_bytes() for p in root.iterdir()},before)
                t=OutputTransaction('independent',overwrite=True)
                try:t.reserve(((root/'output',False),))
                finally:t.close()
                self.assertEqual({p.name:p.read_bytes() for p in root.iterdir()},before)

    def test_actual_initial_crashes_archive_without_interpreting_pending_json(self):
        for mode,code in (('lease',66),('pending',67),('partial',68)):
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as directory:
                root=Path(directory).resolve();(root/'output').write_bytes(b'previous')
                process=self.invoke_child(CHILD,root,mode);self.assertEqual(process.returncode,code,process.stderr)
                lease,=root.glob('*.lease');path=lease.with_suffix('.json')
                before={p.name:p.read_bytes() for p in root.iterdir() if p.name!='output'}
                result=archive_run_recovery_files(path,reason='preserve initial artifacts')
                self.assertEqual(result.state,'archived');self.assertEqual(self.members(result.archive),before)
                self.assertEqual((root/'output').read_bytes(),b'previous');self.assertFalse(path.exists())

    def test_live_locked_owner_refuses_without_creating_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();path,pending=self.initial(root)
            lease=r._Lease(path.with_suffix('.lease'))
            try:
                names=set(root.iterdir())
                with self.assertRaises(OSError):archive_run_recovery_files(path,reason='keep')
                self.assertEqual(set(root.iterdir()),names)
            finally:lease.close()

    def test_live_creator_before_lock_is_blocked_or_loses_original_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();(root/'output').write_bytes(b'previous')
            p=subprocess.Popen([sys.executable,'-I','-B','-c',CHILD,
                str(Path(easysewer.__file__).resolve().parent.parent),str(root),'prelock'],
                stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
            try:
                deadline=time.monotonic()+15
                while not (root/'ready').exists() and p.poll() is None and time.monotonic()<deadline:time.sleep(.01)
                self.assertTrue((root/'ready').exists());self.assertIsNone(p.poll())
                lease,=root.glob('*.lease');path=lease.with_suffix('.json');identity=r._identity(lease)
                result=archive_run_recovery_files(path,reason='preserve initial artifacts')
                if os.name=='nt':
                    self.assertEqual(result.state,'archive-blocked');self.assertEqual(r._identity(lease),identity)
                    p.kill();p.communicate(timeout=15)
                    self.assertEqual(archive_run_recovery_files(path,reason='preserve initial artifacts').state,'archived')
                else:
                    self.assertEqual(result.state,'archived');self.assertNotEqual(r._identity(lease),identity)
                    _,errors=p.communicate(b'x',timeout=15)
                    self.assertEqual(p.returncode,1,errors);self.assertIn(b'Recovery lease was replaced',errors)
                    self.assertFalse(path.exists())
                self.assertEqual((root/'output').read_bytes(),b'previous')
            finally:
                if p.poll() is None:p.kill()
                p.communicate(timeout=15)

    def test_hard_exits_around_archive_publication_revocation_and_cleanup_resume(self):
        for mode,code in (('zip',81),('prepared',82),('revoked',83),('guard',84),('pending',85)):
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as directory:
                root=Path(directory).resolve();path,pending=self.initial(root);raw=pending.read_bytes()
                p=self.invoke_child(ARCHIVER,path,mode);self.assertEqual(p.returncode,code,p.stderr)
                result=archive_run_recovery_files(path,reason='preserve initial artifacts')
                self.assertEqual(result.state,'archived',result)
                self.assertEqual(self.members(result.archive)[pending.name],raw)
                self.assertFalse(list(root.glob('*.pending')))
                self.assertEqual((root/'output').read_bytes(),b'original output')

    def test_competing_archiver_cannot_enter_identity_transition(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();path,pending=self.initial(root)
            p=subprocess.Popen([sys.executable,'-I','-B','-c',ARCHIVER,
                str(Path(easysewer.__file__).resolve().parent.parent),str(path),'pause'],
                stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
            try:
                deadline=time.monotonic()+15
                while not path.with_suffix('.ready').exists() and p.poll() is None and time.monotonic()<deadline:time.sleep(.01)
                self.assertTrue(path.with_suffix('.ready').exists());self.assertIsNone(p.poll())
                with self.assertRaises(OSError):archive_run_recovery_files(path,reason='preserve initial artifacts')
                _,errors=p.communicate(b'x',timeout=30);self.assertEqual(p.returncode,86,errors)
                self.assertEqual(archive_run_recovery_files(path,reason='preserve initial artifacts').state,'archived')
            finally:
                if p.poll() is None:p.kill()
                p.communicate(timeout=15)

    def test_changed_or_added_pending_is_retained(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();path,pending=self.initial(root);raw=pending.read_bytes()
            link=os.link;changed=[]
            def linked(source,target,*args,**kwargs):
                value=link(source,target,*args,**kwargs)
                if Path(target)==path.with_suffix('.lease') and not changed:
                    pending.write_bytes(b'external changed bytes');changed.append(True)
                return value
            with patch.object(os,'link',linked):result=archive_run_recovery_files(path,reason='keep')
            self.assertEqual(result.state,'archive-conflicted');self.assertIn(str(pending),result.retained_files)
            self.assertEqual(pending.read_bytes(),b'external changed bytes');self.assertEqual(self.members(result.archive)[pending.name],raw)
            extra=path.with_name(path.name+'.'+'c'*32+'.pending');extra.write_bytes(b'new unrelated evidence')
            result=archive_run_recovery_files(path,reason='keep')
            self.assertIn(str(extra),result.retained_files);self.assertEqual(extra.read_bytes(),b'new unrelated evidence')

    def test_removal_failure_keeps_archiving_and_retry_reuses_same_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();path,pending=self.initial(root);unlink=Path.unlink
            def unlinked(source,*args,**kwargs):
                if source==pending:raise PermissionError('injected busy source')
                return unlink(source,*args,**kwargs)
            with patch.object(Path,'unlink',unlinked):result=archive_run_recovery_files(path,reason='keep')
            self.assertEqual(result.state,'archiving');self.assertEqual(discover_run_recovery(root)[0].state,'archiving')
            retry=archive_run_recovery_files(path,reason='keep')
            self.assertEqual(retry.state,'archived');self.assertEqual(retry.archive,result.archive)

    def test_archive_corruption_before_removal_preserves_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();path,pending=self.initial(root);original=a._read_file;changed=[];active=[]
            loading=a._load
            def loaded(lease,*args,**kwargs):
                value=loading(lease,*args,**kwargs)
                if value is not None and lease.path==path.with_suffix('.lease'):active.append(True)
                return value
            def reading(source,limit):
                result=original(source,limit)
                if source==pending and active and not changed:
                    archive,=root.glob('*.zip');old=archive.read_bytes();archive.write_bytes(b'corrupt archive');changed.append((archive,old))
                return result
            with patch.object(a,'_read_file',reading),patch.object(a,'_load',loaded):
                result=archive_run_recovery_files(path,reason='keep')
            self.assertTrue(pending.exists());self.assertEqual(result.state,'archive-conflicted')
            self.assertEqual(discover_run_recovery(root)[0].state,'unavailable')
            archive,old=changed[0];archive.write_bytes(old)
            self.assertEqual(archive_run_recovery_files(path,reason='keep').state,'archived')

    def test_disappearing_archive_is_not_mistaken_for_removed_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();path,pending=self.initial(root)
            loading=a._load;reading=a._read_file;active=[];removed=[]
            def loaded(lease,*args,**kwargs):
                value=loading(lease,*args,**kwargs)
                if value is not None and lease.path==path.with_suffix('.lease'):active.append(True)
                return value
            def read(source,limit):
                value=reading(source,limit)
                if source==pending and active and not removed:
                    archive,=root.glob('*.zip');removed.append((archive,archive.read_bytes()));archive.unlink()
                return value
            with patch.object(a,'_load',loaded),patch.object(a,'_read_file',read):
                result=archive_run_recovery_files(path,reason='keep')
            self.assertEqual(result.state,'archive-conflicted');self.assertIn(str(pending),result.retained_files)
            self.assertTrue(pending.exists())
            archive,raw=removed[0];archive.write_bytes(raw)
            self.assertEqual(archive_run_recovery_files(path,reason='keep').state,'archived')

    def test_bad_archive_or_guard_and_wrong_reason_are_not_repaired_silently(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();path,pending=self.initial(root)
            result=archive_run_recovery_files(path,reason='keep');archive=Path(result.archive);original=archive.read_bytes()
            with self.assertRaises(ValueError):archive_run_recovery_files(path,reason='other')
            archive.write_bytes(b'bad')
            with self.assertRaises(ValueError):archive_run_recovery_files(path,reason='keep')
            self.assertEqual(archive.read_bytes(),b'bad');archive.write_bytes(original)
            lease=path.with_suffix('.lease');lease.write_bytes(a._MAGIC+b'{')
            with self.assertRaises(ValueError):archive_run_recovery_files(path,reason='keep')
            self.assertEqual(lease.read_bytes(),a._MAGIC+b'{')

    def test_missing_or_unknown_lease_and_canonical_journal_are_preserved(self):
        for mode in ('missing','unknown','canonical'):
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as directory:
                root=Path(directory).resolve();path,pending=self.initial(root)
                if mode=='missing':path.with_suffix('.lease').unlink()
                elif mode=='unknown':path.with_suffix('.lease').write_bytes(b'foreign lease')
                else:path.write_bytes(b'malformed canonical journal is still not ours to discard')
                before={p.name:p.read_bytes() for p in root.iterdir()}
                with self.assertRaises((OSError,ValueError)):archive_run_recovery_files(path,reason='keep')
                self.assertEqual({p.name:p.read_bytes() for p in root.iterdir()},before)

    def test_reason_budget_and_publication_failure_do_not_change_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();path,pending=self.initial(root);before={p.name:p.read_bytes() for p in root.iterdir()}
            for reason in ('','  ',None):
                with self.assertRaises((ValueError,TypeError)):archive_run_recovery_files(path,reason=reason)
            with self.assertRaises(ValueError):archive_run_recovery_files(path,reason='keep',max_bytes=2)
            with patch.object(os,'link',side_effect=OSError('archive publication failed')):
                with self.assertRaises(OSError):archive_run_recovery_files(path,reason='keep')
            self.assertEqual({p.name:p.read_bytes() for p in root.iterdir()},before)

    def test_initial_lease_edit_before_revocation_is_preserved_at_original_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();path,pending=self.initial(root);activate=a._activate
            def activating(path,original,*args,**kwargs):
                original.stream.seek(0);original.stream.write(b'external lease edit');original.stream.flush()
                return activate(path,original,*args,**kwargs)
            with patch.object(a,'_activate',activating):
                with self.assertRaisesRegex(ValueError,'content changed'):
                    archive_run_recovery_files(path,reason='keep')
            self.assertEqual(path.with_suffix('.lease').read_bytes(),b'external lease edit')
            self.assertEqual(pending.read_bytes(),b'partial or opaque original evidence')


if __name__=='__main__':unittest.main()
