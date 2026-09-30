"""Runner envelope over actual public Session state; Runner orchestration is separate."""
from dataclasses import replace
from datetime import datetime, timezone
import os
from pathlib import Path
import shutil
import tempfile
import unittest

from easysewer.io.json import JsonDocument
from easysewer.runtime import BackendArtifact, CacheConsumption, RunContinuation, RunResult, swmm_cache_policies
from easysewer.runtime._runner_checkpoint import RunnerContext, capture, load
from easysewer.validation import Diagnostic, Severity, ValidationReport
import test_native_v2_checkpoint_lifecycle as fixture
from test_native_v2_checkpoint_rpc import backend
from test_native_v2_checkpoint_session import finish
import test_native_v2_result_archive as result_fixture
from test_runner_checkpoint_context_v2 import continuation

EVIDENCE=[]


def context(snapshot,steps):
    policies=swmm_cache_policies()
    kinds={r.role.rsplit('.',1)[1].upper() for r in snapshot.resources if r.active and r.role.startswith('swmm:interface.')}
    contexts={kind:policies.capture(kind,snapshot) for kind in sorted(kinds)}
    consumed=[]
    for row in snapshot.resources:
        if row.active and row.access!='write' and row.role.startswith('swmm:interface.'):
            kind=row.role.rsplit('.',1)[1].upper()
            consumed.append(CacheConsumption(kind=kind,sha256=row.sha256,producer_run_id=None,
                producer_input_sha256=None,producer_model_sha256=None,producer_engine_sha256=None,
                consumer_model_sha256=snapshot.model_sha256,verification='format-only',
                reuse=policies.assess(None,contexts[kind],cache_sha256=row.sha256,intent='inspect')))
    trace=JsonDocument.from_bytes(snapshot.backend_settings).data['trace'] if snapshot.backend_settings else None
    products=(BackendArtifact(role='easysewer:flexible-ponding-steps',relative_path=trace),) if trace else ()
    return RunnerContext(asset_directory='assets',steps=steps,cache_contexts=tuple(contexts.values()),
        consumed_caches=tuple(consumed),backend_artifacts=products,
        diagnostics=ValidationReport(diagnostics=(Diagnostic(code='test:preserved',message='original warning',severity=Severity.WARNING),)))


@unittest.skipUnless(os.environ.get('EASYSEWER_CHECKPOINT_STANDARD') and os.environ.get('EASYSEWER_CHECKPOINT_CUSTOM'),
                     'Requires ABI2 candidate libraries')
class NativeRunnerCheckpointContextTests(unittest.TestCase):
    def test_actual_state_and_runner_context_survive_move_and_repeat_resume(self):
        for family,case in (('standard','all'),('standard','combined'),('custom','normal'),('custom','climate')):
            with self.subTest(family=family,case=case),tempfile.TemporaryDirectory() as directory:
                root=Path(directory);first=root/'first';first.mkdir()
                with fixture.fixture.working(first):
                    owner=fixture.prepare(first,family,case);snapshot=owner.snapshot;owner.cleanup()
                with backend(family).session(working_directory=first) as session:
                    session.open_checkpoint(snapshot);session.step(max_steps=3)
                    value=context(snapshot,3)
                    saved=capture(session,value,root/'saved')
                    expected=finish(session,family=='custom')
                shutil.rmtree(first);(root/'saved').rename(root/'moved');saved=load(root/'moved')
                self.assertEqual(saved.context,value)
                rebuilt=saved.materialize(root/'second')
                history=RunContinuation(attempt_id='second',execution_run_id=snapshot.run_id,
                    checkpoint_sha256=saved.sha256,state_sha256=saved.state.sha256,
                    execution_sha256=saved.state._archive.binding.hex(),started_at=datetime.now(timezone.utc),
                    simulation_seconds=saved.simulation_seconds,steps=value.steps,config_json=snapshot.config_json)
                with backend(family).session(working_directory=root/'second') as session:
                    session.open_checkpoint(rebuilt);session.restore_checkpoint(saved.state)
                    resumed=replace(value,continuations=(history,))
                    second=capture(session,resumed,root/'second-saved')
                    self.assertEqual(second.context,resumed)
                    observed=finish(session,family=='custom')
                self.assertEqual(observed,expected)
                shutil.rmtree(root/'second');rebuilt=second.materialize(root/'third')
                with backend(family).session(working_directory=root/'third') as session:
                    session.open_checkpoint(rebuilt);session.restore_checkpoint(second.state)
                    self.assertEqual(finish(session,family=='custom'),expected)
                EVIDENCE.append(dict(kind='runner-envelope',family=family,case=case,
                    cache_kinds=[v.kind for v in value.cache_contexts],consumed=len(value.consumed_caches),
                    result=observed,continuation_sha256=second.sha256))

    def test_missing_cache_or_trace_context_is_refused_while_session_can_continue(self):
        for family,case in (('standard','all'),('custom','normal')):
            with self.subTest(family=family),tempfile.TemporaryDirectory() as directory:
                root=Path(directory)
                with fixture.fixture.working(root):
                    owner=fixture.prepare(root,family,case);snapshot=owner.snapshot;owner.cleanup()
                with backend(family).session(working_directory=root) as session:
                    session.open_checkpoint(snapshot);session.step(max_steps=3);value=context(snapshot,3)
                    if family=='standard':
                        self.assertTrue(value.cache_contexts);value=replace(value,cache_contexts=())
                    else:
                        self.assertTrue(value.backend_artifacts);value=replace(value,backend_artifacts=())
                    with self.assertRaises(ValueError):capture(session,value,root/'rejected')
                    self.assertFalse((root/'rejected').exists())
                    self.assertEqual(session.state,'STARTED');session.step(max_steps=2)
                    self.assertIsNone(session.returncode)
                EVIDENCE.append(dict(kind='context-omission',family=family))

    def test_successful_result_11_retains_complete_native_outputs_and_history(self):
        for family in ('swmm:standard','easysewer:flexible-ponding'):
            with self.subTest(family=family),tempfile.TemporaryDirectory() as directory:
                root=Path(directory);test=result_fixture.NativeResultArchiveTests()
                value=test.solve(root,family,cache=True)
                # This record exercises result serialization, not proof that
                # this fresh Runner execution actually resumed a checkpoint.
                history=(continuation(value.snapshot),)
                value=replace(value,continuations=history)
                value.save(root/'archive');loaded=RunResult.load(root/'archive')
                test.assert_same(value,loaded)
                self.assertEqual(loaded.continuations,history)
                EVIDENCE.append(dict(kind='result-11',family=family,synthetic_resume_history=True))
