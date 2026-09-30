"""Post-execution workspace cleanup survives failures without losing ownership."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import easysewer
from easysewer.runtime import Runner, inspect_run_recovery, recover_run
from easysewer.runtime import recovery as r
from easysewer.runtime._workspace import OutputTransaction, Workspace
from easysewer.runtime.runner import _SessionUse
import test_backend_v2 as protocol_fixture
from test_options_v2 import network
from test_runner_v2 import config


CHILD = r'''
import os,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from easysewer.runtime._workspace import OutputTransaction,Workspace
root=Path(sys.argv[2]);mode=sys.argv[3]
target=root/'output';target.write_bytes(b'old')
t=OutputTransaction('cleanup-crash',overwrite=True);t.reserve(((target,False),))
w=Workspace(root,'cleanup-crash');(w.root/'a').write_bytes(b'a');(w.root/'b').write_bytes(b'b');t.track_workspace(w)
t.workspace_cleanup(w)
if mode=='partial':(w.root/'a').unlink()
if mode=='removed':w.close()
os._exit(53)
'''


class WorkspaceRecoveryTests(unittest.TestCase):
    def prepared(self, root):
        target=root/'output';target.write_bytes(b'old')
        t=OutputTransaction('cleanup',overwrite=True);t.reserve(((target,False),))
        w=Workspace(root,'cleanup');(w.root/'a').write_bytes(b'a')
        (w.root/'sub').mkdir();(w.root/'sub'/'b').write_bytes(b'b');t.track_workspace(w)
        t.workspace_cleanup(w);t.journal.lease.close()
        return t,w,target

    def test_hard_exit_before_during_and_after_workspace_deletion(self):
        for mode in ('before','partial','removed'):
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as folder:
                root=Path(folder).resolve()
                p=subprocess.run([sys.executable,'-I','-B','-c',CHILD,
                    str(Path(easysewer.__file__).resolve().parent.parent),str(root),mode],capture_output=True,timeout=30)
                self.assertEqual(p.returncode,53,p.stderr)
                journal,=root.glob('.easysewer-recovery-*.json')
                before=inspect_run_recovery(journal);self.assertEqual(before.cleaned_workspaces,())
                result=recover_run(journal);self.assertEqual(result.state,'recovered',result.issues)
                self.assertEqual(result.cleaned_workspaces,before.workspaces)
                self.assertFalse(any(Path(v).exists() for v in result.cleaned_workspaces))
                self.assertEqual((root/'output').read_bytes(),b'old')
                self.assertFalse(list(root.glob('.easysewer-*')))

    def test_partial_recovery_failure_can_retry_without_recreating_deleted_entries(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder).resolve();t,w,target=self.prepared(root)
            def partial(path,**kw):
                (path/'sub'/'b').unlink();(path/'sub').rmdir()
                raise OSError('injected partial cleanup')
            with patch('easysewer.runtime._workspace.remove_owned_tree',partial):
                result=recover_run(t.journal.path)
            self.assertEqual(result.state,'conflicted');self.assertTrue((w.root/'a').is_file())
            self.assertFalse((w.root/'sub').exists())
            result=recover_run(t.journal.path)
            self.assertEqual(result.cleaned_workspaces,(str(w.root),));self.assertFalse(w.root.exists())
            self.assertEqual(target.read_bytes(),b'old')

    def test_external_additions_edits_and_root_replacements_are_preserved(self):
        for mode in ('added','edited','root'):
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as folder:
                root=Path(folder).resolve();t,w,target=self.prepared(root)
                if mode=='added':(w.root/'foreign').write_bytes(b'foreign')
                elif mode=='edited':(w.root/'a').write_bytes(b'changed')
                else:
                    saved=w.root.with_name(w.root.name+'-saved');w.root.rename(saved)
                    w.root.mkdir();(w.root/'foreign').write_bytes(b'foreign')
                result=recover_run(t.journal.path);self.assertEqual(result.state,'conflicted')
                self.assertTrue(w.root.exists());self.assertEqual(target.read_bytes(),b'old')
                if mode=='added':(w.root/'foreign').unlink()
                elif mode=='edited':(w.root/'a').write_bytes(b'a')
                else:
                    self.assertEqual((w.root/'foreign').read_bytes(),b'foreign')
                    (w.root/'foreign').unlink();w.root.rmdir();saved.rename(w.root)
                self.assertEqual(recover_run(t.journal.path).state,'recovered')
                self.assertFalse(w.root.exists())

    def test_no_inventory_and_legacy_workspaces_remain_preserved(self):
        for version in (1,2,3,4,5):
            with self.subTest(version=version),tempfile.TemporaryDirectory() as folder:
                root=Path(folder).resolve();t,w,target=self.prepared(root)
                data=json.loads(t.journal.path.read_bytes());data['version']=version
                for item in data['workspaces']:
                    if version<5:item.pop('admission')
                    if version<4:
                        item.pop('cleanup_on_crash');item.pop('execution');item.pop('worker')
                    if version<3:item.pop('cleanup_requested');item.pop('cleanup_content');item.pop('cleaned')
                    else:item['cleanup_content']=None;item['cleanup_requested']=False
                if version==1:
                    for target_record in data['targets']:
                        target_record.pop('reservation_source');target_record.pop('lock_content')
                r._write(t.journal.path,data);original=t.journal.path.read_bytes()
                self.assertEqual(inspect_run_recovery(t.journal.path).cleaned_workspaces,())
                self.assertEqual(t.journal.path.read_bytes(),original)
                result=recover_run(t.journal.path);self.assertEqual(result.state,'recovered')
                self.assertEqual(result.cleaned_workspaces,());self.assertTrue((w.root/'a').is_file())

    def test_malformed_cleanup_evidence_is_rejected_without_deletion(self):
        for mutation in ('identity','cleaned','parent','missing'):
            with self.subTest(mutation=mutation),tempfile.TemporaryDirectory() as folder:
                root=Path(folder).resolve();t,w,target=self.prepared(root)
                data=json.loads(t.journal.path.read_bytes());item=data['workspaces'][0]
                if mutation=='identity':item['identity'][1]+=1
                elif mutation=='cleaned':item['cleaned']='yes'
                elif mutation=='parent':data['parents'].pop(str(w.root.parent))
                else:item['cleanup_content']=None;item['cleaned']=True
                r._write(t.journal.path,data)
                with self.assertRaises(ValueError):recover_run(t.journal.path)
                self.assertEqual((w.root/'a').read_bytes(),b'a')
                self.assertEqual(target.read_bytes(),b'old')

    def test_runner_cleanup_failure_keeps_primary_error_path_and_retry_record(self):
        with protocol_fixture.BackendContractTests().fixture() as (root,backend):
            output=root/'out';primary=RuntimeError('original callback failure')
            def progress(value):
                if value.phase=='preparing':raise primary
            with patch.object(Workspace,'close',side_effect=OSError('injected delete failure')):
                value=Runner(backends={backend.key:backend}).run(network(),
                    config(output,backend=backend.key,keep_failed_artifacts=False),progress=progress)
            self.assertEqual(value.failure.message,str(primary));self.assertEqual(value.failure.stage,'callback')
            self.assertIsNotNone(value.retained_directory);retained=Path(value.retained_directory)
            self.assertTrue(retained.is_dir())
            self.assertIn('run.workspace_cleanup',[d.code for d in value.diagnostics.diagnostics])
            journal,=output.glob('.easysewer-recovery-*.json')
            value.save(root/'archive');loaded=type(value).load(root/'archive')
            self.assertEqual(loaded.failure,value.failure);self.assertEqual(loaded.retained_directory,value.retained_directory)
            result=recover_run(journal);self.assertEqual(result.state,'recovered',result.issues)
            self.assertEqual(result.cleaned_workspaces,(str(retained),));self.assertFalse(retained.exists())

    def test_deliberately_retained_failure_is_not_scheduled_for_cleanup(self):
        with protocol_fixture.BackendContractTests().fixture() as (root,backend):
            def progress(value):
                if value.phase=='preparing':raise RuntimeError('keep failure')
            with patch.object(Workspace,'close',side_effect=AssertionError('must retain')):
                value=Runner(backends={backend.key:backend}).run(network(),config(root/'out'),progress=progress)
            self.assertTrue(Path(value.retained_directory).is_dir())
            self.assertNotIn('run.workspace_cleanup',[d.code for d in value.diagnostics.diagnostics])
            self.assertFalse(list((root/'out').glob('.easysewer-recovery-*')))

    def test_session_body_failure_and_cleanup_failure_are_distinguished(self):
        class Context:
            def __enter__(self):return self
            def __exit__(self,*args):return False
        use=_SessionUse(Context())
        with self.assertRaisesRegex(RuntimeError,'body'):
            with use:raise RuntimeError('body')
        self.assertTrue(use.stopped())
        class Broken(Context):
            def __exit__(self,*args):raise OSError('close')
        use=_SessionUse(Broken())
        with self.assertRaises(OSError):
            with use:pass
        self.assertFalse(use.stopped())


if __name__=='__main__':unittest.main()
