"""Changing GUI tags preserves both packaged engines' hydraulic outputs."""
import hashlib
import os
from pathlib import Path
import re
import shutil
import tempfile
import unittest

from easysewer import get_native_capabilities
from easysewer.model import Ref
from easysewer.model.project import ObjectTag
from easysewer.utils import probe_library_path
from test_hydrology_v2 import hydrology_model
from test_native_v2_standard_io import direct_library, execute
from test_scenario_v2 import portable

EVIDENCE = []


@unittest.skipUnless(get_native_capabilities()['swmm_solver'] and get_native_capabilities()['flexible_ponding'],
    'Standard/custom native solvers unavailable')
class NativeTagsTests(unittest.TestCase):
    def test_tagged_model_survives_checkpoint_archive_and_workspace_removal(self):
        from unittest.mock import patch
        from easysewer.runtime import RunResult
        from test_options_v2 import network
        from test_native_v2_runner_checkpoint import NativeRunnerCheckpointTests, runner, resume_config
        helper = NativeRunnerCheckpointTests()
        with patch.dict(os.environ, EASYSEWER_CHECKPOINT_PACKAGED='1'):
            for family in ('standard', 'custom'):
                with self.subTest(family=family), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    model = network()
                    model.tags.add(ObjectTag(target=Ref(collection='swmm:nodes', key='J'), text='checkpoint tag'))
                    original, saved = helper.original(root, family, model=model)
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
                    EVIDENCE.append(dict(family=family, kind='tagged-checkpoint',
                        out_sha256=actual.output.sha256, original_workspace_removed=True))

    def test_all_tag_classes_leave_complete_out_and_report_unchanged(self):
        model = hydrology_model()
        tagged = model.copy()
        for namespace, key in (('raingages', 'R'), ('subcatchments', 'S'), ('nodes', 'J'), ('links', 'P')):
            tagged.tags.add(ObjectTag(target=Ref(collection='swmm:' + namespace, key=key), text='雨水 group'))
        changed = tagged.copy()
        changed.tags.update(('swmm:nodes', 'J'), text='changed')
        changed.tags.remove(('swmm:links', 'P'))
        inputs = (model.to_document().text, tagged.to_document().text,
                  portable(tagged).to_document().text, changed.to_document().text)
        for family, name, symbol in (
            ('standard', 'swmm5', 'swmm_getEasySewerStandardFixes'),
            ('custom', 'flexible_ponding', 'swmm_getEasySewerNativeIOFixes')):
            lib, library = direct_library(probe_library_path(name), revision_symbol=symbol)
            with self.subTest(family=family), tempfile.TemporaryDirectory() as directory:
                results = []
                for i, source in enumerate(inputs):
                    root = Path(directory) / str(i)
                    root.mkdir()
                    self.assertTrue(all(code == 0 for code in execute(lib, root, source)))
                    report = re.sub(rb'(?m)^[ \t]*(?:Analysis (?:begun|ended)|Total elapsed time)[^\r\n]*',
                        b'', (root / 'model.rpt').read_bytes())
                    results.append(((root / 'model.out').read_bytes(), report))
                self.assertTrue(all(result == results[0] for result in results[1:]))
                EVIDENCE.append(dict(family=family, kind='tag-hydraulics', comparisons=3,
                    library_sha256=hashlib.sha256(library.read_bytes()).hexdigest(),
                    out_sha256=hashlib.sha256(results[0][0]).hexdigest()))


if __name__ == '__main__':
    unittest.main()
