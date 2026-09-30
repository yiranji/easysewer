"""C real engines, four detail streams and reordered checkpoint ownership."""
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
from easysewer.io.report_details import read_lid_report
from easysewer.model import Model, FileReference
from easysewer.runtime import RunResult
from easysewer.utils import probe_library_path
from c_domain_fixture import ORACLE, REPORTS, model, imported, edited, edited_oracle, ref, state
from test_native_v2_runner_checkpoint import reports
from test_native_v2_standard_io import direct_library, execute
from test_scenario_v2 import portable

EVIDENCE = []
FAMILIES = (('standard','swmm5','swmm_getEasySewerStandardFixes'),
            ('custom','flexible_ponding','swmm_getEasySewerNativeIOFixes'))


@unittest.skipUnless(get_native_capabilities()['swmm_solver'] and get_native_capabilities()['flexible_ponding'],
                    'Standard/custom native solvers unavailable')
class NativeCDomainTests(unittest.TestCase):
    def test_two_layer_combinations_and_four_detail_files_match_literal_oracles(self):
        for family,name,symbol in FAMILIES:
            lib,library = direct_library(probe_library_path(name),revision_symbol=symbol)
            hashes = []
            for kind,oracle,value in (('created',ORACLE,model()),('edited',edited_oracle(),edited())):
                with self.subTest(family=family,kind=kind),tempfile.TemporaryDirectory() as directory:
                    restored = Model.from_document(value.to_document(),strict=True)
                    outputs, row_counts = [], []
                    for i,source in enumerate((oracle,value.to_document().text,restored.to_document().text,portable(value).to_document().text)):
                        root = Path(directory)/str(i)
                        root.mkdir()
                        codes = execute(lib,root,source)
                        self.assertFalse(any(codes),(codes,(root/'model.rpt').read_bytes()))
                        details = tuple((root/name).read_bytes() for name in REPORTS)
                        outputs.append(((root/'model.out').read_bytes(),reports((root/'model.rpt').read_bytes()),details))
                        if i == 0:
                            for filename in REPORTS:
                                table = read_lid_report(root/filename,on_error='raise')
                                self.assertEqual(table.status,'present')
                                self.assertTrue(table.rows)
                                self.assertGreater(max(r.cells[2].value for r in table.rows),0)
                                row_counts.append(len(table.rows))
                            self.assertNotEqual(details[0],details[1])
                            with OutputReader(root/'model.out') as reader:
                                self.assertGreater(max(reader.series(ref('links','P'),'swmm:flow').values),0)
                    for actual in outputs[1:]:
                        self.assertEqual(actual,outputs[0])
                    hashes.append(hashlib.sha256(outputs[0][0]).hexdigest())
                    EVIDENCE.append(dict(family=family,kind=kind,comparisons=3,out_sha256=hashes[-1],
                        report_sha256=hashlib.sha256(outputs[0][1]).hexdigest(),detail_rows=row_counts,
                        detail_sha256={name:hashlib.sha256(raw).hexdigest() for name,raw in zip(REPORTS,outputs[0][2])},
                        library_sha256=hashlib.sha256(library.read_bytes()).hexdigest()))
            self.assertEqual(len(set(hashes)),2)

    def test_reordered_placements_preserve_four_artifact_owners_on_fresh_resume(self):
        import easysewer
        from test_native_v2_runner_checkpoint import NativeRunnerCheckpointTests
        helper = NativeRunnerCheckpointTests()
        package = str(Path(easysewer.__file__).resolve().parent.parent)
        tests = str(Path(__file__).resolve().parent)
        child = '''import sys
from pathlib import Path
package,tests,family,checkpoint,output,archive=sys.argv[1:]
sys.path[:0]=[package,tests]
import easysewer
assert Path(easysewer.__file__).resolve().is_relative_to(Path(package).resolve())
from c_domain_fixture import schema
from test_native_v2_runner_checkpoint import runner,resume_config
value=runner(family).resume(checkpoint,resume_config(Path(output)),schema=schema())
assert value.succeeded,(value.failure,value.diagnostics)
value.save(archive)
'''
        with patch.dict(os.environ,EASYSEWER_CHECKPOINT_PACKAGED='1'):
            for family,_,_ in FAMILIES:
                with self.subTest(family=family),tempfile.TemporaryDirectory() as directory:
                    root = Path(directory).resolve()
                    value = edited(imported())
                    targets = []
                    for key in value.lid_usage:
                        row = value.lid_usage[key]
                        path = root/row.report_file.path
                        targets.append(path)
                        value.lid_usage.update(key,report_file=FileReference(path=str(path),direction='output'))
                    before = state(value)
                    owners = tuple(ref('lid_usage',key) for key in value.lid_usage)
                    origins = tuple(value.provenance(owner) for owner in owners)
                    original,saved = helper.original(root,family,model=value)
                    helper.success(original)
                    self.assertTrue(saved)
                    original.save(root/'expected')
                    expected = RunResult.load(root/'expected')
                    self.assertEqual(state(expected.snapshot.model()),before)
                    expected_tables = []
                    for owner in owners:
                        table = expected.read_lid_report(owner,on_error='raise')
                        self.assertEqual(table.target,owner)
                        self.assertEqual(table.status,'present')
                        self.assertTrue(table.rows)
                        expected_tables.append(table)
                    for path in [root/'first',root/'saved',root/'moved',root/'original.hsf',*targets]:
                        self.assertTrue(path.resolve().is_relative_to(root))
                    shutil.rmtree(root/'first')
                    (root/'original.hsf').unlink()
                    for path in targets:
                        self.assertTrue(path.exists())
                        path.unlink()
                    (root/'saved').rename(root/'moved')
                    process = subprocess.run([sys.executable,'-I','-B','-c',child,package,tests,family,
                        str(root/'moved'/saved[0].directory.name),str(root/'resumed'),str(root/'result')],
                        capture_output=True,text=True,timeout=90,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                    self.assertEqual(process.returncode,0,process.stdout+process.stderr)
                    actual = RunResult.load(root/'result')
                    helper.equivalent(expected,actual)
                    restored = actual.snapshot.model()
                    self.assertEqual(state(restored),before)
                    self.assertEqual(tuple(restored.provenance(owner) for owner in owners),origins)
                    for owner,table in zip(owners,expected_tables):
                        other = actual.read_lid_report(owner,on_error='raise')
                        self.assertEqual(other.target,owner)
                        self.assertEqual(other.title,table.title)
                        self.assertEqual(other.raw_text,table.raw_text)
                        self.assertEqual(other.columns,table.columns)
                        self.assertEqual(tuple(tuple(c.value for c in r.cells) for r in other.rows),
                                         tuple(tuple(c.value for c in r.cells) for r in table.rows))
                    EVIDENCE.append(dict(family=family,kind='fresh-parent-checkpoint',out_sha256=actual.output.sha256,
                        checkpoints=len(saved),reordered_usage_ids=[o.key for o in owners],detail_files=4,
                        source_files_removed=True,origins_preserved=4))


if __name__ == '__main__':
    unittest.main()
