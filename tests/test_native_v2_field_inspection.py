"""Field declarations and semantics remain queryable from actual run artifacts."""
from datetime import timedelta
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from easysewer import get_native_capabilities
from easysewer.io.inp import InpDocument
from easysewer.model import Model, Ref
from easysewer.schema.option_profile import OPTION_DEFINITIONS
from test_map_geometry_v2 import geometry_model

EVIDENCE = []


@unittest.skipUnless(get_native_capabilities()['swmm_solver'] and get_native_capabilities()['flexible_ponding'],
    'Standard/custom native solvers unavailable')
class NativeFieldInspectionTests(unittest.TestCase):
    def test_all_option_queries_survive_actual_archives_and_moved_checkpoint(self):
        from easysewer.runtime import RunResult
        from test_native_v2_runner_checkpoint import NativeRunnerCheckpointTests, runner, resume_config
        helper = NativeRunnerCheckpointTests()
        owner = Ref(collection='swmm:options', key='settings')
        def queries(model):
            return tuple(model.inspect_field(owner, d.field) for d in OPTION_DEFINITIONS)
        with patch.dict(os.environ, EASYSEWER_CHECKPOINT_PACKAGED='1'):
            for family in ('standard', 'custom'):
                with self.subTest(family=family), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    base = geometry_model(); base.update_options(rule_step=timedelta())
                    model = Model.from_document(InpDocument.from_text(base.to_document().text,
                        source='source-project.inp'), strict=True)
                    model.update_options(rule_step=timedelta(seconds=30), allow_ponding=True)
                    before = queries(model)
                    rule = model.inspect_field(owner, 'rule_step')
                    self.assertEqual(rule.provenance.value.value, timedelta())
                    self.assertTrue(rule.changed)
                    original, saved = helper.original(root, family, model=model)
                    helper.success(original)
                    self.assertEqual(queries(original.snapshot.model()), before)
                    original.save(root / 'expected')
                    expected = RunResult.load(root / 'expected')
                    self.assertEqual(queries(expected.snapshot.model()), before)
                    shutil.rmtree(root / 'first'); (root / 'original.hsf').unlink()
                    (root / 'saved').rename(root / 'moved')
                    actual = runner(family).resume(root / 'moved' / saved[0].directory.name,
                        resume_config(root / 'resumed'))
                    helper.equivalent(expected, actual)
                    self.assertEqual(queries(actual.snapshot.model()), before)
                    EVIDENCE.append(dict(family=family, kind='field-inspection-checkpoint',
                        out_sha256=actual.output.sha256, original_workspace_removed=True,
                        fields=len(before), source_sha256=rule.provenance.source_sha256))


if __name__ == '__main__':
    unittest.main()
