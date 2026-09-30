"""Runner publication conflicts after successful native completion."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from easysewer import get_native_capabilities
from easysewer.runtime import Runner
from easysewer.runtime import _workspace as workspace
from test_options_v2 import network
from test_runner_v2 import config
from test_output_ownership_v2 import fixed_stamps


EVIDENCE = []


@unittest.skipUnless(get_native_capabilities()['swmm_solver'], 'Native solver unavailable')
class NativeOutputOwnershipTests(unittest.TestCase):
    def test_completed_run_rejects_external_same_stamp_edit_for_both_backends(self):
        for backend in ('swmm:standard', 'easysewer:flexible-ponding'):
            with self.subTest(backend=backend), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve(); target = root/'model.out'
                model = network()
                if backend == 'easysewer:flexible-ponding': model.update_options(flow_routing='DYNWAVE', allow_ponding=True)
                previous = Runner().run(model, config(root, backend=backend))
                self.assertTrue(previous.succeeded, previous.failure)
                original = target.read_bytes(); edited = b'X'*len(original)
                baseline = {target: workspace.fingerprint(target)}; called = []
                def edit(progress):
                    if progress.phase == 'finalizing': target.write_bytes(edited); called.append(True)
                with fixed_stamps(baseline):
                    result = Runner().run(model, config(root, backend=backend, overwrite=True,
                        keep_failed_artifacts=False), progress=edit)
                self.assertEqual(called, [True]); self.assertTrue(result.native_completed)
                self.assertEqual(result.status, 'failed'); self.assertEqual(result.failure.stage, 'publication')
                self.assertIn('content changed after reservation', result.failure.message)
                self.assertEqual(target.read_bytes(), edited)
                self.assertEqual(previous.input.read_bytes(), Path(previous.input.path).read_bytes())
                self.assertEqual(previous.report.read_bytes(), Path(previous.report.path).read_bytes())
                self.assertFalse(list(root.glob('.easysewer-*')))
                EVIDENCE.append(dict(case='completed-reservation-conflict', backend=backend,
                    status=result.status, native_completed=result.native_completed, protected_bytes=len(edited)))

    def test_partial_publish_retains_external_edit_backup_and_primary_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            previous = Runner().run(network(), config(root)); self.assertTrue(previous.succeeded, previous.failure)
            inp = Path(previous.input.path); out = Path(previous.output.path); rpt = Path(previous.report.path)
            original_out = out.read_bytes(); original_rpt = rpt.read_bytes(); original_inp = inp.read_bytes()
            native_replace = os.replace; stamps = {}; edited = b'X'*len(original_out)
            def replacing(src, dst):
                if Path(dst) == rpt and Path(src).name.startswith('.easysewer-publish-'):
                    stamps[out] = workspace.fingerprint(out); out.write_bytes(edited)
                    raise OSError('report publish deliberately failed')
                return native_replace(src, dst)
            with fixed_stamps(stamps), patch.object(workspace.os, 'replace', side_effect=replacing):
                result = Runner().run(network(), config(root, overwrite=True, keep_failed_artifacts=False))
            self.assertTrue(result.native_completed); self.assertEqual(result.status, 'failed')
            self.assertIn('report publish deliberately failed', result.failure.message)
            self.assertEqual(out.read_bytes(), edited); self.assertEqual(rpt.read_bytes(), original_rpt)
            self.assertEqual(inp.read_bytes(), original_inp)
            self.assertEqual([p.read_bytes() for p in root.glob('.easysewer-backup-*')], [original_out])
            self.assertTrue(any('Published output changed' in d.message for d in result.diagnostics.diagnostics))
            self.assertTrue(list(root.glob('.easysewer-lock-*')))
            from easysewer.runtime import inspect_run_recovery
            journal, = root.glob('.easysewer-recovery-*.json')
            recovery = inspect_run_recovery(journal)
            self.assertEqual(recovery.state, 'interrupted')
            self.assertEqual(recovery.failure, (result.failure.stage, result.failure.exception_type, result.failure.message))
            EVIDENCE.append(dict(case='completed-partial-rollback-conflict', status=result.status,
                native_completed=result.native_completed, backup_bytes=len(original_out)))
