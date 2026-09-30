"""Native completion followed by interrupted execution-record construction."""
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from easysewer import get_native_capabilities
from easysewer.io.json import document as jd
from easysewer.io.report import read_report_tables
from easysewer.io.report_document import ReportDocument
from easysewer.results import tables
from easysewer.runtime import Runner, RunResult
from easysewer.runtime import runner as rm
from test_options_v2 import network
from test_runner_v2 import config
from test_report_cancel_v2 import large_depth


EVIDENCE = []


@unittest.skipUnless(get_native_capabilities()['swmm_solver'], 'Native solver unavailable')
class NativeRecordCancellationTests(unittest.TestCase):
    def test_prepublication_record_cancel_and_deadline_preserve_outputs(self):
        # Enlarge only the private record's table payload. The actual native
        # INP/RPT/OUT files stay intact. This is a cancellation fixture, not a
        # claim that this table came from the small native network.
        table, = read_report_tables(ReportDocument.from_bytes(large_depth(1200)), ('swmm:node_depth',))
        stages = ((tables, 'record_asdict'), (tables, 'record_dumps'),
                  (jd, '_pairs'), (jd, 'check_json'), (rm, 'write_record_bytes'))
        for backend in ('swmm:standard', 'easysewer:flexible-ponding'):
            for module, name in stages:
                for status in ('cancelled', 'timed_out'):
                    for keep in (False, True):
                        with self.subTest(backend=backend, phase=name, status=status, keep=keep), tempfile.TemporaryDirectory() as folder:
                            root = Path(folder); target = root/'out'; target.mkdir()
                            for filename in ('model.inp', 'model.rpt', 'model.out'):
                                (target/filename).write_bytes(b'previous')
                            event = threading.Event(); active = []; recording = []; checks = []; triggered = []
                            original_check = rm._Cancellation.check
                            original_record = Runner._execution_record
                            original_stage = getattr(module, name)
                            def record(*args, **kwargs):
                                args = list(args); args[9] = (table,); recording.append(True)
                                try: return original_record(*args, **kwargs)
                                finally: recording.pop()
                            def entered(*args, **kwargs):
                                active.append(True)
                                try: return original_stage(*args, **kwargs)
                                finally: active.pop()
                            def check(control):
                                if recording and active:
                                    checks.append(1)
                                    if len(checks) == 3:
                                        triggered.append(time.monotonic())
                                        if status == 'cancelled': event.set()
                                        else: control.deadline = time.monotonic() - 1
                                return original_check(control)
                            model = network()
                            if backend == 'easysewer:flexible-ponding':
                                model.update_options(flow_routing='DYNWAVE', allow_ponding=True)
                            with patch.object(Runner, '_execution_record', staticmethod(record)), \
                                 patch.object(module, name, entered), patch.object(rm._Cancellation, 'check', check):
                                result = Runner().run(model, config(target, backend=backend, overwrite=True,
                                    keep_failed_artifacts=keep), cancel_event=event)
                            returned = time.monotonic()
                            self.assertEqual(result.status, status, result.failure)
                            self.assertEqual(result.failure.stage, 'publication')
                            self.assertTrue(result.native_completed); self.assertEqual(len(checks), 3)
                            self.assertTrue(triggered)
                            for filename in ('model.inp', 'model.rpt', 'model.out'):
                                self.assertEqual((target/filename).read_bytes(), b'previous')
                            self.assertFalse(list(target.glob('.easysewer-lock-*')))
                            self.assertEqual(result.retained_directory is not None, keep)
                            if keep:
                                for artifact in result.artifacts: artifact.read_bytes()
                            else: self.assertFalse(result.artifacts)
                            result.save(root/'saved'); loaded = RunResult.load(root/'saved')
                            self.assertEqual(loaded.failure, result.failure)
                            self.assertEqual(loaded.failure_report, result.failure_report)
                            EVIDENCE.append(dict(backend=backend, phase=name, status=status, retained=keep,
                                old_outputs_preserved=True, native_completed=True,
                                return_seconds=returned-triggered[0], checkpoints_in_stage=len(checks)))


if __name__ == '__main__': unittest.main()
