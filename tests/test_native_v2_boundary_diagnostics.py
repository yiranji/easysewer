"""Rejected drafts leave numerical runs unchanged and warnings survive recovery."""
from datetime import timedelta
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from easysewer import get_native_capabilities
from easysewer.model import Model
from easysewer.runtime import RunResult
from easysewer.runtime._result_codec import Codec
from easysewer.validation import ValidationError
from test_native_v2_domain_diagnostics import warning_model
from test_hydrology_fields_v2 import UNITS
from test_native_v2_regulator_fields import FAMILIES, library
from test_native_v2_control_fields import observe

EVIDENCE = []


def option_warnings(result):
    return tuple(d for d in result.diagnostics.diagnostics if d.code == 'options.effective_adjustment')


@unittest.skipUnless(get_native_capabilities()['swmm_solver'] and get_native_capabilities()['flexible_ponding'],
                     'Both solver families required')
class NativeBoundaryDiagnosticTests(unittest.TestCase):
    def reject_drafts(self, model):
        before = model.to_json_document().to_bytes()
        for operation in (lambda: model.update_options(routing_step=timedelta(seconds=-1)),
                          lambda: model.pollutants.update('Q0', units='UG/L'),
                          lambda: model.nodes.remove('J')):
            with self.assertRaises(ValidationError) as caught: operation()
            self.assertTrue(all(d.subject and d.locations for d in caught.exception.report.errors))
            self.assertEqual(model.to_json_document().to_bytes(), before)

    def test_failed_drafts_six_units_preserve_complete_results(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for family, name, symbol in FAMILIES:
                lib, _ = library(name, symbol)
                rows = []
                for units in UNITS:
                    with self.subTest(family=family, units=units):
                        m = warning_model(units)
                        expected = observe(self, lib, root, m.document.text)
                        self.reject_drafts(m)
                        restored = Model.from_json_document(m.to_json_document(), strict=True)
                        self.assertEqual(restored.validate(for_run=True), m.validate(for_run=True))
                        self.assertEqual(observe(self, lib, root, restored.to_document(normalize=True).text), expected)
                        rows.append(dict(units=units, **expected))
                EVIDENCE.append(dict(kind='boundary-results', family=family, rows=rows))

    def test_option_sources_and_legacy_bootstrap_survive_moved_checkpoint(self):
        import test_native_v2_runner_checkpoint as checkpoint
        helper = checkpoint.NativeRunnerCheckpointTests()
        with patch.dict(os.environ, EASYSEWER_CHECKPOINT_PACKAGED='1'):
            for family in ('standard', 'custom'):
                with self.subTest(family=family), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    m = warning_model()
                    self.reject_drafts(m)
                    original, saved = helper.original(root, family, model=m)
                    helper.success(original)
                    before = option_warnings(original)
                    self.assertTrue(before)
                    self.assertTrue(all(d.subject.collection == 'swmm:options' and d.locations for d in before))
                    self.assertTrue(any(v.source_sha256 for d in before for v in d.locations))
                    low = original.snapshot.options
                    codec = Codec(None, result_version='1.1')
                    self.assertEqual(codec.decode(codec.encode(low)), low)
                    original.save(root / 'expected')
                    expected = RunResult.load(root / 'expected')
                    self.assertEqual(option_warnings(expected), before)
                    shutil.rmtree(root / 'first')
                    (root / 'original.hsf').unlink()
                    (root / 'saved').rename(root / 'moved')
                    resumed = checkpoint.runner(family).resume(root / 'moved' / saved[0].directory.name,
                        checkpoint.resume_config(root / 'resumed'))
                    helper.equivalent(expected, resumed)
                    self.assertEqual(option_warnings(resumed), before)
                    resumed.save(root / 'archive')
                    self.assertEqual(option_warnings(RunResult.load(root / 'archive')), before)
                    EVIDENCE.append(dict(kind='boundary-recovery', family=family, diagnostics=len(before),
                        original_workspace_removed=True, legacy_bootstrap=True, out_sha256=resumed.output.sha256))


if __name__ == '__main__': unittest.main()
