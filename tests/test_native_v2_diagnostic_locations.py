"""Actual solver results and moved recovery retain structured warning locations."""
from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from easysewer import get_native_capabilities
from easysewer.io.inp import InpDocument
from easysewer.model import Model
from easysewer.runtime import RunResult
from test_regulators_v2 import regulator_model
from test_native_v2_regulator_fields import FAMILIES, library
from test_native_v2_control_fields import observe
from test_hydrology_fields_v2 import UNITS

EVIDENCE = []


def warning_model(units='CFS'):
    model = regulator_model('SIDE'); model.reinterpret_units(units)
    model.update_options(allow_ponding=True)
    model.nodes.update('O', elevation=11)
    return Model.from_document(InpDocument.from_text(model.to_document().text, source='D:/original/model.inp'), strict=True)


def warnings(result):
    return tuple(d for d in result.diagnostics.diagnostics if d.code == 'regulator.crest_below_downstream'
                 and d.locations and any(s.source == 'D:/original/model.inp' for v in d.locations for s in v.spans))


@unittest.skipUnless(get_native_capabilities()['swmm_solver'] and get_native_capabilities()['flexible_ponding'],
                     'Both solver families required')
class NativeDiagnosticLocationTests(unittest.TestCase):
    def test_warning_sources_do_not_change_complete_results(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for family, name, symbol in FAMILIES:
                lib, _ = library(name, symbol); rows = []
                for units in UNITS:
                    model = warning_model(units)
                    issue = next(d for d in model.validate(for_run=True).diagnostics if d.code == 'regulator.crest_below_downstream')
                    self.assertTrue(issue.locations)
                    self.assertEqual(issue.locations[0].status, 'context')
                    restored = Model.from_json_document(model.to_json_document(), strict=True)
                    self.assertEqual(restored.validate(for_run=True), model.validate(for_run=True))
                    original = observe(self, lib, root, model.document.text)
                    self.assertEqual(observe(self, lib, root, restored.to_document(normalize=True).text), original)
                    rows.append(dict(units=units, **original))
                EVIDENCE.append(dict(kind='diagnostic-native-results', family=family, rows=rows))

    def test_warning_locations_survive_moved_checkpoint_and_result_archive(self):
        import test_native_v2_runner_checkpoint as checkpoint
        helper = checkpoint.NativeRunnerCheckpointTests()
        with patch.dict(os.environ, EASYSEWER_CHECKPOINT_PACKAGED='1'):
            for family in ('standard','custom'):
                with self.subTest(family=family), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    original, saved = helper.original(root, family, model=warning_model())
                    helper.success(original); self.assertTrue(saved)
                    before = warnings(original); self.assertTrue(before)
                    self.assertTrue(all(d.subject.collection == 'swmm:links' for d in before))
                    self.assertEqual(json.loads(saved[0].manifest)['codec_version'], '1.2')
                    original.save(root/'expected'); expected = RunResult.load(root/'expected')
                    self.assertEqual(warnings(expected), before)
                    shutil.rmtree(root/'first'); (root/'original.hsf').unlink()
                    (root/'saved').rename(root/'moved')
                    resumed = checkpoint.runner(family).resume(root/'moved'/saved[0].directory.name,
                        checkpoint.resume_config(root/'resumed'))
                    helper.equivalent(expected, resumed)
                    self.assertEqual(warnings(resumed), before)
                    resumed.save(root/'archive'); self.assertEqual(warnings(RunResult.load(root/'archive')), before)
                    EVIDENCE.append(dict(kind='diagnostic-moved-checkpoint', family=family,
                        diagnostics=len(before), out_sha256=resumed.output.sha256, original_workspace_removed=True))


if __name__ == '__main__': unittest.main()
