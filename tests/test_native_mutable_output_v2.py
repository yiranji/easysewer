from pathlib import Path
import hashlib,tempfile,unittest,shutil
from easysewer.model import Model,FileReference
from easysewer.model.resources import FileTimeSeries
from easysewer.io.inp import InpDocument
from easysewer.runtime import Runner,RunResult,DirectoryAdapter
from easysewer.runtime._directory_tree import inspect_tree
from test_native_v2_lid_report_io import fixture as lid_fixture
from test_runner_v2 import config
from test_flexible_v2 import configuration
from test_native_public_directory_checkpoint_v2 import valid
from directory_roundtrip_fixture import row
import native_directory_lifecycle_fixture as nf
EVIDENCE=[]
def direct(parent,result,family):
 from easysewer.io.json import JsonDocument
 work=parent/'direct';work.mkdir();published=Path(result.input.path).parent
 for path in published.iterdir():
  if path.is_dir() and path.name.startswith('easysewer-assets-'):shutil.copytree(path,work/path.name)
 (work/'model.inp').write_bytes(result.snapshot.input_bytes)
 for resource in result.snapshot.resources:
  if resource.active and resource.access=='write' and resource.kind=='file':
   path=work/resource.relative_path
   if path.exists():path.unlink()
 settings=JsonDocument.from_bytes(result.snapshot.backend_settings).data if family=='custom' else None
 trace=settings.get('trace') if settings else None
 if trace is not None:
  path=work/trace
  if path.exists():path.unlink()
 solver=nf.new_solver(family)
 with nf.working(work):
  try:
   solver.open(['model.inp','model.rpt','model.out'])
   if family=='custom':solver.configure(JsonDocument.from_bytes(result.snapshot.backend_settings).data)
   solver.start(True)
   for _ in range(1000):
    if solver.step(3)['finished']:break
   else:raise AssertionError('direct native completion timeout')
   solver.end();solver.report()
  finally:solver.cleanup()
 if trace is not None:assert (work/trace).read_bytes()==(published/trace).read_bytes()
 lid=next(x for x in result.snapshot.resources if x.role=='swmm:lid-detail')
 return (work/'model.out').read_bytes(),(work/lid.relative_path).read_bytes()

class NativeMutableOutputTests(unittest.TestCase):
 def test_actual_native_input_sibling_and_lid_output_preserve_source_and_archive(self):
  for family in ('standard','custom'):
   with self.subTest(family=family),tempfile.TemporaryDirectory() as tmp:
    root=Path(tmp).resolve();backend=nf.backend(family);expected=None
    for mode in ('baseline','single','nested','prior-linked-output'):
     parent=root/mode;parent.mkdir();source=parent/'source';(source/'sub').mkdir(parents=True);(source/'state').write_bytes(b'initial side state');(source/'rain').write_text(''.join('01/30/2020 00:%02d %s\n'%(n,.6 if n in (2,4) else 0) for n in range(7)))
     if mode=='prior-linked-output':(source/'sub/lid.txt').write_bytes(b'old report');(source/'sub/old-alias').hardlink_to(source/'sub/lid.txt')
     target=parent/'external-lid.txt' if mode=='baseline' else source/'sub/lid.txt';model=Model.from_document(InpDocument.from_text(lid_fixture(detail=str(target))),schema=nf.schema(),strict=True);model.timeseries.replace('Rain',FileTimeSeries(id='Rain',file=FileReference(path=str(source/'rain'))))
     if family=='custom':model.update_options(allow_ponding=True)
     if mode!='baseline':model.collection('test:directory').add(row('D',str(source),access='read_write'))
     if mode=='nested':model.collection('test:directory').add(row('N',str(source/'sub'),access='read_write'));model.collection('test:directory').add(row('R',str(source)))
     original=model.to_json_document().to_bytes();before=inspect_tree(source);runner=Runner(backends={backend.key:backend},directory_adapters={'test:directory.format':DirectoryAdapter(inspector=valid)});settings=(configuration if family=='custom' else config)(parent/'result');result=runner.run(model,settings,relative_to=parent);self.assertTrue(result.succeeded,result.failure);report=next(a for a in result.artifacts if a.role=='swmm:lid-detail');observed=(result.output.read_bytes(),report.read_bytes());self.assertGreater(result.output_metadata.periods,0);self.assertGreater(report.size,1000)
     if expected is None:expected=observed[0]
     else:self.assertEqual(observed[0],expected)
     self.assertEqual(observed,direct(parent,result,family))
     self.assertEqual(inspect_tree(source),before);self.assertEqual(model.to_json_document().to_bytes(),original);result.save(parent/'archive');loaded=RunResult.load(parent/'archive');self.assertEqual(loaded.output.read_bytes(),observed[0]);self.assertEqual(next(a for a in loaded.artifacts if a.role=='swmm:lid-detail').read_bytes(),observed[1])
     if mode!='baseline':
      directory=next(r for r in result.snapshot.resources if r.owner.key=='D');child=next(r for r in result.snapshot.resources if r.role=='swmm:lid-detail');self.assertEqual(Path(child.relative_path),Path(directory.relative_path)/'sub/lid.txt');self.assertEqual((Path(result.input.path).parent/child.relative_path).read_bytes(),observed[1])
     EVIDENCE.append(dict(family=family,kind=mode,periods=result.output_metadata.periods,out_bytes=len(observed[0]),lid_bytes=len(observed[1]),out_sha256=hashlib.sha256(observed[0]).hexdigest(),lid_sha256=hashlib.sha256(observed[1]).hexdigest(),full_output_lid_equal=True,comparison="identical executed INP through direct native solver",source_unchanged=True,archive_equal=True,trace_direct_equal=family=='custom'))
if __name__=='__main__':unittest.main()
