"""Actual bundled workers and publication followed by failed workspace cleanup."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch, PropertyMock

import easysewer
from easysewer import get_native_capabilities
from easysewer.runtime import Runner, recover_run
from easysewer.runtime._workspace import Workspace
from easysewer.runtime._process_session import ProcessSession
from test_options_v2 import network
from test_runner_v2 import config


EVIDENCE=[]

CHILD=r'''
import json,os,sys
from pathlib import Path
sys.path[:0]=[sys.argv[1],sys.argv[2]]
from easysewer.runtime import Runner
from easysewer.runtime._workspace import Workspace
from easysewer.runtime.runner import _SessionUse
from test_options_v2 import network
from test_runner_v2 import config
root=Path(sys.argv[3]);backend=sys.argv[4];model=network()
if backend=='easysewer:flexible-ponding':model.update_options(flow_routing='DYNWAVE',allow_ponding=True)
stopped=_SessionUse.stopped
def checked(use):
 result=stopped(use)
 assert result and type(use.value.returncode) is int
 (root/'worker-exit.json').write_text(json.dumps(dict(pid=use.value.pid,returncode=use.value.returncode)))
 return result
_SessionUse.stopped=checked
Workspace.close=lambda self:os._exit(54)
Runner().run(model,config(root/'out',backend=backend,overwrite=True,keep_failed_artifacts=False))
raise AssertionError('Expected parent exit at cleanup')
'''


@unittest.skipUnless(get_native_capabilities()['swmm_solver'],'Native solver unavailable')
class NativeWorkspaceRecoveryTests(unittest.TestCase):
    def model(self,backend):
        model=network()
        if backend=='easysewer:flexible-ponding':model.update_options(flow_routing='DYNWAVE',allow_ponding=True)
        return model

    def test_real_success_and_callback_failure_keep_cleanup_receipt_and_retry(self):
        for backend in ('swmm:standard','easysewer:flexible-ponding'):
            for fail in (False,True):
                with self.subTest(backend=backend,fail=fail),tempfile.TemporaryDirectory() as folder:
                    root=Path(folder).resolve();output=root/'out';output.mkdir()
                    (output/'model.out').write_bytes(b'previous')
                    def progress(value):
                        if fail and value.phase=='running':raise RuntimeError('original running callback')
                    with patch.object(Workspace,'close',side_effect=OSError('injected cleanup failure')):
                        value=Runner().run(self.model(backend),config(output,backend=backend,
                            overwrite=True,keep_failed_artifacts=False),progress=progress)
                    self.assertEqual(value.succeeded,not fail,value.failure)
                    if fail:self.assertEqual(value.failure.message,'original running callback')
                    retained=Path(value.retained_directory);self.assertTrue(retained.is_dir())
                    journal,=output.glob('.easysewer-recovery-*.json')
                    data=json.loads(journal.read_bytes());item,=data['workspaces']
                    self.assertTrue(item['cleanup_requested']);self.assertIsNotNone(item['cleanup_content'])
                    before=(output/'model.out').read_bytes()
                    if fail:self.assertEqual(before,b'previous')
                    value.save(root/'archive');loaded=type(value).load(root/'archive')
                    self.assertEqual(loaded.failure,value.failure)
                    result=recover_run(journal);self.assertEqual(result.state,'recovered',result.issues)
                    self.assertEqual(result.cleaned_workspaces,(str(retained),));self.assertFalse(retained.exists())
                    self.assertEqual((output/'model.out').read_bytes(),before)
                    repeated=Runner().run(self.model(backend),config(output,backend=backend,overwrite=True))
                    self.assertTrue(repeated.succeeded,repeated.failure)
                    if not fail:self.assertEqual(repeated.output.read_bytes(),before)
                    EVIDENCE.append(dict(backend=backend,primary_failure=fail,workspace_removed=True,
                        output_preserved=True,rerun_succeeded=True,output_sha256=hashlib.sha256(repeated.output.read_bytes()).hexdigest()))

    def test_unknown_worker_exit_keeps_directory_and_pending_journal(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder).resolve();output=root/'out'
            with patch.object(ProcessSession,'returncode',new_callable=PropertyMock,return_value=None),\
                    patch('easysewer.runtime._admission.revoke',side_effect=PermissionError('injected cleanup denial')):
                value=Runner().run(network(),config(output,keep_failed_artifacts=False))
            self.assertTrue(value.succeeded,value.failure)
            retained=Path(value.retained_directory);self.assertTrue(retained.is_dir())
            journal,=output.glob('.easysewer-recovery-*.json')
            item,=json.loads(journal.read_bytes())['workspaces']
            self.assertTrue(item['cleanup_requested']);self.assertIsNone(item['cleanup_content'])
            with patch('easysewer.runtime._admission._Lease',side_effect=PermissionError('injected denied')):
                result=recover_run(journal)
            self.assertEqual(result.state,'conflicted')
            self.assertTrue(retained.is_dir());self.assertTrue(journal.is_file())
            self.assertTrue(any('injected denied' in v for v in result.issues))
            with patch('easysewer.runtime._worker_identity.has_exited',side_effect=AssertionError('Admission should prove ownership independently')):
                result=recover_run(journal)
            self.assertEqual(result.state,'recovered',result.issues)
            self.assertFalse(retained.exists())

    def test_admission_proves_cleanup_after_factory_failure_or_unknown_return_code(self):
        original=ProcessSession.__init__
        def failed_factory(session,*args,**kw):
            original(session,*args,**kw)
            if kw.get('_execution_guard') is not None:
                session.close()
                raise RuntimeError('injected failure after factory cleanup')
        for backend in ('swmm:standard','easysewer:flexible-ponding'):
            for mode in ('factory','unknown-code'):
                with self.subTest(backend=backend,mode=mode),tempfile.TemporaryDirectory() as folder:
                    output=Path(folder).resolve()/'out';output.mkdir()
                    (output/'model.out').write_bytes(b'previous')
                    injection=(patch.object(ProcessSession,'__init__',failed_factory) if mode=='factory'
                        else patch.object(ProcessSession,'returncode',new_callable=PropertyMock,return_value=None))
                    with injection:
                        result=Runner().run(self.model(backend),config(output,backend=backend,
                            overwrite=True,keep_failed_artifacts=False))
                    self.assertEqual(result.succeeded,mode=='unknown-code',result.failure)
                    if mode=='factory':
                        self.assertEqual(result.failure.message,'injected failure after factory cleanup')
                        self.assertEqual((output/'model.out').read_bytes(),b'previous')
                    self.assertIsNone(result.retained_directory)
                    self.assertFalse(list(output.glob('.easysewer-*')))

    def test_parent_exit_after_actual_worker_exit_and_cleanup_authorization(self):
        for backend in ('swmm:standard','easysewer:flexible-ponding'):
            with self.subTest(backend=backend),tempfile.TemporaryDirectory() as folder:
                root=Path(folder).resolve();output=root/'out'
                first=Runner().run(self.model(backend),config(output,backend=backend))
                self.assertTrue(first.succeeded,first.failure);original=first.output.read_bytes()
                child=subprocess.run([sys.executable,'-I','-B','-c',CHILD,
                    str(Path(easysewer.__file__).resolve().parent.parent),str(Path(__file__).parent),
                    str(root),backend],capture_output=True,timeout=60)
                self.assertEqual(child.returncode,54,child.stderr)
                worker=json.loads((root/'worker-exit.json').read_bytes());self.assertEqual(worker['returncode'],0)
                journal,=output.glob('.easysewer-recovery-*.json')
                result=recover_run(journal);self.assertEqual(result.state,'recovered',result.issues)
                self.assertTrue(result.committed);self.assertEqual(len(result.cleaned_workspaces),1)
                self.assertFalse(Path(result.cleaned_workspaces[0]).exists())
                self.assertEqual((output/'model.out').read_bytes(),original)
                EVIDENCE.append(dict(backend=backend,parent_exit=54,worker_returncode=0,
                    workspace_removed=True,output_preserved=True,output_sha256=hashlib.sha256(original).hexdigest()))


if __name__=='__main__':unittest.main()
