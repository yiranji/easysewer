from datetime import timedelta
from pathlib import Path
import shutil,tempfile,unittest
from easysewer.runtime import Runner,DirectoryAdapter,Checkpoint,CheckpointSchedule,RunResult,ResumeConfig
from easysewer.model import FileReference
from easysewer.io.interface_inspection import InterfaceInspection
from easysewer.io.output_metadata import OutputMetadata
from test_runner_v2 import config
from test_flexible_v2 import configuration
import native_directory_lifecycle_fixture as fixture
EVIDENCE=[]
def valid(path,**kwargs):return InterfaceInspection(format=kwargs['use'].format,status='validated')
def finish(session):
 for _ in range(1000):
  if session.step(max_steps=3).finished:break
 else:raise AssertionError('Did not finish native session')
 outputs=session.checkpoint_outputs;session.end();session.report();session.close();path=next(a.path for a in outputs if a.role=='run:output');meta=OutputMetadata.read(path);assert meta.periods==10 and meta.names('swmm:nodes')==('J','O') and meta.names('swmm:links')==('P',);return path.read_bytes()
class NativePublicDirectoryCheckpointTests(unittest.TestCase):
 def test_public_native_session_moved_directory_checkpoint(self):
  for family in ('standard','custom'):
   with self.subTest(family=family),tempfile.TemporaryDirectory() as tmp:
    root=Path(tmp).resolve();source,work,value=fixture.prepare(root,family);backend=fixture.backend(family)
    with backend.session(working_directory=work) as session:session.open_checkpoint(value,schema=fixture.schema());session.step(max_steps=30);saved=session.save_checkpoint(root/'saved');self.assertGreaterEqual(saved.simulation_seconds,120);expected=finish(session)
    for path in (source,work):self.assertTrue(path.resolve().is_relative_to(root));shutil.rmtree(path)
    (root/'saved').rename(root/'moved');saved=Checkpoint.load(root/'moved');snapshot=saved.materialize(root/'restored',schema=fixture.schema())
    with backend.session(working_directory=root/'restored') as session:session.open_checkpoint(snapshot,schema=fixture.schema());restored=session.restore_checkpoint(saved);self.assertTrue(restored.committed);self.assertEqual(finish(session),expected)
    EVIDENCE.append(dict(kind='public-session',family=family,periods=10,bytes=len(expected),full_output_equal=True))
 def test_public_runner_directory_checkpoint_resume_and_archive(self):
  for family in ('standard','custom'):
   with self.subTest(family=family),tempfile.TemporaryDirectory() as tmp:
    root=Path(tmp).resolve();source,work,value=fixture.prepare(root,family);model=value.model(schema=fixture.schema());backend=fixture.backend(family);runner=Runner(backends={backend.key:backend},directory_adapters={'test:directory.format':DirectoryAdapter(inspector=valid)});saved=[];settings=(configuration if family=='custom' else config)(root/'first',step_batch_size=30)
    result=runner.run(model,settings,relative_to=work,checkpoints=CheckpointSchedule(directory=FileReference(path=str(root/'saved'),direction='output'),interval=timedelta(seconds=150),on_saved=saved.append));self.assertTrue(result.succeeded,result.failure);self.assertTrue(saved);self.assertEqual(result.output_metadata.periods,10);self.assertTrue(result.directory_group_artifacts);expected=result.output.read_bytes();result.save(root/'result');self.assertEqual(RunResult.load(root/'result').output.read_bytes(),expected);relative=saved[0].directory.relative_to(root/'saved')
    for path in (source,work,root/'first'):self.assertTrue(path.resolve().is_relative_to(root));shutil.rmtree(path)
    (root/'saved').rename(root/'moved');resumed=runner.resume(root/'moved'/relative,ResumeConfig(output_directory=FileReference(path=str(root/'resumed'),direction='output')),schema=fixture.schema());self.assertTrue(resumed.succeeded,resumed.failure);self.assertEqual(resumed.output.read_bytes(),expected);self.assertTrue(resumed.directory_group_artifacts);resumed.save(root/'again');self.assertEqual(RunResult.load(root/'again').output.read_bytes(),expected)
    EVIDENCE.append(dict(kind='public-runner',family=family,periods=10,bytes=len(expected),full_output_equal=True,result_archive=True))
if __name__=='__main__':unittest.main()
