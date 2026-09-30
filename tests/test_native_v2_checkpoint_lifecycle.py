"""Private lifecycle integration using real coordinator ABI 2 libraries."""
import hashlib
from dataclasses import replace
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from easysewer.runtime._checkpoint_lifecycle import CheckpointLifecycle
from easysewer.runtime._native_solver import NativeSolver
from easysewer.runtime._native_flexible import NativeFlexibleSolver
from easysewer.runtime import _checkpoint_worker as worker
import test_native_v2_checkpoint_container as fixture

EVIDENCE = []


def prepare(root, family, case):
    kind = NativeSolver if family == 'standard' else NativeFlexibleSolver
    with patch.object(fixture, 'open_solver', side_effect=lambda family, snapshot: kind(
            os.environ['EASYSEWER_CHECKPOINT_'+family.upper()])):
        snapshot, solver = fixture.prepare(root, family, case)
    return CheckpointLifecycle(solver, snapshot)


def finish(lifecycle):
    solver = lifecycle.solver
    outputs = lifecycle.api.outputs()
    trace = Path(solver.trace.name) if lifecycle.custom and solver.trace else None
    for _ in range(20000):
        if lifecycle.step(3)['finished']: break
    else: raise AssertionError('Did not finish')
    balance = lifecycle.end(); lifecycle.report()
    result = solver.execution_results() if lifecycle.custom else None
    assert not lifecycle.cleanup()
    hashes = {}
    for item in outputs:
        raw = Path(item.path).read_bytes()
        if item.role == 0: raw = re.sub(rb'(?m)^[ \t]*(?:Analysis (?:begun|ended)|Total elapsed time)[^\r\n]*', b'', raw)
        if item.text: raw = raw.replace(os.fsencode(Path.cwd()), b'<workspace>')
        hashes[str(item.index)] = dict(role=item.role, sha256=hashlib.sha256(raw).hexdigest(), size=len(raw))
    return dict(outputs=hashes, trace=hashlib.sha256(trace.read_bytes()).hexdigest() if trace else None,
                balance=balance, results=result)


@unittest.skipUnless(os.environ.get('EASYSEWER_CHECKPOINT_STANDARD') and os.environ.get('EASYSEWER_CHECKPOINT_CUSTOM'),
                     'Requires clean ABI2 candidate libraries')
