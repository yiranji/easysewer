"""LABELS/PROFILES must preserve hydraulic outputs and checkpoint results."""
import hashlib
import os
from pathlib import Path
import re
import shutil
import tempfile
import unittest
from unittest.mock import patch

from easysewer import get_native_capabilities
from easysewer.model import Point, Ref
from easysewer.model.project import MapLabel, ProfilePlot
from easysewer.utils import probe_library_path
from test_options_v2 import network
from test_project_v2 import load
from test_scenario_v2 import portable
from test_native_v2_standard_io import direct_library, execute

EVIDENCE = []


def display_model():
    model = load(network().to_document().text + '[DWF]\nJ FLOW 1\n')
    model.update_labels(entries=(
        MapLabel(position=Point(x=1, y=2), text='地图 label', anchor=Ref(collection='swmm:nodes', key='J'),
                 font_name='Times New Roman', font_size=14, bold=True),
        MapLabel(position=Point(x=1, y=2), text='地图 label'),
    ))
    model.profiles.add(ProfilePlot(name='North route', links=(Ref(collection='swmm:links', key='P'),) * 8))
    return model


@unittest.skipUnless(get_native_capabilities()['swmm_solver'] and get_native_capabilities()['flexible_ponding'],
    'Standard/custom native solvers unavailable')
class NativeDisplayTests(unittest.TestCase):
    def test_header_like_profile_is_rejected_before_native_execution(self):
        from easysewer.runtime import Runner, StandardBackend
        from test_runner_v2 import config
        model = display_model()
        model.profiles.rename('North route', '[North]')
        self.assertTrue(model.validate().is_valid)
        info = StandardBackend().probe()
        self.assertTrue(info.available)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'not-created'
            with patch.object(StandardBackend, 'probe', return_value=info), \
                 patch.object(StandardBackend, 'session', side_effect=AssertionError('simulation session started')) as session:
                result = Runner().run(model, config(root))
            session.assert_not_called()
            self.assertEqual(result.status, 'rejected')
            issues = [d for d in result.diagnostics.errors if d.code == 'inp.native_header_token']
            self.assertTrue(issues)
            self.assertTrue(all(d.span and d.section == 'PROFILES' for d in issues))
            self.assertFalse(any(a.role == 'run:output' for a in result.artifacts))

    def test_gui_metadata_edits_preserve_complete_native_outputs(self):
        model = display_model()
        base = model.copy()
        base.collection('swmm:labels').remove('layer')
        base.profiles.remove('North route')
        edited = model.copy()
        edited.update_labels(entries=tuple(reversed(model.labels.entries)))
        edited.profiles.rename('North route', 'New profile')
        edited.profiles.update('New profile', links=(Ref(collection='swmm:links', key='P'),))
        sources = (base.to_document().text, model.to_document().text,
                   portable(model).to_document().text, edited.to_document().text)
        for family, name, symbol in (
            ('standard', 'swmm5', 'swmm_getEasySewerStandardFixes'),
            ('custom', 'flexible_ponding', 'swmm_getEasySewerNativeIOFixes')):
            lib, library = direct_library(probe_library_path(name), revision_symbol=symbol)
            with self.subTest(family=family), tempfile.TemporaryDirectory() as directory:
                results = []
                for index, source in enumerate(sources):
                    root = Path(directory) / str(index)
                    root.mkdir()
                    self.assertTrue(all(code == 0 for code in execute(lib, root, source)))
                    report = re.sub(rb'(?m)^[ \t]*(?:Analysis (?:begun|ended)|Total elapsed time)[^\r\n]*',
                        b'', (root / 'model.rpt').read_bytes())
                    results.append(((root / 'model.out').read_bytes(), report))
                self.assertTrue(all(result == results[0] for result in results[1:]))
                EVIDENCE.append(dict(family=family, kind='display-hydraulics', comparisons=3,
                    library_sha256=hashlib.sha256(library.read_bytes()).hexdigest(),
                    out_sha256=hashlib.sha256(results[0][0]).hexdigest()))

    def test_display_model_survives_checkpoint_and_workspace_removal(self):
        from easysewer.runtime import RunResult
        from test_native_v2_runner_checkpoint import NativeRunnerCheckpointTests, runner, resume_config
        helper = NativeRunnerCheckpointTests()
        with patch.dict(os.environ, EASYSEWER_CHECKPOINT_PACKAGED='1'):
            for family in ('standard', 'custom'):
                with self.subTest(family=family), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    original, saved = helper.original(root, family, model=display_model())
                    helper.success(original)
                    original.save(root / 'expected')
                    expected = RunResult.load(root / 'expected')
                    shutil.rmtree(root / 'first')
                    (root / 'original.hsf').unlink()
                    (root / 'saved').rename(root / 'moved')
                    actual = runner(family).resume(root / 'moved' / saved[0].directory.name,
                        resume_config(root / 'resumed'))
                    helper.equivalent(expected, actual)
                    self.assertEqual(len(actual.continuations), 1)
                    EVIDENCE.append(dict(family=family, kind='display-checkpoint',
                        out_sha256=actual.output.sha256, original_workspace_removed=True))


if __name__ == '__main__':
    unittest.main()
