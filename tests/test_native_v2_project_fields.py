"""Project field inspection preserves full simulation results and saved sources."""
import hashlib
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from easysewer import get_native_capabilities
from easysewer.model import Model
from test_project_fields_v2 import fixture, load, queries, materialize, UNITS
from test_native_v2_regulator_fields import FAMILIES, library
from test_native_v2_control_fields import observe

EVIDENCE = []
VARIANTS = ('mixed', 'title', 'tags', 'profiles', 'annotation')


@unittest.skipUnless(get_native_capabilities()['swmm_solver'] and get_native_capabilities()['flexible_ponding'],
                     'Native solver libraries unavailable')
class NativeProjectFieldTests(unittest.TestCase):
    def test_field_queries_json_and_normalization_preserve_complete_results(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            for family, name, symbol in FAMILIES:
                lib, path = library(name, symbol); rows = []
                for units in UNITS:
                    for variant in VARIANTS:
                        with self.subTest(family=family, units=units, variant=variant):
                            source = fixture(units, variant=variant); m = load(source)
                            before = queries(m); materialize(m)
                            m = Model.from_json_document(m.to_json_document(), strict=True)
                            self.assertEqual(queries(m), before)
                            expected = observe(self, lib, base, source)
                            actual = observe(self, lib, base, m.to_document(normalize=True).text)
                            self.assertEqual(actual, expected)
                            self.assertGreater(max(row[-1] for row in actual['history']), 0)
                            rows.append(dict(units=units, variant=variant, steps=len(actual['history']),
                                out_sha256=actual['out_sha256'], report_sha256=actual['report_sha256']))
                EVIDENCE.append(dict(kind='project-field-native-results', family=family, cases=len(rows), rows=rows,
                    library_sha256=hashlib.sha256(Path(path).read_bytes()).hexdigest()))

    def test_gui_metadata_is_informational_and_native_title_limit_is_separate(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            for family, name, symbol in FAMILIES:
                lib, _ = library(name, symbol)
                plain = observe(self, lib, base, fixture(variant='absent'))
                rows = []
                for variant in ('tags', 'profiles', 'annotation', 'title'):
                    actual = observe(self, lib, base, fixture(variant=variant))
                    self.assertEqual(actual['out_sha256'], plain['out_sha256'])
                    self.assertEqual(actual['history'], plain['history'])
                    if variant == 'title':
                        self.assertNotEqual(actual['report_sha256'], plain['report_sha256'])
                        report = (base/'model.rpt').read_bytes()
                        for value in (b'First title', b'Second title', b'Third title'): self.assertIn(value, report)
                        self.assertNotIn(b'Fourth title', report)
                    else:
                        self.assertEqual(actual['report_sha256'], plain['report_sha256'])
                    rows.append(dict(variant=variant, out_sha256=actual['out_sha256'], report_sha256=actual['report_sha256']))
                EVIDENCE.append(dict(kind='project-metadata-native-boundaries', family=family, rows=rows,
                    plain_out_sha256=plain['out_sha256'], plain_report_sha256=plain['report_sha256']))

    def test_raw_sources_survive_runresult_and_moved_checkpoint(self):
        from easysewer.runtime import RunResult
        import test_native_v2_runner_checkpoint as checkpoint
        helper = checkpoint.NativeRunnerCheckpointTests()
        with patch.dict(os.environ, EASYSEWER_CHECKPOINT_PACKAGED='1'):
            for family in ('standard', 'custom'):
                for variant in ('mixed', 'title', 'annotation'):
                    with self.subTest(family=family, variant=variant), tempfile.TemporaryDirectory() as directory:
                        root = Path(directory); m = load(fixture(variant=variant)); before = queries(m)
                        original, saved = helper.original(root, family, model=m); helper.success(original)
                        self.assertTrue(saved); self.assertEqual(queries(original.snapshot.model()), before)
                        original.save(root/'expected'); expected = RunResult.load(root/'expected')
                        self.assertEqual(queries(expected.snapshot.model()), before)
                        shutil.rmtree(root/'first'); (root/'original.hsf').unlink(); (root/'saved').rename(root/'moved')
                        actual = checkpoint.runner(family).resume(root/'moved'/saved[0].directory.name,
                            checkpoint.resume_config(root/'resumed'))
                        helper.equivalent(expected, actual); self.assertEqual(queries(actual.snapshot.model()), before)
                        EVIDENCE.append(dict(kind='project-fields-checkpoint', family=family, variant=variant,
                            queries=len(before), out_sha256=actual.output.sha256, original_workspace_removed=True))


if __name__ == '__main__': unittest.main()