class CheckpointLifecycleTests(unittest.TestCase):
    def test_fresh_process_reconstructs_startup_ledger_and_resumes(self):
        child = '''import json,os,sys
from pathlib import Path
from easysewer.runtime._checkpoint_container import load
from easysewer.runtime._checkpoint_lifecycle import CheckpointLifecycle
from test_native_v2_checkpoint_lifecycle import NativeSolver,NativeFlexibleSolver,fixture,finish
archive=load(sys.argv[1]);root=Path(sys.argv[2]);family=sys.argv[3]
snapshot=archive.materialize(root)
with fixture.working(root):
 solver=(NativeSolver if family=='standard' else NativeFlexibleSolver)(os.environ['EASYSEWER_CHECKPOINT_'+family.upper()])
 lifecycle=CheckpointLifecycle(solver,snapshot)
 try:
  lifecycle.open_start();result=lifecycle.restore(archive)
  assert result.native.committed and result.native.error==0 and not result.cleanup
  lifecycle.capture(root/'recaptured')
  (root/'result.json').write_text(json.dumps(finish(lifecycle)))
 finally:lifecycle.cleanup()
'''
        for family,case in (('standard','combined'),('custom','normal'),('custom','climate')):
            with self.subTest(family=family,case=case),tempfile.TemporaryDirectory() as directory:
                root=Path(directory);first=root/'first';first.mkdir()
                with fixture.working(first):
                    lifecycle=prepare(first,family,case)
                    try:
                        lifecycle.open_start();lifecycle.step(3);lifecycle.capture(root/'saved')
                        expected=finish(lifecycle)
                    finally:lifecycle.cleanup()
                shutil.rmtree(first);(root/'saved').rename(root/'moved')
                env=dict(os.environ,PYTHONPATH=os.pathsep.join((str(Path(__file__).parent),str(Path(__file__).parents[1]/'src'))))
                result=subprocess.run([sys.executable,'-B','-c',child,str(root/'moved'),str(root/'second'),family],
                                      env=env,capture_output=True,text=True,timeout=90,
                                      creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                self.assertEqual(result.returncode,0,result.stdout+result.stderr)
                self.assertEqual(json.loads((root/'second/result.json').read_bytes()),expected)
                EVIDENCE.append(dict(kind='fresh-lifecycle',family=family,case=case,result=expected))

    def test_repeated_restore_and_capture_use_replacement_input_paths(self):
        for family, case in (('standard','all'), ('standard','combined'), ('custom','normal'), ('custom','climate')):
            values = []
            for restored in (False, True):
                with self.subTest(family=family,case=case,restored=restored),tempfile.TemporaryDirectory() as directory,fixture.working(directory):
                    root = Path(directory); lifecycle = prepare(root, family, case)
                    try:
                        lifecycle.open_start(); lifecycle.step(3)
                        if case in ('combined','climate'): self.assertTrue(lifecycle.inputs)
                        saved = lifecycle.capture(root/'saved')
                        lifecycle.step(2)
                        second = lifecycle.capture(root/'second')
                        if restored:
                            outcome = lifecycle.restore(saved)
                            self.assertTrue(outcome.native.committed)
                            self.assertEqual((outcome.native.error,outcome.native.cleanup_error,outcome.cleanup),(0,0,()))
                            self.assertTrue(all('.checkpoint-restore-' in str(v.path) for v in lifecycle.inputs.values()))
                            lifecycle.step(2)
                            repeated = lifecycle.capture(root/'repeated')
                            self.assertEqual(repeated.native_state,second.native_state)
                            self.assertEqual(repeated.worker_state,second.worker_state)
                            outcome = lifecycle.restore(second)
                            self.assertTrue(outcome.native.committed)
                            self.assertEqual((outcome.native.error,outcome.native.cleanup_error,outcome.cleanup),(0,0,()))
                        values.append(finish(lifecycle))
                    finally: lifecycle.cleanup()
            self.assertEqual(values[0],values[1])
            EVIDENCE.append(dict(kind='repeat-restore',family=family,case=case,result=values[0]))

    def test_startup_input_tampering_and_late_binding_are_rejected(self):
        for mode in ('before-open','after-start','reverted-after-start','late-binding'):
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as directory,fixture.working(directory):
                root=Path(directory);lifecycle=prepare(root,'standard','combined')
                try:
                    if mode=='before-open':
                        path=root/'model.inp';path.write_bytes(path.read_bytes()+b'\n')
                        with self.assertRaisesRegex(ValueError,'snapshot'):lifecycle.open_start()
                        self.assertTrue(lifecycle.solver.closed)
                    else:
                        lifecycle.open_start()
                        if mode=='late-binding':
                            with self.assertRaisesRegex(ValueError,'before open'):
                                CheckpointLifecycle(lifecycle.solver,lifecycle.snapshot)
                        else:
                            self.assertTrue(lifecycle.inputs)
                            path=next(iter(lifecycle.inputs.values())).path;raw=path.read_bytes()
                            with path.open('r+b') as stream:stream.write(bytes([raw[0]^1]))
                            if mode=='reverted-after-start':
                                with path.open('r+b') as stream:stream.write(raw[:1])
                            with self.assertRaisesRegex(ValueError,'changed'):lifecycle.capture(root/'rejected')
                            self.assertFalse((root/'rejected').exists())
                            with self.assertRaisesRegex(ValueError,'changed'):lifecycle.step()
                            self.assertEqual(lifecycle.state,'FAILED')
                    EVIDENCE.append(dict(kind='input-rejection',mode=mode))
                finally:lifecycle.cleanup()

    def test_cancelled_restore_and_reentrant_capture_leave_live_state_unchanged(self):
        with tempfile.TemporaryDirectory() as directory,fixture.working(directory):
            root=Path(directory);lifecycle=prepare(root,'custom','normal')
            try:
                lifecycle.open_start();lifecycle.step(3);saved=lifecycle.capture(root/'saved')
                lifecycle.step(2);native=lifecycle.api.capture();python=worker.capture(lifecycle.solver)
                with self.assertRaisesRegex(ValueError,'verified storage'):
                    lifecycle.restore(replace(saved,native_state=native))
                calls=0
                def cancel():
                    nonlocal calls
                    if not list(root.glob('.checkpoint-restore-*')): return
                    calls+=1
                    if calls==3:raise KeyboardInterrupt('cancel restoration')
                with self.assertRaises(KeyboardInterrupt):lifecycle.restore(saved,checkpoint=cancel)
                self.assertFalse(list(root.glob('.checkpoint-restore-*')))
                self.assertEqual(lifecycle.api.capture(),native)
                self.assertEqual(worker.capture(lifecycle.solver),python)
                def reenter():lifecycle.step()
                with self.assertRaisesRegex(ValueError,'already in progress'):
                    lifecycle.capture(root/'reentrant',checkpoint=reenter)
                self.assertFalse((root/'reentrant').exists())
                self.assertEqual(lifecycle.api.capture(),native)
                self.assertEqual(worker.capture(lifecycle.solver),python)
                finish(lifecycle)
                EVIDENCE.append(dict(kind='rejection-keeps-session'))
            finally:lifecycle.cleanup()
