"""Real clean venv workers: native solve, dependencies, termination and recovery.

Run this module on Windows and Linux, using the installed native wheel. No pip
or network access is needed: the selected installed package is copied into an
otherwise empty stdlib venv. Never skip a broken native probe inside that venv.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import venv

import easysewer
from easysewer import get_native_capabilities
from easysewer.runtime._worker_identity import has_exited


INP = '''[OPTIONS]
FLOW_UNITS CMS
FLOW_ROUTING DYNWAVE
START_DATE 01/01/2026
START_TIME 00:00:00
REPORT_START_DATE 01/01/2026
REPORT_START_TIME 00:00:00
END_DATE 01/01/2026
END_TIME 00:10:00
REPORT_STEP 00:01:00
ROUTING_STEP 00:00:05
[JUNCTIONS]
J1 1 2 0 0 0
[OUTFALLS]
O1 0 FREE NO
[CONDUITS]
P1 J1 O1 100 0.013 0 0 0 0
[XSECTIONS]
P1 CIRCULAR 1 0 0 0 1
[DWF]
J1 FLOW 0.1
[REPORT]
NODES ALL
LINKS ALL
[COORDINATES]
J1 0 0
O1 100 0
'''

CHILD = r"""
import json, os, sys, threading, time
from pathlib import Path
from unittest.mock import patch
import easysewer
from easysewer import Model
from easysewer.model import FileReference
from easysewer.runtime import StandardBackend, RunConfig, SessionError, SessionCancelled, SessionTimeout, recover_run
from easysewer.runtime import _process_session
from easysewer.runtime._worker_identity import has_exited
from easysewer.runtime._workspace import OutputTransaction, Workspace
root = Path(sys.argv[1]); mode = sys.argv[2]
assert sys.prefix != sys.base_prefix
assert Path(easysewer.__file__).is_relative_to(Path(sys.prefix))
assert sys.flags.isolated and sys.dont_write_bytecode
connections = []
factory = _process_session._WorkerConnection
def traced(directory):
    result = factory(directory)
    connections.append(result)
    return result
_process_session._WorkerConnection = traced
if mode == 'clean':
    info = StandardBackend().probe()
    assert info.available, info.reason
    assert info.engine_version == 52004
    result = Model.from_inp(root/'model.inp', strict=True).run(RunConfig(
        output_directory=FileReference(path=str(root/'results'), direction='output')))
    assert result.succeeded, (result.failure, result.diagnostics.errors)
    with result.open_output() as output:
        assert output.metadata.periods == 10, output.metadata.periods
    assert all(c.process.poll() == 0 for c in connections)
    print(json.dumps(dict(mode=mode, periods=10, workers=len(connections))))
    raise SystemExit(0)

# Instrument the production command without replacing interpreter selection,
# isolation flags, environment handoff, startup guard or the real native worker.
original_command = _process_session._worker_command
blocking = False
def instrumented():
    command = original_command()
    prefix = '''
import json, os, sys, time
from pathlib import Path
import venv_only_dependency, pth_only_dependency
from easysewer.runtime._worker_identity import capture
assert venv_only_dependency.TOKEN == 'venv-only'
assert pth_only_dependency.TOKEN == 'pth-only'
assert sys.flags.isolated and sys.dont_write_bytecode and sys.stdout.write_through
stamp = capture()
Path(RECEIPT, 'worker-'+str(os.getpid())+'.json').write_text(json.dumps(dict(
    identity=stamp, prefix=sys.prefix, package=__import__('easysewer').__file__,
    dependency=venv_only_dependency.__file__, pth=pth_only_dependency.__file__)))
'''.replace('RECEIPT', repr(str(root)))
    if blocking:
        prefix += ('from easysewer.runtime._native_solver import NativeSolver\n'
                   'def blocked_step(self, count):\n    time.sleep(30)\n'
                   'NativeSolver.step = blocked_step\n')
    command[-1] = command[-1].replace('from easysewer.runtime._solver_worker import main\nmain()',
                                    prefix+'\nfrom easysewer.runtime._solver_worker import main\nmain()')
    return command
_process_session._worker_command = instrumented

def receipt(connection):
    value = json.loads((root/('worker-'+str(connection.process.pid)+'.json')).read_text())
    assert value['identity']['pid'] == connection.process.pid
    assert Path(value['prefix']) == Path(sys.prefix)
    assert Path(value['package']) == Path(easysewer.__file__)
    assert Path(value['dependency']).is_relative_to(Path(sys.prefix))
    assert Path(value['pth']).parent == root/'extension'
    return value['identity']

def retired(connection):
    stamp = receipt(connection)
    assert connection.process.poll() is not None
    assert has_exited(stamp), stamp
    assert all(not t.is_alive() for t in connection._threads)

backend = StandardBackend()
if mode == 'parent-exit':
    session = backend.session(working_directory=root)
    stamp = receipt(connections[-1])
    assert stamp == session.worker_identity
    (root/'abandoned.json').write_text(json.dumps(stamp))
    os._exit(62)

# A successful session and real native solve retain exactly the created PID.
with backend.session(working_directory=root) as session:
    assert session.pid == session.worker_identity['pid']
    assert receipt(connections[-1]) == session.worker_identity
    assert not has_exited(session.worker_identity)
    session.open('model.inp', 'session.rpt', 'session.out')
    session.start()
    while not session.step(max_steps=1000).finished:
        pass
    session.end(); session.report()
assert session.returncode == 0
retired(connections[-1])

