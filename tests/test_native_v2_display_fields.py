"""Actual snapshots/archives/continuation retain display field inspection."""
from dataclasses import fields
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from easysewer import get_native_capabilities
from easysewer.io.inp import InpDocument
from easysewer.model import Model, Ref
from easysewer.model.project import MapSettings, Backdrop, MapLabel
from test_map_geometry_v2 import geometry_model

EVIDENCE = []
MAP = Ref(collection='swmm:map', key='settings')
IMAGE = Ref(collection='swmm:backdrop', key='image')
LABELS = Ref(collection='swmm:labels', key='layer')
PATHS = (
    *((MAP, (f.name,)) for f in fields(MapSettings)),
    *((IMAGE, (f.name,)) for f in fields(Backdrop)),
    *((owner, ('extent', corner, axis)) for owner in (MAP, IMAGE)
      for corner in ('lower_left', 'upper_right') for axis in ('x', 'y')),
    *((IMAGE, (name, axis)) for name in ('legacy_offset', 'legacy_scaling') for axis in ('x', 'y')),
    (LABELS, ('entries',)),
    *((LABELS, ('entries', index, f.name)) for index in (0, 1) for f in fields(MapLabel)),
    *((LABELS, ('entries', index, 'position', axis)) for index in (0, 1) for axis in ('x', 'y')),
    (LABELS, ('entries', 0, 'anchor', 'key')), (LABELS, ('entries', 0, 'anchor', 'collection')),
)


def queries(model):
    return tuple((model.inspect_field(owner, path), model.field_provenance(owner, path)) for owner, path in PATHS)


@unittest.skipUnless(get_native_capabilities()['swmm_solver'] and get_native_capabilities()['flexible_ponding'],
    'Standard/custom native solvers unavailable')
class NativeDisplayFieldTests(unittest.TestCase):
    def test_display_queries_survive_result_archives_and_relocated_continuation(self):
        from easysewer.runtime import RunResult
        from test_native_v2_runner_checkpoint import NativeRunnerCheckpointTests, runner, resume_config
        helper = NativeRunnerCheckpointTests()
        with patch.dict(os.environ, EASYSEWER_CHECKPOINT_PACKAGED='1'):
            for family in ('standard', 'custom'):
                with self.subTest(family=family), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    source = geometry_model().to_document().text + (
                        '\n[MAP]\nDIMENSIONS 0 0 100 200\nUNITS FEET\n'
                        '[BACKDROP]\nDIMENSIONS 10 20 -3 -4\nUNITS METERS\nFILE ""\nOFFSET 1 2\nSCALING 3 4\n'
                        '[MAP]\nUNITS DEGREES\n[LABELS]\n1 2 "original" J\n3 4 "explicit" "" Arial 10 0 0\n')
                    model = Model.from_document(InpDocument.from_text(source, source='display-source.inp'), strict=True)
                    model.update_map(units_precedence='BACKDROP')
                    model.nodes.rename('J', 'Tank')
                    expected_queries = queries(model)
                    original, saved = helper.original(root, family, model=model)
                    helper.success(original)
                    self.assertEqual(queries(original.snapshot.model()), expected_queries)
                    original.save(root / 'expected')
                    expected = RunResult.load(root / 'expected')
                    self.assertEqual(queries(expected.snapshot.model()), expected_queries)
                    shutil.rmtree(root / 'first'); (root / 'original.hsf').unlink()
                    (root / 'saved').rename(root / 'moved')
                    actual = runner(family).resume(root / 'moved' / saved[0].directory.name,
                        resume_config(root / 'resumed'))
                    helper.equivalent(expected, actual)
                    self.assertEqual(queries(actual.snapshot.model()), expected_queries)
                    EVIDENCE.append(dict(family=family, kind='display-fields-checkpoint',
                        out_sha256=actual.output.sha256, fields=len(PATHS), original_workspace_removed=True,
                        source_sha256=model.field_provenance(MAP, 'units_precedence').source_sha256))


if __name__ == '__main__':
    unittest.main()
