from pathlib import Path
import hashlib,tempfile,unittest
from easysewer.model import Model
from easysewer.io.inp import InpDocument
from easysewer.runtime import Runner,RunResult,DirectoryAdapter
from test_native_v2_lid_report_io import fixture as lid_fixture
from test_runner_v2 import config
from test_flexible_v2 import configuration
from test_native_public_directory_checkpoint_v2 import valid
from directory_roundtrip_fixture import row
import native_directory_lifecycle_fixture as nf
EVIDENCE=[]
class NativeOutputContainmentTests(unittest.TestCase):
 def test_actual_native_lid_child_output_is_published_and_archived_in_its_declared_tree(self):
  for family in ('standard','custom'):
   with self.subTest(family=family),tempfile.TemporaryDirectory() as tmp:
    root=Path(tmp).resolve();backend=nf.backend(family);raw=[]
    for nested in (False,True):
     parent=root/('nested' if nested else 'baseline');parent.mkdir();target=parent/'reports';model=Model.from_document(InpDocument.from_text(lid_fixture(detail=str(target/'sub/lid.txt'))),schema=nf.schema(),strict=True)
     if family=='custom':model.update_options(allow_ponding=True)
     if nested:model.collection('test:directory').add(row('D',str(target),access='write'));model.collection('test:directory').add(row('N',str(target/'sub'),access='write'))
     original=model.to_json_document().to_bytes();runner=Runner(backends={backend.key:backend},directory_adapters={'test:directory.format':DirectoryAdapter(inspector=valid)});settings=(configuration if family=='custom' else config)(parent/'main');result=runner.run(model,settings,relative_to=parent);self.assertTrue(result.succeeded,result.failure);self.assertGreater(result.output_metadata.periods,0);raw.append(result.output.read_bytes());self.assertEqual(model.to_json_document().to_bytes(),original)
     report=next(a for a in result.artifacts if a.role=='swmm:lid-detail');self.assertEqual((target/'sub/lid.txt').read_bytes(),report.read_bytes());self.assertGreater(report.size,1000)
     if nested:
      records={v.owner.key:v for v in result.snapshot.resources if v.owner.collection=='test:directory'};child=next(v for v in result.snapshot.resources if v.role=='swmm:lid-detail');self.assertEqual(Path(child.relative_path),Path(records['D'].relative_path)/'sub/lid.txt');self.assertEqual(Path(records['N'].relative_path),Path(records['D'].relative_path)/'sub');dirs={v.owner.key:v for v in result.directory_artifacts};self.assertEqual(dirs['D'].file('sub/lid.txt').read_bytes(),report.read_bytes());self.assertEqual(dirs['N'].file('lid.txt').read_bytes(),report.read_bytes());result.save(parent/'archive');loaded=RunResult.load(parent/'archive');self.assertTrue(loaded.succeeded);self.assertEqual(loaded.output.read_bytes(),raw[-1]);self.assertEqual(next(a for a in loaded.directory_artifacts if a.owner.key=='D').file('sub/lid.txt').read_bytes(),report.read_bytes())
      EVIDENCE.append(dict(family=family,periods=result.output_metadata.periods,out_bytes=len(raw[-1]),lid_bytes=report.size,child_containment=True,published_equal=True,result_archive_equal=True,out_sha256=hashlib.sha256(raw[-1]).hexdigest()))
    self.assertEqual(raw[0],raw[1]);EVIDENCE[-1]['full_output_equal_without_directory_declaration']=True
if __name__=='__main__':unittest.main()