"""Combined F scenario: independent native oracle and fresh-parent recovery."""
import hashlib
from datetime import datetime, timedelta
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from easysewer import get_native_capabilities
from easysewer.model.project import Backdrop, MapSettings
from easysewer.runtime import RunResult
from easysewer.utils import probe_library_path
from test_f_domain_v2 import edited_model, state, target
from test_native_v2_map_geometry import without_geometry
from test_native_v2_runner_checkpoint import reports
from test_native_v2_standard_io import direct_library, execute
from test_scenario_v2 import portable

EVIDENCE = []


def without_gui(model):
    model = without_geometry(model)
    for key in tuple(model.tags):
        model.tags.remove(key)
    for key in tuple(model.profiles):
        model.profiles.remove(key)
    model.update_labels(entries=())
    for key in tuple(model.metadata):
        model.metadata.remove(key)
    model.collection('swmm:map').replace('settings', MapSettings())
    model.collection('swmm:backdrop').replace('image', Backdrop())
    return model


@unittest.skipUnless(get_native_capabilities()['swmm_solver'] and get_native_capabilities()['flexible_ponding'],
    'Standard/custom native solvers unavailable')
class NativeFDomainTests(unittest.TestCase):
    def test_combined_scenario_preserves_gui_independence_and_event_hydraulics(self):
        model = edited_model()
        plain = without_gui(model)
        plain.update_events(periods=())
        # Explicit native event rows form an oracle independent of event encoding.
        raw = plain.to_document().text + ('[EVENTS]\n'
            '01/01/2020 00:06 01/01/2020 00:12\n'
            '01/01/2020 00:01:30 01/01/2020 00:04:30\n'
            '01/01/2020 00:01:30 01/01/2020 00:04:30\n')
        start = datetime.combine(model.options.start_date, model.options.start_time)
        end = datetime.combine(model.options.end_date, model.options.end_time)
        self.assertTrue(all(start < row.start < row.end < end for row in model.events.periods))
        sources = (raw, model.to_document().text, portable(model).to_document().text,
                   without_gui(model).to_document().text, plain.to_document().text)
        for family, name, symbol in (
            ('standard', 'swmm5', 'swmm_getEasySewerStandardFixes'),
            ('custom', 'flexible_ponding', 'swmm_getEasySewerNativeIOFixes')):
            lib, library = direct_library(probe_library_path(name), revision_symbol=symbol)
            with self.subTest(family=family), tempfile.TemporaryDirectory() as directory:
                outputs = []
                for index, source in enumerate(sources):
                    root = Path(directory) / str(index)
                    root.mkdir()
                    self.assertTrue(all(code == 0 for code in execute(lib, root, source)))
                    outputs.append(((root/'model.out').read_bytes(), reports((root/'model.rpt').read_bytes())))
                for actual in outputs[1:4]:
                    self.assertEqual(actual, outputs[0])
                self.assertNotEqual(outputs[0][0], outputs[4][0])
                EVIDENCE.append(dict(family=family, kind='joint-native-oracle', comparisons=3,
                    out_sha256=hashlib.sha256(outputs[0][0]).hexdigest(),
                    report_sha256=hashlib.sha256(outputs[0][1]).hexdigest(),
                    events_change_output=True, library_sha256=hashlib.sha256(library.read_bytes()).hexdigest()))

    def test_joint_state_and_origins_survive_moved_checkpoint_in_fresh_parent(self):
        import easysewer
        from test_native_v2_runner_checkpoint import NativeRunnerCheckpointTests
        helper = NativeRunnerCheckpointTests()
        package = str(Path(easysewer.__file__).resolve().parent.parent)
        tests = str(Path(__file__).resolve().parent)
        child = '''import sys
from pathlib import Path
package, tests, family, checkpoint, output, archive = sys.argv[1:]
sys.path[:0] = [package, tests]
import easysewer
assert Path(easysewer.__file__).resolve().is_relative_to(Path(package).resolve())
from test_native_v2_runner_checkpoint import runner, resume_config
value = runner(family).resume(checkpoint, resume_config(Path(output)))
assert value.succeeded, (value.failure, value.diagnostics)
value.save(archive)
'''
        owners = (target('nodes', 'Tank'), target('links', 'Pipe'),
            target('tags', ('swmm:nodes', 'Tank')), target('labels', 'layer'),
            target('profiles', 'Edited profile'), target('events', 'schedule'))
        with patch.dict(os.environ, EASYSEWER_CHECKPOINT_PACKAGED='1'):
            for family in ('standard', 'custom'):
                with self.subTest(family=family), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    model = edited_model()
                    before = state(model)
                    origins = tuple(model.provenance(owner) for owner in owners)
                    original, saved = helper.original(root, family, model=model)
                    helper.success(original)
                    self.assertTrue(saved)
                    checkpoint_time = datetime.combine(model.options.start_date, model.options.start_time) + timedelta(seconds=saved[0].simulation_seconds)
                    self.assertTrue(any(row.start < checkpoint_time < row.end for row in model.events.periods))
                    original.save(root/'expected')
                    expected = RunResult.load(root/'expected')
                    self.assertEqual(state(expected.snapshot.model()), before)
                    shutil.rmtree(root/'first')
                    (root/'original.hsf').unlink()
                    (root/'saved').rename(root/'moved')
                    process = subprocess.run([sys.executable, '-I', '-B', '-c', child, package,
                        tests, family, str(root/'moved'/saved[0].directory.name), str(root/'resumed'),
                        str(root/'result')], capture_output=True, text=True, timeout=90,
                        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                    self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
                    actual = RunResult.load(root/'result')
                    helper.equivalent(expected, actual)
                    restored = actual.snapshot.model()
                    self.assertEqual(state(restored), before)
                    self.assertEqual(tuple(restored.provenance(owner) for owner in owners), origins)
                    EVIDENCE.append(dict(family=family, kind='joint-fresh-parent-checkpoint',
                        out_sha256=actual.output.sha256, checkpoints=len(saved),
                        fresh_parent=True, original_workspace_removed=True, joint_origins=len(owners)))


if __name__ == '__main__':
    unittest.main()