# Loading failures must terminate the actual interpreter, not only a redirector.
broken = root/'broken.dll'; broken.write_bytes(b'not a native library')
try:
    StandardBackend(library=str(broken)).session(working_directory=root)
except SessionError as error:
    assert error.failure.stage == 'load', error
else:
    raise AssertionError('Broken library loaded')
retired(connections[-1])

# Force a native-session call to block, then exercise timeout and cancellation.
blocking = True
for cancelled in (False, True):
    event = threading.Event()
    session = backend.session(working_directory=root, cancel_event=event)
    session.open('model.inp', 'blocked.rpt', 'blocked.out', overwrite=True)
    session.start()
    session.call_timeout = 5 if cancelled else .15
    timer = threading.Timer(.15, event.set) if cancelled else None
    if timer: timer.start()
    try:
        session.step()
    except (SessionTimeout, SessionCancelled) as error:
        assert isinstance(error, SessionCancelled if cancelled else SessionTimeout), error
        assert error.returncode is not None
    else:
        raise AssertionError('Blocked step completed')
    finally:
        if timer: timer.cancel(); timer.join()
        session.close()
    retired(connections[-1])
blocking = False

# Explicit force termination and the following failed RPC both reap cleanly.
session = backend.session(working_directory=root)
connection = connections[-1]
connection.process.kill()
connection.process.wait(timeout=10)
try:
    session.close()
except SessionError:
    pass
retired(connection)

# A fresh recovery process refuses an active interpreter, then safely removes
# the workspace after that exact creation identity has demonstrably exited.
target = root/'previous.out'; target.write_bytes(b'previous result')
transaction = OutputTransaction('venv-identity', overwrite=True)
transaction.reserve(((target, False),))
workspace = Workspace(root, 'venv-identity')
(workspace.root/'partial').write_bytes(b'partial')
transaction.track_workspace(workspace, cleanup_on_crash=True)
transaction.workspace_execution(workspace, state='starting')
session = backend.session(working_directory=workspace.root)
transaction.workspace_execution(workspace, state='active', identity=session.worker_identity)
transaction.journal.lease.close()
def recover():
    import subprocess
    code = ('import json,sys; from easysewer.runtime import recover_run; '
            'r=recover_run(sys.argv[1]); print(json.dumps(dict(state=r.state,issues=r.issues,cleaned=r.cleaned_workspaces)))')
    process = subprocess.run([sys.executable, '-I', '-B', '-c', code, str(transaction.journal.path)],
                             capture_output=True, text=True, timeout=20)
    assert process.returncode == 0, process.stderr
    return json.loads(process.stdout)
first = recover()
assert first['state'] == 'conflicted', first
assert any('worker is still active' in issue for issue in first['issues']), first
assert workspace.root.exists()
session.close(); retired(connections[-1])
last = recover()
assert last['state'] == 'recovered', last
assert last['cleaned'] == [str(workspace.root)], last
assert not workspace.root.exists()
assert target.read_bytes() == b'previous result'
assert all(c.process.poll() is not None for c in connections)
print(json.dumps(dict(mode=mode, workers=len(connections), recovered=True,
                     normal=True, load_failure=True, timeout=True, cancellation=True, forced_exit=True)))
"""


@unittest.skipUnless(get_native_capabilities()['swmm_solver'], 'Native solver unavailable')
class NativeVenvWorkerTests(unittest.TestCase):
    def exercise(self, mode):
        with tempfile.TemporaryDirectory(prefix='easysewer-venv-') as directory:
            root = Path(directory).resolve()
            environment = root / 'venv'
            venv.EnvBuilder(with_pip=False).create(environment)
            executable = environment / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
            location = subprocess.run([str(executable), '-I', '-B', '-c',
                                       'import sysconfig; print(sysconfig.get_path("purelib"))'],
                                      capture_output=True, text=True, check=True, timeout=20)
            site = Path(location.stdout.strip())
            shutil.copytree(Path(easysewer.__file__).resolve().parent, site / 'easysewer',
                            ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
            (root / 'model.inp').write_text(INP, encoding='utf-8')
            if mode != 'clean':
                (site / 'venv_only_dependency.py').write_text("TOKEN = 'venv-only'\n")
                extension = root / 'extension'
                extension.mkdir()
                (extension / 'pth_only_dependency.py').write_text("TOKEN = 'pth-only'\n")
                (site / 'extension.pth').write_text(str(extension) + '\n', encoding='utf-8')
            process = subprocess.run([str(executable), '-I', '-B', '-c', CHILD, str(root), mode],
                                     capture_output=True, text=True, timeout=90,
                                     creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            self.assertEqual(process.returncode, 62 if mode == 'parent-exit' else 0,
                             process.stdout + '\n' + process.stderr)
            if mode == 'parent-exit':
                identity = json.loads((root / 'abandoned.json').read_text())
                deadline = time.monotonic() + 15
                while not has_exited(identity) and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertTrue(has_exited(identity), 'Worker survived parent pipe closure')
            else:
                value = json.loads(process.stdout)
                self.assertEqual(value['mode'], mode)
                self.assertGreater(value['workers'], 0)
                return value

    def test_clean_venv_probe_model_run_and_out_read(self):
        self.assertEqual(self.exercise('clean')['periods'], 10)

    def test_venv_dependencies_and_real_worker_lifecycle(self):
        value = self.exercise('lifecycle')
        for field in ('recovered', 'normal', 'load_failure', 'timeout', 'cancellation', 'forced_exit'):
            self.assertTrue(value[field], field)

    def test_parent_exit_retires_real_venv_worker(self):
        self.exercise('parent-exit')


if __name__ == '__main__':
    unittest.main()
