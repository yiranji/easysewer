"""MAP/BACKDROP order and legacy directives do not change solver results."""
import hashlib
import os
from pathlib import Path
import re
import shutil
import tempfile
import unittest
from unittest.mock import patch

from easysewer import get_native_capabilities
from easysewer.model import Point
from easysewer.model.project import MapExtent
from easysewer.utils import probe_library_path
from test_native_v2_display import display_model
from test_native_v2_standard_io import direct_library, execute
from test_scenario_v2 import portable

EVIDENCE = []


def map_model():
    model = display_model()
    model.update_map(units='FEET', extent=MapExtent(
        lower_left=Point(x=-100, y=-200), upper_right=Point(x=100, y=200)))
    model.update_backdrop(clear_file=True, units='DEGREES',
        extent=MapExtent(lower_left=Point(x=5, y=6), upper_right=Point(x=0, y=0)),
        legacy_offset=Point(x=-1, y=2), legacy_scaling=Point(x=0, y=-3))
    model.update_map(units_precedence='BACKDROP')
    return model


@unittest.skipUnless(get_native_capabilities()['swmm_solver'] and get_native_capabilities()['flexible_ponding'],
    'Standard/custom native solvers unavailable')
class NativeMapTests(unittest.TestCase):
    def test_map_edits_preserve_complete_native_outputs(self):
        model = map_model()
        edited = model.copy()
        edited.update_map(units_precedence='MAP')
        sources = (display_model().to_document().text, model.to_document().text,
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
                EVIDENCE.append(dict(family=family, kind='map-hydraulics', comparisons=3,
                    library_sha256=hashlib.sha256(library.read_bytes()).hexdigest(),
                    out_sha256=hashlib.sha256(results[0][0]).hexdigest()))

    def test_map_model_survives_checkpoint_and_workspace_removal(self):
        from easysewer.runtime import RunResult
        from test_native_v2_runner_checkpoint import NativeRunnerCheckpointTests, runner, resume_config
        helper = NativeRunnerCheckpointTests()
        with patch.dict(os.environ, EASYSEWER_CHECKPOINT_PACKAGED='1'):
            for family in ('standard', 'custom'):
                with self.subTest(family=family), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    original, saved = helper.original(root, family, model=map_model())
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
                    EVIDENCE.append(dict(family=family, kind='map-checkpoint',
                        out_sha256=actual.output.sha256, original_workspace_removed=True))


if __name__ == '__main__':
    unittest.main()
