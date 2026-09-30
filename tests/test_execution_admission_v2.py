"""Durable execution admission, late starts, revocation and recovery retries."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import easysewer
from easysewer.runtime import recover_run
from easysewer.runtime import _admission as admission
from easysewer.runtime._workspace import OutputTransaction, Workspace


DELAYED = r'''
import json,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from easysewer.runtime import _admission as a
record=json.loads(sys.argv[2]);mode=sys.argv[3]
if mode=='opened':
 original=Path.open
 def opened(path,*args,**kw):
  stream=original(path,*args,**kw)
  if str(path)==record['path'] and args==('r+b',):
   print('ready',flush=True);sys.stdin.readline()
  return stream
 Path.open=opened
else:
 print('ready',flush=True);sys.stdin.readline()
try:
 lease=a.enter(record)
except (OSError,ValueError) as error:
 print(json.dumps(dict(denied=True,error=str(error))),flush=True)
else:
 lease.close();raise AssertionError('Late worker was admitted after recovery')
'''

CRASH = r'''
import os,sys
sys.path.insert(0,sys.argv[1])
from easysewer.runtime import recovery as r
write=r._write
def interrupt(path,data):
 write(path,data)
 item,=data['workspaces']
 if item['admission']['revoked'] and item['cleanup_content'] is None:os._exit(71)
r._write=interrupt
r.recover_run(sys.argv[2])
raise AssertionError('Expected interruption after durable revocation')
'''


class ExecutionAdmissionTests(unittest.TestCase):
    def setup_execution(self, root):
        output=root/'output';output.write_bytes(b'old')
        t=OutputTransaction('admission',overwrite=True);t.reserve(((output,False),))
        w=Workspace(root,'admission');(w.root/'partial').write_bytes(b'partial')
        t.track_workspace(w,cleanup_on_crash=True)
        guard=t.prepare_execution(w)
        persisted,=json.loads(t.journal.path.read_bytes())['workspaces']
        self.assertEqual(persisted['admission'],guard)
        self.assertEqual(persisted['execution'],'not-started')
        t.workspace_execution(w,state='starting')
        t.journal.lease.close()
        return t,w,guard,output

    def test_active_admission_refuses_cleanup_and_revocation_is_irreversible(self):
        with tempfile.TemporaryDirectory() as folder:
            t,w,guard,output=self.setup_execution(Path(folder).resolve())
            lease=admission.enter(guard)
            try:
                result=recover_run(t.journal.path)
                self.assertEqual(result.state,'conflicted')
                self.assertTrue(any('worker is still active' in v for v in result.issues))
                self.assertTrue(w.root.exists());self.assertFalse(guard['revoked'])
            finally:lease.close()
            admission.revoke(guard)
            admission.revoke(guard)
            with self.assertRaisesRegex(ValueError,'revoked'):
                admission.enter(dict(guard,revoked=False))
            result=recover_run(t.journal.path)
            self.assertEqual(result.state,'recovered',result.issues)
            self.assertFalse(w.root.exists());self.assertEqual(output.read_bytes(),b'old')

    def test_late_worker_before_open_or_with_open_descriptor_cannot_enter(self):
        for mode in ('before-open','opened'):
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as folder:
                t,w,guard,output=self.setup_execution(Path(folder).resolve())
                worker=subprocess.Popen([sys.executable,'-I','-B','-u','-c',DELAYED,
                    str(Path(easysewer.__file__).resolve().parent.parent),json.dumps(guard),mode],
                    stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
                try:
                    self.assertEqual(worker.stdout.readline().strip(),b'ready')
                    first=recover_run(t.journal.path)
                    self.assertIn(first.state,('recovered','conflicted'),first.issues)
                    # An open descriptor can prevent deletion on Windows, but
                    # revocation must already be durable on either platform.
                    if first.state=='conflicted':
                        item,=json.loads(t.journal.path.read_bytes())['workspaces']
                        self.assertTrue(item['admission']['revoked'])
                    out,err=worker.communicate(b'continue\n',timeout=10)
                    self.assertEqual(worker.returncode,0,err)
                    self.assertTrue(json.loads(out)['denied'])
                    if first.state=='conflicted':
                        second=recover_run(t.journal.path)
                        self.assertEqual(second.state,'recovered',second.issues)
                    self.assertFalse(w.root.exists());self.assertEqual(output.read_bytes(),b'old')
                finally:
                    if worker.poll() is None:worker.kill();worker.wait(timeout=10)
                    for stream in (worker.stdin,worker.stdout,worker.stderr):stream.close()

    def test_recovery_exit_after_revocation_before_inventory_is_retryable(self):
        with tempfile.TemporaryDirectory() as folder:
            t,w,guard,output=self.setup_execution(Path(folder).resolve())
            child=subprocess.run([sys.executable,'-I','-B','-c',CRASH,
                str(Path(easysewer.__file__).resolve().parent.parent),str(t.journal.path)],
                capture_output=True,timeout=15)
            self.assertEqual(child.returncode,71,child.stderr)
            item,=json.loads(t.journal.path.read_bytes())['workspaces']
            self.assertTrue(item['admission']['revoked']);self.assertIsNone(item['cleanup_content'])
            self.assertTrue(w.root.is_dir())
            with self.assertRaisesRegex(ValueError,'revoked'):admission.enter(guard)
            result=recover_run(t.journal.path)
            self.assertEqual(result.state,'recovered',result.issues)
            self.assertFalse(w.root.exists());self.assertEqual(output.read_bytes(),b'old')

    def test_changed_or_replaced_admission_is_preserved(self):
        for mode in ('content','identity'):
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as folder:
                t,w,guard,output=self.setup_execution(Path(folder).resolve())
                path=Path(guard['path']);original=path.read_bytes()
                if mode=='content':path.write_bytes(b'0R' + b'f'*32)
                else:
                    path.rename(path.with_suffix('.saved'));path.write_bytes(original)
                changed=path.read_bytes();result=recover_run(t.journal.path)
                self.assertEqual(result.state,'conflicted');self.assertTrue(w.root.exists())
                self.assertEqual(path.read_bytes(),changed);self.assertTrue(t.journal.path.exists())
                self.assertEqual(output.read_bytes(),b'old')

    def test_before_launch_creation_or_journal_failure_never_grants_execution(self):
        for mode in ('create','journal'):
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as folder:
                root=Path(folder).resolve();output=root/'output';output.write_bytes(b'old')
                t=OutputTransaction('unlaunched',overwrite=True);t.reserve(((output,False),))
                w=Workspace(root,'unlaunched');t.track_workspace(w,cleanup_on_crash=True)
                def fail_create(path):
                    (Path(path)/'incomplete-admission').write_bytes(b'0')
                    raise OSError('injected create failure')
                if mode=='create':
                    with patch.object(admission,'create',fail_create),self.assertRaises(OSError):t.prepare_execution(w)
                else:
                    with patch('easysewer.runtime.recovery._write',side_effect=OSError('injected journal failure')),self.assertRaises(OSError):t.prepare_execution(w)
                t.journal.lease.close()
                persisted,=json.loads(t.journal.path.read_bytes())['workspaces']
                self.assertEqual(persisted['execution'],'not-started');self.assertIsNone(persisted['admission'])
                result=recover_run(t.journal.path)
                self.assertEqual(result.state,'recovered',result.issues)
                self.assertFalse(w.root.exists());self.assertEqual(output.read_bytes(),b'old')

    def test_overridden_native_factory_does_not_inherit_admission_permission(self):
        from easysewer.runtime.native import StandardBackend,_NATIVE_SESSION_FACTORY
        from easysewer.runtime.flexible import FlexiblePondingBackend
        class Override(StandardBackend):
            def session(self,**options):raise AssertionError('Third-party implementation')
        for backend in (StandardBackend(),FlexiblePondingBackend()):
            self.assertIs(backend.session.__func__,_NATIVE_SESSION_FACTORY)
        self.assertIsNot(Override().session.__func__,_NATIVE_SESSION_FACTORY)
        with patch.object(StandardBackend,'session',Override.session):
            self.assertIsNot(StandardBackend().session.__func__,_NATIVE_SESSION_FACTORY)


if __name__=='__main__':unittest.main()
