"""Actual normal-flow behavior and portable checkpoints for the NONE option."""
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
from test_option_variants_v2 import limitation_model, OWNER
from test_native_v2_regulator_fields import FAMILIES, library
from test_native_v2_control_fields import observe
import test_native_v2_runner_checkpoint as checkpoint

EVIDENCE = []


@unittest.skipUnless(get_native_capabilities()['swmm_solver'] and get_native_capabilities()['flexible_ponding'],
                     'Both native families required')
class NativeOptionVariantTests(unittest.TestCase):
    def test_normal_limit_modes_preserve_complete_results_and_none_changes_flow(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for family, name, symbol in FAMILIES:
                lib, _ = library(name, symbol)
                for unit in ('CFS', 'GPM', 'MGD', 'CMS', 'LPS', 'MLD'):
                    m = limitation_model(); m.convert_units(unit); source = m.to_document().text
                    modes = {}
                    for mode in ('BOTH', 'SLOPE', 'FROUDE', 'NONE'):
                        with self.subTest(family=family, unit=unit, mode=mode):
                            raw = source.replace('[OPTIONS]', '[OPTIONS]\nNORMAL_FLOW_LIMITED ' + mode)
                            expected = observe(self, lib, root, raw)
                            typed = Model.from_document(InpDocument.from_text(raw), strict=True)
                            created = m.copy(); created.update_options(normal_flow_limited=mode)
                            restored = Model.from_json_document(typed.to_json_document(), strict=True)
                            for value in (typed, created, restored):
                                self.assertEqual(observe(self, lib, root, value.to_document(normalize=True).text), expected)
                            modes[mode] = expected
                            EVIDENCE.append(dict(kind='normal-limit-result', family=family, unit=unit, mode=mode,
                                out_sha256=expected['out_sha256'], report_sha256=expected['report_sha256'],
                                history=expected['history']))
                    self.assertNotEqual(modes['NONE']['out_sha256'], modes['BOTH']['out_sha256'])
                    self.assertGreater(max(abs(a[3]-b[3]) for a, b in zip(modes['NONE']['history'], modes['BOTH']['history'])), .01)

    def test_none_result_and_moved_checkpoint_keep_explicit_option(self):
        helper = checkpoint.NativeRunnerCheckpointTests()
        with patch.dict(os.environ, EASYSEWER_CHECKPOINT_PACKAGED='1'):
            for family in ('standard', 'custom'):
                with self.subTest(family=family), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory); m = limitation_model(); m.update_options(normal_flow_limited='NONE')
                    m = Model.from_document(InpDocument.from_text(m.to_document().text, source='normal-flow.inp'), strict=True)
                    original, saved = helper.original(root, family, model=m)
                    helper.success(original); self.assertTrue(saved)
                    original.save(root/'expected'); expected = RunResult.load(root/'expected')
                    shutil.rmtree(root/'first'); (root/'original.hsf').unlink(); (root/'saved').rename(root/'moved')
                    actual = checkpoint.runner(family).resume(root/'moved'/saved[0].directory.name,
                        checkpoint.resume_config(root/'resumed'))
                    helper.equivalent(expected, actual)
                    for value in (expected.snapshot.model(), actual.snapshot.model()):
                        self.assertEqual(value.options.normal_flow_limited, 'NONE')
                        self.assertEqual(value.inspect_field(OWNER, 'normal_flow_limited').semantics.effective.value, 'NONE')
                    actual.save(root/'archive')
                    self.assertEqual(RunResult.load(root/'archive').snapshot.model().options.normal_flow_limited, 'NONE')
                    EVIDENCE.append(dict(kind='normal-limit-recovery', family=family,
                        out_sha256=actual.output.sha256, original_workspace_removed=True))


if __name__ == '__main__': unittest.main()
