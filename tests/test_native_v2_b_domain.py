"""B native contract: both pollutants act independently alongside FLOW and DWF."""
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
from b_domain_fixture import ORACLE, model, imported, edited, edited_oracle, ref, state
from test_native_v2_runner_checkpoint import reports
from test_native_v2_standard_io import direct_library, execute
from test_scenario_v2 import portable

EVIDENCE = []
FAMILIES = (('standard', 'swmm5', 'swmm_getEasySewerStandardFixes'),
            ('custom', 'flexible_ponding', 'swmm_getEasySewerNativeIOFixes'))


def converted():
    value = model()
    value.convert_pollutant_units('A', 'UG/L')
    value.convert_pollutant_units('B', 'MG/L')
    return value


CONVERTED_ORACLE = ORACLE.replace('A MG/L', 'A UG/L').replace('B UG/L', 'B MG/L').replace(
    'C 0 .5', 'C 0 500').replace('C 00:10 1', 'C 00:10 1000').replace(
    'CONCEN 1 1 2 ', 'CONCEN 1 1 2000 ').replace('MASS 126 ', 'MASS .126 ').replace(
    'J A 4 ', 'J A 4000 ').replace('J B 30 ', 'J B .03 ')


def observe(lib, root, source):
    root.mkdir()
    codes = execute(lib, root, source)
    if any(codes):
        raise AssertionError((codes, (root/'model.rpt').read_bytes()))
    return ((root/'model.out').read_bytes(), reports((root/'model.rpt').read_bytes()))


@unittest.skipUnless(get_native_capabilities()['swmm_solver'] and get_native_capabilities()['flexible_ponding'],
                    'Standard/custom native solvers unavailable')
class NativeBDomainTests(unittest.TestCase):
    def test_handwritten_oracles_for_created_edited_and_unit_converted_model(self):
        for family, name, symbol in FAMILIES:
            lib, library = direct_library(probe_library_path(name), revision_symbol=symbol)
            hashes = []
            for kind, oracle, value in (('created', ORACLE, model()),
                ('edited', edited_oracle(), edited()), ('pollutant-units', CONVERTED_ORACLE, converted())):
                with self.subTest(family=family, kind=kind), tempfile.TemporaryDirectory() as directory:
                    base = Path(directory)
                    restored = Model.from_document(value.to_document(), strict=True)
                    sources = (oracle, value.to_document().text, restored.to_document().text, portable(value).to_document().text)
                    outputs = [observe(lib, base/str(i), text) for i, text in enumerate(sources)]
                    for actual in outputs[1:]:
                        self.assertEqual(actual, outputs[0])
                    with OutputReader(base/'0/model.out') as reader:
                        pollutants = ('Solids', 'Tracer') if kind == 'edited' else ('A', 'B')
                        peaks = {}
                        for key in pollutants:
                            series = reader.series(ref('links', 'P'), 'swmm:concentration', pollutant=ref('pollutants', key))
                            self.assertTrue(series.values)
                            peaks[key] = max(series.values)
                            self.assertGreater(peaks[key], 0)
                        self.assertGreater(max(reader.series(ref('links', 'P'), 'swmm:flow').values), 0)
                    hashes.append(hashlib.sha256(outputs[0][0]).hexdigest())
                    EVIDENCE.append(dict(family=family, kind=kind, comparisons=3, out_sha256=hashes[-1],
                        report_sha256=hashlib.sha256(outputs[0][1]).hexdigest(), concentration_peaks=peaks,
                        library_sha256=hashlib.sha256(library.read_bytes()).hexdigest()))
            self.assertEqual(len(set(hashes)), 3)

    def test_removing_each_pollutant_keeps_the_other_and_hydraulics_unchanged(self):
        for family, name, symbol in FAMILIES:
            lib, _ = direct_library(probe_library_path(name), revision_symbol=symbol)
            with self.subTest(family=family), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                histories = []
                for removed in (None, 'A', 'B'):
                    value = model()
                    if removed:
                        for collection in (value.inflows, value.dwf):
                            collection.remove(('J', 'POLLUTANT:'+removed))
                    folder = root/(removed or 'both')
                    observe(lib, folder, value.to_document().text)
                    with OutputReader(folder/'model.out') as reader:
                        flow = reader.series(ref('links', 'P'), 'swmm:flow').values
                        quality = {key: reader.series(ref('links', 'P'), 'swmm:concentration',
                            pollutant=ref('pollutants', key)).values for key in ('A', 'B')}
                    histories.append((flow, quality))
                flow, quality = histories[0]
                for index, removed, other in ((1, 'A', 'B'), (2, 'B', 'A')):
                    actual_flow, actual = histories[index]
                    self.assertEqual(actual_flow, flow)
                    self.assertEqual(actual[other], quality[other])
                    self.assertTrue(all(v == 0 for v in actual[removed]))
                    self.assertGreater(max(quality[removed]), 0)
                    EVIDENCE.append(dict(family=family, kind='independent-constituent', removed=removed,
                        flow_unchanged=True, other_pollutant_unchanged=True, removed_concentration_zero=True))

    def test_joint_model_archive_and_fresh_parent_checkpoint(self):
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
from b_domain_fixture import schema
from test_native_v2_runner_checkpoint import runner, resume_config
value = runner(family).resume(checkpoint, resume_config(Path(output)), schema=schema())
assert value.succeeded, (value.failure, value.diagnostics)
value.save(archive)
'''
        with patch.dict(os.environ, EASYSEWER_CHECKPOINT_PACKAGED='1'):
            for family, _, _ in FAMILIES:
                with self.subTest(family=family), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory).resolve()
                    value = edited(imported())
                    before = state(value)
                    owners = tuple(ref(c, ('Tank', k)) for c in ('inflows', 'dwf')
                                   for k in ('FLOW', 'POLLUTANT:Solids', 'POLLUTANT:Tracer'))
                    origins = tuple(value.provenance(owner) for owner in owners)
                    original, saved = helper.original(root, family, model=value)
                    helper.success(original)
                    self.assertTrue(saved)
                    self.assertGreater(saved[0].simulation_seconds, 0)
                    self.assertLess(saved[0].simulation_seconds, 600)
                    original.save(root/'expected')
                    expected = RunResult.load(root/'expected')
                    self.assertEqual(state(expected.snapshot.model()), before)
                    for name in ('first', 'original.hsf', 'saved', 'moved'):
                        self.assertTrue((root/name).resolve().is_relative_to(root))
                    shutil.rmtree(root/'first')
                    (root/'original.hsf').unlink()
                    (root/'saved').rename(root/'moved')
                    process = subprocess.run([sys.executable, '-I', '-B', '-c', child, package, tests,
                        family, str(root/'moved'/saved[0].directory.name), str(root/'resumed'), str(root/'result')],
                        capture_output=True, text=True, timeout=90,
                        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                    self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
                    actual = RunResult.load(root/'result')
                    helper.equivalent(expected, actual)
                    restored = actual.snapshot.model()
                    self.assertEqual(state(restored), before)
                    self.assertEqual(tuple(restored.provenance(owner) for owner in owners), origins)
                    EVIDENCE.append(dict(family=family, kind='fresh-parent-checkpoint',
                        out_sha256=actual.output.sha256, checkpoints=len(saved), preserved_origins=len(owners),
                        original_workspace_removed=True))


if __name__ == '__main__':
    unittest.main()
