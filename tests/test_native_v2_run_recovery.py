"""A real Runner parent crash is recoverable with both bundled solvers."""
import hashlib
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

import easysewer
from easysewer import get_native_capabilities
from easysewer.runtime import Runner, inspect_run_recovery, recover_run
from test_options_v2 import network
from test_runner_v2 import config


EVIDENCE = []
CHILD = r'''
import os,sys
from pathlib import Path
sys.path[:0]=[sys.argv[1],sys.argv[2]]
from easysewer.runtime import Runner
from easysewer.runtime import recovery as r
from test_options_v2 import network
from test_runner_v2 import config
root=Path(sys.argv[3]);backend=sys.argv[4];stage=sys.argv[5]
model=network()
if backend=='easysewer:flexible-ponding':model.update_options(flow_routing='DYNWAVE',allow_ponding=True)
def progress(value):
 if value.phase==stage:os._exit(39)
sync=r._Journal.sync
def syncing(journal,transaction,**kw):
 value=sync(journal,transaction,**kw)
 if stage=='committed' and kw.get('phase')=='committed':os._exit(39)
 return value
r._Journal.sync=syncing
result=Runner().run(model,config(root,backend=backend,overwrite=True,keep_failed_artifacts=False),progress=progress)
raise AssertionError(result.failure)
'''


@unittest.skipUnless(get_native_capabilities()['swmm_solver'], 'Native solver unavailable')
class NativeRunRecoveryTests(unittest.TestCase):
    def test_real_parent_exit_recovery_and_complete_output_on_rerun(self):
        for backend in ('swmm:standard', 'easysewer:flexible-ponding'):
            for phase in ('preparing', 'running', 'finalizing', 'committed'):
                with self.subTest(backend=backend, phase=phase), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory).resolve(); model = network()
                    if backend == 'easysewer:flexible-ponding':model.update_options(flow_routing='DYNWAVE', allow_ponding=True)
                    first = Runner().run(model, config(root, backend=backend))
                    self.assertTrue(first.succeeded, first.failure)
                    original = first.output.read_bytes()
                    process = subprocess.run([sys.executable, '-I', '-B', '-c', CHILD,
                        str(Path(easysewer.__file__).resolve().parent.parent), str(Path(__file__).parent),
                        str(root), backend, phase], capture_output=True, timeout=60)
                    self.assertEqual(process.returncode, 39, process.stderr)
                    journal, = root.glob('.easysewer-recovery-*.json')
                    self.assertEqual(inspect_run_recovery(journal).state, 'interrupted')
                    recovered = recover_run(journal)
                    deadline=time.monotonic()+10
                    while recovered.state=='conflicted' and any('worker is still active' in v for v in recovered.issues) and time.monotonic()<deadline:
                        time.sleep(.02);recovered=recover_run(journal)
                    self.assertEqual(recovered.state, 'recovered', recovered.issues)
                    self.assertEqual((root/'model.out').read_bytes(), original)
                    self.assertEqual(recovered.cleaned_workspaces,recovered.workspaces)
                    self.assertTrue(all(not Path(p).exists() for p in recovered.workspaces))
                    repeated = Runner().run(model, config(root, backend=backend, overwrite=True))
                    self.assertTrue(repeated.succeeded, repeated.failure)
                    self.assertEqual(repeated.output.read_bytes(), original)
                    self.assertFalse(list(root.glob('.easysewer-lock-*')))
                    self.assertFalse(list(root.glob('.easysewer-recovery-*')))
                    EVIDENCE.append(dict(backend=backend, phase=phase, parent_exit=39,
                        recovered=True, output_sha256=hashlib.sha256(original).hexdigest(),
                        workspaces_preserved=sum(Path(p).exists() for p in recovered.workspaces),
                        workspaces_cleaned=len(recovered.cleaned_workspaces)))


if __name__ == '__main__':unittest.main()
