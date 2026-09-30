"""Public 2.0 edit workflow compared to hand-written input through the actual C API."""
from datetime import timedelta
from dataclasses import fields
import hashlib
from pathlib import Path
import tempfile
import unittest

from easysewer import get_native_capabilities
from easysewer.model import FileReference, Ref
from easysewer.model.resources import FileTimeSeries
from easysewer.runtime import RunConfig, RunResult
from test_native_v2_output import compare_native
from test_edit_workflow_v2 import EDITED, example, external_rain_text

EVIDENCE = []
sha = lambda p: hashlib.sha256(Path(p).read_bytes()).hexdigest()


def config(directory):
    return RunConfig(output_directory=FileReference(path=str(directory), direction='output'),
                     wall_time_limit=timedelta(minutes=2))


def direct(test, directory, text):
    from easysewer.runtime._solver_api import SWMMSolverAPI
    directory.mkdir()
    inp, rpt, out = (directory/('oracle'+suffix) for suffix in ('.inp', '.rpt', '.out'))
    inp.write_text(text, encoding='utf-8')
    solver = SWMMSolverAPI()
    started = False
    try:
        test.assertEqual(solver.open(str(inp), str(rpt), str(out)), 0)
        test.assertEqual(solver.start(1), 0)
        started = True
        for _ in range(50000):
            code, elapsed = solver.step()
            test.assertEqual(code, 0)
            if not elapsed: break
        else: test.fail('Direct simulation did not finish')
        test.assertEqual(solver.end(), 0)
        started = False
        test.assertEqual(solver.report(), 0)
    finally:
        if started: solver.end()
        solver.close()
    return out, rpt


def stable_report(path):
    return b'\n'.join(line for line in Path(path).read_bytes().splitlines()
        if not line.strip().startswith((b'Analysis begun on:', b'Analysis ended on:', b'Total elapsed time:')))


@unittest.skipUnless(get_native_capabilities()['swmm_solver'] and get_native_capabilities()['swmm_output'],
                     'Native solver/output unavailable')
class NativeEditWorkflowTests(unittest.TestCase):
    def test_full_output_controls_all_variables_and_moved_archive(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = example.create_model()
            edited = example.edit_model(original)
            before = original.run(config(root/'original')).raise_for_status()
            actual = edited.run(config(root/'edited')).raise_for_status()
            self.assertLessEqual(abs(actual.mass_balance.flow_percent), 1)
            self.assertLessEqual(abs(actual.mass_balance.runoff_percent), 1)
            expected_out, expected_report = direct(self, root/'direct', EDITED)
            self.assertEqual(Path(actual.output.path).read_bytes(), expected_out.read_bytes())
            report = stable_report(actual.report.path)
            self.assertEqual(report, stable_report(expected_report))
            self.assertIn(b'Control Actions Taken', report)
            control_lines = [line.strip().decode('ascii') for line in report.splitlines() if b'Feeder' in line and b'setting' in line]
            self.assertTrue(control_lines, report[:1000])
            self.assertTrue(any('08:10:00' in line and '0.00' in line for line in control_lines), control_lines)
            self.assertTrue(any('08:20:00' in line and '1.00' in line for line in control_lines), control_lines)
            queries = compare_native(self, actual)
            self.assertEqual(queries, 51)  # 8 catchment + 3*6 node + 2*5 link + 15 system.
            with before.open_output() as old, actual.open_output() as new:
                target = Ref(collection='swmm:links', key='C2')
                old_flow, new_flow = old.series(target, 'swmm:flow'), new.series(target, 'swmm:flow')
                self.assertNotEqual(old_flow.values, new_flow.values)
                self.assertEqual(len(new_flow.values), 180)
                self.assertGreater(max(new_flow.values), 0)
                runoff = new.series(Ref(collection='swmm:subcatchments', key='area1'), 'swmm:runoff')
                self.assertGreater(max(runoff.values), 0)
                saved_depth = new.series(example.node('Middle'), 'swmm:depth')
            edited.nodes.rename('Middle', 'AfterRun')
            actual.save(root/'archive')
            (root/'archive').rename(root/'moved')
            (root/'edited').rename(root/'retired')
            restored = RunResult.load(root/'moved')
            with restored.open_output() as reader:
                self.assertEqual(reader.metadata.names('swmm:nodes'), ('Upstream', 'Middle', 'J3'))
                self.assertEqual(reader.series(example.node('Middle'), 'swmm:depth').values, saved_depth.values)
            EVIDENCE.append(dict(check='direct-native-edited-workflow', output_sha256=actual.output.sha256,
                queries=queries, samples=queries*180, control_actions=control_lines,
                original_flow_changed=True, moved_archive_preserved_identity=True,
                flow_continuity_percent=actual.mass_balance.flow_percent,
                runoff_continuity_percent=actual.mass_balance.runoff_percent,
                original_dry_start_flow_continuity_percent=before.mass_balance.flow_percent))

    def test_external_rain_file_and_fractional_step_match_inline_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inline = example.edit_model(example.create_model())
            original = inline.run(config(root/'inline')).raise_for_status()
            data = root/'rain data.dat'
            data.write_text(external_rain_text(), encoding='utf-8')
            data_sha = sha(data)
            external = inline.copy()
            external.timeseries.replace('Storm', FileTimeSeries(id='Storm',
                file=FileReference(path='rain data.dat', base_directory=str(root))))
            result = external.run(config(root/'external')).raise_for_status()
            self.assertEqual(result.output.sha256, original.output.sha256)
            self.assertEqual(sha(data), data_sha)
            # Execution identities differ; numerical continuity values must agree.
            for field in fields(result.mass_balance):
                if field.name != 'result_context':
                    self.assertEqual(getattr(result.mass_balance, field.name), getattr(original.mass_balance, field.name), field.name)
            EVIDENCE.append(dict(check='external-rain-and-fractional-step', output_sha256=result.output.sha256,
                                 input_file_unchanged=True, full_out_equal=True, routing_step_seconds=.5))


if __name__ == '__main__':
    unittest.main()
