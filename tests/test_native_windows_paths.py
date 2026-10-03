"""Real Windows aliases keep native execution in the selected owned directory."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest

from easysewer.model import FileReference
from easysewer.runtime import Checkpoint, Runner
from easysewer.runtime._native_paths import worker_directory
from test_options_v2 import network
from test_runner_v2 import config
from test_flexible_v2 import configuration, ponding_model
import native_directory_lifecycle_fixture as input_fixture
import native_output_checkpoint_fixture as output_fixture
from test_native_public_directory_checkpoint_v2 import finish as finish_inputs
from test_native_output_directory_checkpoint_v2 import finish as finish_outputs


@unittest.skipUnless(os.name == 'nt', 'Requires actual Windows short-path APIs')
class WindowsNativePathTests(unittest.TestCase):
    def require_alias(self, directory):
        directory = Path(directory).resolve()
        alias = str(worker_directory(directory))
        self.assertTrue(alias.isascii(),
            'This Windows compatibility gate requires an existing ASCII short-name alias: '+alias)
        self.assertTrue(Path(alias).samefile(directory))
        self.assertEqual(Path(alias).resolve(), directory)
        return alias

    def assert_success(self, result):
        self.assertTrue(result.succeeded, repr(result.failure))
        self.assertTrue(result.native_completed)
        self.assertIsNone(result.retained_directory)
        self.assertFalse(Path(result.snapshot.execution_directory).exists())

    def test_worker_keeps_ascii_alias_and_runner_matches_both_backends(self):
        original_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            unicode_root = root/'中文😀'; unicode_root.mkdir()
            alias = self.require_alias(unicode_root)
            observed = subprocess.run([sys.executable, '-I', '-B', '-c',
                'import json, os; print(json.dumps([os.getcwd(), os.path.abspath("model.inp")]))'],
                cwd=alias, check=True, capture_output=True, text=True, timeout=15,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            cwd, absolute_input = json.loads(observed.stdout)
            self.assertTrue(cwd.isascii(), cwd)
            self.assertTrue(absolute_input.isascii(), absolute_input)
            self.assertTrue(Path(cwd).samefile(unicode_root))
            for family, model, settings in (
                ('standard', network(), config), ('custom', ponding_model(), configuration),
            ):
                with self.subTest(family=family):
                    baseline = Runner().run(model, settings(root/(family+'-ascii')))
                    result = Runner().run(model, settings(unicode_root/family))
                    self.assert_success(baseline); self.assert_success(result)
                    self.assertEqual(Path(result.snapshot.execution_directory).parent,
                                     (unicode_root/family).resolve())
                    self.assertEqual(result.output.read_bytes(), baseline.output.read_bytes())
                    if family == 'custom':
                        self.assertIsNotNone(result.backend_results)
                        self.assertTrue(result.artifact('easysewer:flexible-ponding-steps').read_bytes())
        self.assertEqual(Path.cwd(), original_cwd)

    def test_explicit_unicode_scratch_stays_selected_and_cancellation_cleans_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            for family, model, settings in (
                ('standard', network(), config), ('custom', ponding_model(), configuration),
            ):
                with self.subTest(family=family):
                    scratch = root/(family+'-中文😀'); scratch.mkdir()
                    self.require_alias(scratch)
                    marker = scratch/'caller-owned'; marker.write_bytes(b'keep')
                    model.update_options(temp_directory=FileReference(path=str(scratch), direction='output'))
                    destination = root/(family+'-published')
                    result = Runner().run(model, settings(destination))
                    self.assert_success(result)
                    self.assertEqual(Path(result.snapshot.execution_directory).parent, scratch)
                    previous = {name:(destination/name).read_bytes()
                                for name in ('model.inp', 'model.rpt', 'model.out')}
                    event = threading.Event()
                    def cancel(progress):
                        if progress.phase == 'running': event.set()
                    cancelled = Runner().run(model, settings(destination, overwrite=True,
                        step_batch_size=1, keep_failed_artifacts=False), progress=cancel, cancel_event=event)
                    self.assertEqual(cancelled.status, 'cancelled')
                    self.assertFalse(cancelled.native_completed)
                    self.assertEqual(Path(cancelled.snapshot.execution_directory).parent, scratch)
                    self.assertFalse(Path(cancelled.snapshot.execution_directory).exists())
                    self.assertIsNone(cancelled.retained_directory)
                    self.assertEqual(list(scratch.iterdir()), [marker])
                    self.assertEqual(marker.read_bytes(), b'keep')
                    for name, data in previous.items():
                        self.assertEqual((destination/name).read_bytes(), data)
                    self.assertFalse(list(destination.glob('.easysewer-*')))

    def test_unicode_checkpoint_move_repeat_restore_and_output_bytes(self):
        for family in ('standard', 'custom'):
            for kind in ('inputs', 'outputs'):
                with self.subTest(family=family, kind=kind), tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary).resolve()
                    first = root/'初始😀'; first.mkdir()
                    self.require_alias(first)
                    if kind == 'inputs':
                        _, work, snapshot = input_fixture.prepare(first, family)
                        finish = finish_inputs
                    else:
                        work, snapshot = output_fixture.prepare(first, family)
                        finish = finish_outputs
                    backend = input_fixture.backend(family)
                    with backend.session(working_directory=work) as session:
                        self.assertEqual(session.working_directory, work.resolve())
                        session.open_checkpoint(snapshot, schema=input_fixture.schema())
                        session.step(max_steps=24)
                        saved = session.save_checkpoint(root/'保存😀')
                        expected = finish(session)
                    shutil.rmtree(first)
                    saved.directory.rename(root/'移动😀')
                    saved = Checkpoint.load(root/'移动😀')
                    restored_root = root/'恢复😀'
                    snapshot = saved.materialize(restored_root, schema=input_fixture.schema())
                    self.require_alias(restored_root)
                    with backend.session(working_directory=restored_root) as session:
                        session.open_checkpoint(snapshot, schema=input_fixture.schema())
                        for index in range(2):
                            session.step(max_steps=3)
                            restored = session.restore_checkpoint(saved)
                            self.assertTrue(restored.committed)
                            self.assertEqual(restored.cleanup, ())
                            for output in restored.outputs:
                                self.assertEqual(output.path, output.path.resolve())
                                self.assertTrue(output.path.is_relative_to(restored_root))
                            again = session.save_checkpoint(root/('再保存😀-'+str(index)))
                            self.assertEqual(again.simulation_seconds, saved.simulation_seconds)
                        self.assertEqual(finish(session), expected)
                    # Windows permits removing these directories only after
                    # the worker and its native file handles have been reaped.
                    shutil.rmtree(restored_root)


if __name__ == '__main__':
    unittest.main()
