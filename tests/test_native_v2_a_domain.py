"""A independent native oracle, real Runner and portable checkpoint resume."""
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from easysewer import get_native_capabilities
from easysewer.io.output import OutputReader
from easysewer.model import Model
from easysewer.runtime import RunResult
from easysewer.utils import probe_library_path
from a_extension_fixture import ORACLE, extension_schema, model
from test_a_domain_v2 import EDITED_ORACLE, edited, imported, portable, ref, state
from test_native_v2_runner_checkpoint import reports
from test_native_v2_standard_io import direct_library, execute

EVIDENCE = []


@unittest.skipUnless(get_native_capabilities()['swmm_solver'] and get_native_capabilities()['flexible_ponding'],
                    'Standard/custom native solvers unavailable')
class NativeADomainTests(unittest.TestCase):
    def test_independent_native_oracles_for_creation_and_edited_roundtrips(self):
        for family, name, symbol in (
            ('standard', 'swmm5', 'swmm_getEasySewerStandardFixes'),
            ('custom', 'flexible_ponding', 'swmm_getEasySewerNativeIOFixes')):
            lib, library = direct_library(probe_library_path(name), revision_symbol=symbol)
            hashes = []
            for kind, oracle, created in (('created', ORACLE, model()), ('edited', EDITED_ORACLE, edited())):
                loaded = Model.from_document(created.to_document(), schema=extension_schema(), strict=True)
                sources = (oracle, created.to_document().text, loaded.to_document().text,
                           portable(created).to_document().text)
                with self.subTest(family=family, kind=kind), tempfile.TemporaryDirectory() as directory:
                    outputs, peaks = [], {}
                    for index, source in enumerate(sources):
                        root = Path(directory)/str(index)
                        root.mkdir()
                        codes = execute(lib, root, source)
                        self.assertTrue(all(code == 0 for code in codes), (codes, (root/'model.rpt').read_bytes()))
                        outputs.append(((root/'model.out').read_bytes(), reports((root/'model.rpt').read_bytes())))
                        if index == 0:
                            with OutputReader(root/'model.out') as reader:
                                for key in ('P', 'Q', 'T'):
                                    values = reader.series(ref('links', key), 'swmm:flow').values
                                    self.assertTrue(values)
                                    peaks[key] = max(abs(v) for v in values)
                                    self.assertGreater(peaks[key], 0)
                    for output in outputs[1:]:
                        self.assertEqual(output, outputs[0])
                    hashes.append(hashlib.sha256(outputs[0][0]).hexdigest())
                    EVIDENCE.append(dict(family=family, kind=kind, comparisons=3,
                        out_sha256=hashes[-1], report_sha256=hashlib.sha256(outputs[0][1]).hexdigest(),
                        flow_peaks=peaks, library_sha256=hashlib.sha256(library.read_bytes()).hexdigest()))
            if len(hashes) == 2:
                self.assertNotEqual(hashes[0], hashes[1])

    def test_custom_types_survive_archive_and_moved_checkpoint_in_fresh_parent(self):
        import easysewer
        from test_native_v2_runner_checkpoint import NativeRunnerCheckpointTests
        helper = NativeRunnerCheckpointTests()
        package = str(Path(easysewer.__file__).resolve().parent.parent)
        tests = str(Path(__file__).resolve().parent)
        child = '''import sys
from pathlib import Path
package, tests, family, checkpoint, output, archive = sys.argv[1:]
sys.path[:0] = [package, tests]
import easysewer
assert Path(easysewer.__file__).resolve().is_relative_to(Path(package).resolve())
from a_extension_fixture import extension_schema
from test_native_v2_runner_checkpoint import runner, resume_config
value = runner(family).resume(checkpoint, resume_config(Path(output)), schema=extension_schema())
assert value.succeeded, (value.failure, value.diagnostics)
value.save(archive)
'''
        with patch.dict(os.environ, EASYSEWER_CHECKPOINT_PACKAGED='1'):
            for family in ('standard', 'custom'):
                with self.subTest(family=family), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory).resolve()
                    value = edited(imported())
                    before = state(value)
                    owners = (ref('nodes', 'J2'), ref('links', 'P'), ref('links', 'Q'), ref('curves', 'Rate2'))
                    origins = tuple(value.provenance(owner) for owner in owners)
                    original, saved = helper.original(root, family, model=value)
                    helper.success(original)
                    self.assertTrue(saved)
                    self.assertGreater(saved[0].simulation_seconds, 0)
                    self.assertLess(saved[0].simulation_seconds, 300)
                    original.save(root/'expected')
                    expected = RunResult.load(root/'expected')
                    self.assertEqual(state(expected.snapshot.model(schema=extension_schema())), before)
                    # All deletion/movement targets are inside this newly created temporary root.
                    for name in ('first', 'original.hsf', 'saved', 'moved'):
                        self.assertTrue((root/name).resolve().is_relative_to(root))
                    shutil.rmtree(root/'first')
                    (root/'original.hsf').unlink()
                    (root/'saved').rename(root/'moved')
                    process = subprocess.run([sys.executable, '-I', '-B', '-c', child, package, tests,
                        family, str(root/'moved'/saved[0].directory.name), str(root/'resumed'),
                        str(root/'result')], capture_output=True, text=True, timeout=90,
                        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                    self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
                    actual = RunResult.load(root/'result')
                    helper.equivalent(expected, actual)
                    restored = actual.snapshot.model(schema=extension_schema())
                    self.assertEqual(state(restored), before)
                    self.assertEqual(tuple(restored.provenance(owner) for owner in owners), origins)
                    EVIDENCE.append(dict(family=family, kind='fresh-parent-checkpoint',
                        out_sha256=actual.output.sha256, checkpoints=len(saved),
                        original_workspace_removed=True, explicit_schema=True, preserved_origins=len(owners)))


if __name__ == '__main__':
    unittest.main()
