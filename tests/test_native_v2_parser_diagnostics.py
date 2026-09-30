"""Parser metadata must not change native results and must survive relocation."""
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from easysewer import get_native_capabilities
from easysewer.io.inp import InpDocument
from easysewer.io.json import JsonDocument
from easysewer.model import Model
from easysewer.runtime import RunResult
from test_json_v2 import entry
from test_options_v2 import network
from test_native_v2_regulator_fields import FAMILIES, library
from test_native_v2_control_fields import observe
import test_native_v2_runner_checkpoint as checkpoint

EVIDENCE = []
CODES = {'geometry.ignored_columns', 'json.source_field_promoted'}


def parser_model():
    text = network().to_document().text + '\n[COORDINATES]\nJ 1 2 ignored\n[BACKDROP]\nOFFSET 1 2\n'
    m = Model.from_document(InpDocument.from_text(text, source='parser-original.inp'), strict=True)
    data = m.to_json_document().data
    entry(data, 'swmm:backdrop', 'image')['value'].pop('legacy_offset')
    return Model.from_json_document(JsonDocument.from_data(data, source='parser-input.json'), strict=True)


def original_diagnostics(result):
    return tuple(d for d in result.diagnostics.diagnostics if d.code in CODES
        and d.span is not None and d.span.source in ('parser-original.inp', 'parser-input.json'))


@unittest.skipUnless(get_native_capabilities()['swmm_solver'] and get_native_capabilities()['flexible_ponding'],
                     'Both native families required')
class NativeParserDiagnosticTests(unittest.TestCase):
    def test_original_json_promoted_and_normalized_models_keep_complete_results(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for family, name, symbol in FAMILIES:
                with self.subTest(family=family):
                    lib, _ = library(name, symbol)
                    expected = observe(self, lib, root, network().to_document().text)
                    m = parser_model()
                    issues = [d for d in m.validate().diagnostics if d.code in CODES]
                    self.assertEqual({d.code for d in issues}, CODES)
                    self.assertTrue(all(d.subject and d.locations for d in issues))
                    for text in (m.document.text, m.to_document(normalize=True).text,
                                 Model.from_json_document(m.to_json_document()).to_document(normalize=True).text):
                        self.assertEqual(observe(self, lib, root, text), expected)
                    EVIDENCE.append(dict(kind='parser-results', family=family, **expected))

    def test_json_and_inp_sources_remain_distinct_in_moved_recovery(self):
        helper = checkpoint.NativeRunnerCheckpointTests()
        with patch.dict(os.environ, EASYSEWER_CHECKPOINT_PACKAGED='1'):
            for family in ('standard', 'custom'):
                with self.subTest(family=family), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory); original, saved = helper.original(root, family, model=parser_model())
                    helper.success(original); self.assertTrue(saved)
                    before = original_diagnostics(original)
                    self.assertEqual({d.code for d in before}, CODES)
                    promoted = next(d for d in before if d.code == 'json.source_field_promoted')
                    self.assertEqual(promoted.span.source, 'parser-input.json')
                    self.assertTrue(any(s.source == 'parser-original.inp' for v in promoted.locations for s in v.spans))
                    original.save(root / 'expected'); expected = RunResult.load(root / 'expected')
                    self.assertEqual(original_diagnostics(expected), before)
                    shutil.rmtree(root / 'first'); (root / 'original.hsf').unlink()
                    (root / 'saved').rename(root / 'moved')
                    resumed = checkpoint.runner(family).resume(root / 'moved' / saved[0].directory.name,
                        checkpoint.resume_config(root / 'resumed'))
                    helper.equivalent(expected, resumed)
                    self.assertEqual(original_diagnostics(resumed), before)
                    fresh = [d for d in resumed.diagnostics.diagnostics if d.code == 'geometry.ignored_columns'
                        and d.span.source == 'checkpoint-input:' + resumed.snapshot.input_sha256]
                    self.assertTrue(fresh)
                    self.assertTrue(all(s.source == d.span.source for d in fresh for v in d.locations for s in v.spans))
                    resumed.save(root / 'archive')
                    self.assertEqual(RunResult.load(root / 'archive').diagnostics, resumed.diagnostics)
                    EVIDENCE.append(dict(kind='parser-recovery', family=family, original_diagnostics=len(before),
                        checkpoint_diagnostics=len(fresh), original_workspace_removed=True, out_sha256=resumed.output.sha256))


if __name__ == '__main__': unittest.main()
