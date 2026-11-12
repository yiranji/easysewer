"""Packaged accelerated Runner output and checkpoint identity integration."""
from datetime import time, timedelta
from pathlib import Path
import tempfile
import unittest

from easysewer import get_native_capabilities
from easysewer.model import FileReference
from easysewer.model.report import ReportSelection
from easysewer.runtime import (AcceleratedBackend, CheckpointRejected, CheckpointSchedule,
                              ResumeConfig, Runner, StandardBackend)
from test_options_v2 import network
from test_runner_v2 import config


def output_directory(path):
    return FileReference(path=str(path), direction='output')


@unittest.skipUnless(all(get_native_capabilities()[name] for name in
    ('swmm_solver', 'swmm_accelerated', 'swmm_output')), 'Packaged standard, accelerated and output libraries required')
class NativeAcceleratedRuntimeTests(unittest.TestCase):
    def assert_success(self, result):
        self.assertTrue(result.succeeded, (result.failure, result.diagnostics))
        self.assertTrue(result.native_completed)

    def test_runner_output_resume_and_cross_backend_rejection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backends = {'swmm:standard': StandardBackend(), 'swmm:accelerated': AcceleratedBackend()}
            results, saved = {}, {}
            for key in backends:
                label = key.split(':')[1]
                model = network()
                model.update_options(end_time=time(0, 2), threads=4, report_step=timedelta(seconds=30))
                model.update_report(nodes=ReportSelection(mode='ALL'), links=ReportSelection(mode='ALL'))
                saved[key] = []
                # The standard run deliberately exercises the public default.
                options = {} if key == 'swmm:standard' else dict(backend=key)
                result = Runner().run(model, config(root / label, step_batch_size=3, **options),
                    checkpoints=CheckpointSchedule(directory=output_directory(root / (label + '_saved')),
                        interval=timedelta(seconds=60), on_saved=saved[key].append))
                self.assert_success(result)
                self.assertEqual(result.backend.key, key)
                self.assertTrue(saved[key])
                results[key] = result
            self.assertEqual(results['swmm:standard'].output.read_bytes(),
                             results['swmm:accelerated'].output.read_bytes())
            self.assertEqual(results['swmm:standard'].backend.numerical_policy, 'easysewer:standard:5.2.4:16')
            self.assertEqual(results['swmm:accelerated'].backend.numerical_policy, 'easysewer:accelerated:5.2.4:1')
            self.assertEqual(results['swmm:accelerated'].backend.abi,
                             results['swmm:standard'].backend.abi + ':accelerated:1')
            for key, backend in backends.items():
                label = key.split(':')[1]
                checkpoint = saved[key][0]
                other_key = 'swmm:standard' if key == 'swmm:accelerated' else 'swmm:accelerated'
                with self.subTest(backend=key):
                    resumed = Runner().resume(checkpoint, ResumeConfig(
                        output_directory=output_directory(root / (label + '_resumed')), step_batch_size=4))
                    self.assert_success(resumed)
                    self.assertEqual(resumed.backend.key, key)
                    self.assertEqual(resumed.output.read_bytes(), results[key].output.read_bytes())
                    # Registration under the archived key cannot substitute a
                    # different implementation when restoring an execution.
                    rejected = Runner(backends={key: backends[other_key]}).resume(checkpoint,
                        ResumeConfig(output_directory=output_directory(root / (label + '_rejected'))))
                    self.assertEqual(rejected.status, 'rejected', rejected.failure)
                    self.assertIn('run.checkpoint_backend',
                                  {item.code for item in rejected.diagnostics.diagnostics})
                    work = root / (label + '_session')
                    snapshot = checkpoint.state.materialize(work)
                    with backend.session(working_directory=work) as session:
                        session.open_checkpoint(snapshot)
                        session.step(max_steps=1)
                        with self.assertRaises(CheckpointRejected):
                            session.restore_checkpoint(saved[other_key][0].state)
                        self.assertEqual(session.state, 'STARTED')
                        session.restore_checkpoint(checkpoint.state)
                        session.step(max_steps=1)


if __name__ == '__main__':
    unittest.main()
