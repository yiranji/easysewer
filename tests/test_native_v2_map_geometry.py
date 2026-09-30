"""Node/link/gage coordinates and both polygon owner classes do not change solver results."""
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
from easysewer.utils import probe_library_path
from test_native_v2_standard_io import direct_library, execute
from test_scenario_v2 import portable

EVIDENCE = []


from test_map_geometry_v2 import geometry_model


def without_geometry(model):
    base = model.copy()
    for key, row in base.nodes.items():
        base.nodes.update(key, position=None, **({'polygon': ()} if hasattr(row, 'polygon') else {}))
    for key in base.links: base.links.update(key, vertices=())
    for key in base.raingages: base.raingages.update(key, position=None)
    for key in base.subcatchments: base.subcatchments.update(key, polygon=())
    return base


@unittest.skipUnless(get_native_capabilities()['swmm_solver'] and get_native_capabilities()['flexible_ponding'],
    'Standard/custom native solvers unavailable')
class NativeMapGeometryTests(unittest.TestCase):
    def test_geometry_edits_preserve_complete_native_outputs(self):
        model = geometry_model()
        edited = model.copy()
        edited.nodes.update('J', polygon=(Point(x=99, y=-10), *model.nodes['J'].polygon))
        edited.links.update('P', vertices=())
        edited.raingages.update('R', position=None)
        edited.subcatchments.update('S', polygon=())
        sources = (without_geometry(model).to_document().text, model.to_document().text,
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
                EVIDENCE.append(dict(family=family, kind='geometry-hydraulics', comparisons=3,
                    library_sha256=hashlib.sha256(library.read_bytes()).hexdigest(),
                    out_sha256=hashlib.sha256(results[0][0]).hexdigest()))

    def test_geometry_model_survives_checkpoint_and_workspace_removal(self):
        from easysewer.runtime import RunResult
        from test_native_v2_runner_checkpoint import NativeRunnerCheckpointTests, runner, resume_config
        helper = NativeRunnerCheckpointTests()
        with patch.dict(os.environ, EASYSEWER_CHECKPOINT_PACKAGED='1'):
            for family in ('standard', 'custom'):
                with self.subTest(family=family), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    original, saved = helper.original(root, family, model=geometry_model())
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
                    EVIDENCE.append(dict(family=family, kind='geometry-checkpoint',
                        out_sha256=actual.output.sha256, original_workspace_removed=True))


if __name__ == '__main__':
    unittest.main()
