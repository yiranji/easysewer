"""Worker process creation identities and conservative crash cleanup decisions."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import easysewer
from easysewer.runtime import recover_run
from easysewer.runtime import _worker_identity as identity
from easysewer.runtime._workspace import OutputTransaction, Workspace


class WorkerIdentityTests(unittest.TestCase):
    def workspace(self,root,*,cleanup=True):
        target=root/'output';target.write_bytes(b'old')
        t=OutputTransaction('identity',overwrite=True);t.reserve(((target,False),))
        w=Workspace(root,'identity');(w.root/'partial').write_bytes(b'partial')
        t.track_workspace(w,cleanup_on_crash=cleanup)
        return t,w,target

    def test_current_process_and_reused_pid_are_distinguished_without_signals(self):
        original=identity.capture();self.assertEqual(original['pid'],os.getpid())
        self.assertFalse(identity.has_exited(original))
        previous=dict(original,started=original['started']+1)
        self.assertTrue(identity.has_exited(previous))
        self.assertFalse(identity.has_exited(original))

    def test_independent_child_identity_survives_exit(self):
        code='import json,sys;sys.path.insert(0,sys.argv[1]);from easysewer.runtime._worker_identity import capture;print(json.dumps(capture()),flush=True);sys.stdin.readline()'
        child=subprocess.Popen([sys.executable,'-I','-B','-c',code,
            str(Path(easysewer.__file__).resolve().parent.parent)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        try:
            value=json.loads(child.stdout.readline());self.assertEqual(value['pid'],child.pid)
            self.assertFalse(identity.has_exited(value))
            child.communicate(b'finish\n',timeout=10);self.assertEqual(child.returncode,0)
            self.assertTrue(identity.has_exited(value))
        finally:
            if child.poll() is None:child.kill();child.wait(timeout=10)
            for stream in (child.stdin,child.stdout,child.stderr):stream.close()

    def test_malformed_identity_and_permission_errors_never_mean_exited(self):
        value=identity.capture()
        for field,wrong in (('pid',0),('pid',True),('started',False),('started',0),('platform','other')):
            with self.subTest(field=field,wrong=wrong),self.assertRaises(ValueError):
                identity.has_exited(dict(value,**{field:wrong}))
        with patch.object(identity,'_observe',side_effect=PermissionError('denied')):
            with self.assertRaises(PermissionError):identity.has_exited(value)
        with self.assertRaises(ValueError):identity.validate(value,pid=value['pid']+1)

    def test_linux_stat_handles_parentheses_arbitrary_command_bytes_and_zombies(self):
        boot='12345678-1234-1234-1234-123456789abc';namespace=[1,2]
        for state,alive in ((b'R',True),(b'Z',False)):
            fields=[state]+[b'0']*18+[b'123']
            raw=b'17 (name )\xff with spaces) '+b' '.join(fields)
            with patch.object(identity,'_linux_environment',return_value=(boot,namespace)),patch.object(Path,'read_bytes',return_value=raw):
                value,actual=identity._linux(17)
            self.assertEqual(actual,alive);self.assertEqual(value['started'],123)
            self.assertEqual(value['pid_namespace'],namespace)

    def test_changed_boot_or_namespace_is_refused_before_pid_lookup(self):
        boot='12345678-1234-1234-1234-123456789abc'
        value=dict(platform='linux',pid=17,started=123,boot_id=boot,pid_namespace=[1,2])
        for environment in ((boot,[1,3]),('22345678-1234-1234-1234-123456789abc',[1,2])):
            with patch.object(identity.sys,'platform','linux'),patch.object(identity,'_linux_environment',return_value=environment),patch.object(identity,'_observe') as observe:
                with self.assertRaisesRegex(ValueError,'boot or PID namespace'):identity.has_exited(value)
                observe.assert_not_called()

    def test_startup_gap_and_missing_worker_identity_preserve_workspace(self):
        for state in ('starting','active'):
            with self.subTest(state=state),tempfile.TemporaryDirectory() as folder:
                root=Path(folder).resolve();t,w,target=self.workspace(root)
                t.workspace_execution(w,state='starting')
                if state=='active':t.workspace_execution(w,state='active')
                t.journal.lease.close();result=recover_run(t.journal.path)
                self.assertEqual(result.state,'conflicted');self.assertTrue(w.root.is_dir())
                self.assertTrue(t.journal.path.is_file());self.assertEqual(target.read_bytes(),b'old')
                self.assertTrue(any('worker identity is incomplete' in v for v in result.issues))

    def test_active_worker_is_not_cleaned_and_proven_exit_allows_retry(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder).resolve();t,w,target=self.workspace(root);stamp=identity.capture()
            t.workspace_execution(w,state='starting');t.workspace_execution(w,state='active',identity=stamp)
            t.journal.lease.close();result=recover_run(t.journal.path)
            self.assertEqual(result.state,'conflicted');self.assertTrue((w.root/'partial').is_file())
            self.assertTrue(any('worker is still active' in v for v in result.issues))
            with patch.object(identity,'_observe',return_value=(stamp,False)):
                result=recover_run(t.journal.path)
            self.assertEqual(result.state,'recovered',result.issues);self.assertFalse(w.root.exists())
            self.assertEqual(result.cleaned_workspaces,(str(w.root),));self.assertEqual(target.read_bytes(),b'old')

    def test_before_session_crash_cleanup_obeys_retention_policy(self):
        for cleanup in (False,True):
            with self.subTest(cleanup=cleanup),tempfile.TemporaryDirectory() as folder:
                root=Path(folder).resolve();t,w,target=self.workspace(root,cleanup=cleanup)
                t.journal.lease.close();result=recover_run(t.journal.path)
                self.assertEqual(result.state,'recovered',result.issues)
                self.assertEqual(w.root.exists(),not cleanup)
                self.assertEqual(result.cleaned_workspaces,(str(w.root),) if cleanup else ())
                self.assertEqual(target.read_bytes(),b'old')

    def test_crash_cleanup_inventory_is_persisted_before_deletion_and_rechecked(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder).resolve();t,w,target=self.workspace(root);t.journal.lease.close()
            from easysewer.runtime import recovery
            write=recovery._write;changed=[]
            def alter(path,data):
                result=write(path,data)
                if data['workspaces'][0]['cleanup_content'] is not None and not changed:
                    (w.root/'foreign').write_bytes(b'foreign');changed.append(True)
                return result
            with patch.object(recovery,'_write',alter):result=recover_run(t.journal.path)
            self.assertEqual(result.state,'conflicted');self.assertEqual((w.root/'foreign').read_bytes(),b'foreign')
            (w.root/'foreign').unlink();self.assertEqual(recover_run(t.journal.path).state,'recovered')


if __name__=='__main__':unittest.main()
