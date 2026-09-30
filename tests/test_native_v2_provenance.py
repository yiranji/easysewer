"""Record origins survive ordinary Runner snapshots, archives and continuation."""
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from easysewer import get_native_capabilities
from easysewer.io.inp import InpDocument
from easysewer.model import Model, Ref
from test_map_geometry_v2 import geometry_model

EVIDENCE = []


@unittest.skipUnless(get_native_capabilities()['swmm_solver'] and get_native_capabilities()['flexible_ponding'],
    'Standard/custom native solvers unavailable')
class NativeProvenanceTests(unittest.TestCase):
    def test_source_lineage_survives_result_archive_and_relocated_checkpoint(self):
        from easysewer.runtime import RunResult
        from test_native_v2_runner_checkpoint import NativeRunnerCheckpointTests, runner, resume_config
        helper = NativeRunnerCheckpointTests()
        target = Ref(collection='swmm:nodes', key='Tank')
        with patch.dict(os.environ, EASYSEWER_CHECKPOINT_PACKAGED='1'):
            for family in ('standard', 'custom'):
                with self.subTest(family=family), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    model = Model.from_document(InpDocument.from_text(geometry_model().to_document().text,
                        source='original-project.inp'), strict=True)
                    model.nodes.rename('J', 'Tank')
                    provenance = model.provenance(target)
                    original, saved = helper.original(root, family, model=model)
                    helper.success(original)
                    self.assertEqual(original.snapshot.model().provenance(target), provenance)
                    original.save(root / 'expected')
                    expected = RunResult.load(root / 'expected')
                    self.assertEqual(expected.snapshot.model().provenance(target), provenance)
                    shutil.rmtree(root / 'first')
                    (root / 'original.hsf').unlink()
                    (root / 'saved').rename(root / 'moved')
                    actual = runner(family).resume(root / 'moved' / saved[0].directory.name,
                        resume_config(root / 'resumed'))
                    helper.equivalent(expected, actual)
                    self.assertEqual(actual.snapshot.model().provenance(target), provenance)
                    self.assertEqual(provenance.original, Ref(collection='swmm:nodes', key='J'))
                    self.assertEqual(len(provenance.records), 5)
                    EVIDENCE.append(dict(family=family, kind='provenance-checkpoint',
                        out_sha256=actual.output.sha256, original_workspace_removed=True,
                        original_records=len(provenance.records), source_sha256=provenance.source_sha256))


if __name__ == '__main__':
    unittest.main()
