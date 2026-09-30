from pathlib import Path
from datetime import timedelta
import hashlib,shutil,tempfile,unittest
from easysewer.model import FileReference
from easysewer.runtime import Checkpoint,Runner,RunResult,DirectoryAdapter,CheckpointSchedule,ResumeConfig
from easysewer.runtime._directory_tree import inspect_tree
from easysewer.runtime._checkpoint_directory_outputs import decode
from native_mutable_output_checkpoint_fixture import prepare,finish,model_at,nf,valid
from test_runner_v2 import config
from test_flexible_v2 import configuration
EVIDENCE=[]
class NativeMutableOutputCheckpointTests(unittest.TestCase):
 def test_public_repeated_restore_recapture_binds_mutable_native_and_trace_outputs(self):
  for family in ('standard','custom'):
   with self.subTest(family=family),tempfile.TemporaryDirectory() as tmp:
    root=Path(tmp).resolve();base=root/'base';base.mkdir();_,bw,bv,_=prepare(base,family)
    with nf.backend(family).session(working_directory=bw) as session:session.open_checkpoint(bv,schema=nf.schema());expected=finish(session,bw,bv)
    run=root/'run';run.mkdir();source,work,value,current=prepare(run,family);original=inspect_tree(source)
    with nf.backend(family).session(working_directory=work) as session:
     session.open_checkpoint(value,schema=nf.schema());session.step(max_steps=24);(current/'side').write_bytes(b'saved');(current/'side-alias').hardlink_to(current/'side');(current/'empty').mkdir();saved=session.save_checkpoint(root/'saved');self.assertEqual(saved._archive.data['schema_version'],'1.8');self.assertEqual(saved._archive.data['output_directory_states'],[]);states,bindings=decode(saved._archive.data,value);self.assertEqual(states,{});self.assertEqual(len(bindings['outputs']),1);self.assertEqual(bindings['trace'] is not None,family=='custom');tree=inspect_tree(current)
     for index in range(2):
      session.step(max_steps=3);(current/'side-alias').unlink();(current/'side').write_bytes(b'later');(current/'later').write_bytes(b'extra');(current/'empty').rmdir();restored=session.restore_checkpoint(saved);self.assertTrue(restored.committed);self.assertEqual(inspect_tree(current),tree)
      for output in restored.outputs:
       if output.directory is not None:self.assertTrue(output.path.samefile(output.destination));self.assertTrue(current.is_relative_to(output.directory))
      recaptured=session.save_checkpoint(root/('again-'+str(index)));self.assertEqual(recaptured.simulation_seconds,saved.simulation_seconds);self.assertEqual(decode(recaptured._archive.data,value),(states,bindings))
     observed=finish(session,work,value);self.assertEqual(observed,expected);self.assertEqual((current/'sub/lid.txt').read_bytes(),expected[1]);self.assertEqual((current/'sub/alias').read_bytes(),b'prior report')
    self.assertEqual(inspect_tree(source),original);EVIDENCE.append(dict(family=family,kind='live',restores=2,recaptures=2,source_unchanged=True,full_out_lid_trace_equal=True,no_duplicate_output_tree=True,bytes=len(expected[0])))
 def test_moved_archive_materializes_inputs_without_precreating_native_outputs(self):
  for family in ('standard','custom'):
   with self.subTest(family=family),tempfile.TemporaryDirectory() as tmp:
    root=Path(tmp).resolve();source,work,value,current=prepare(root,family)
    with nf.backend(family).session(working_directory=work) as session:
     session.open_checkpoint(value,schema=nf.schema());session.step(max_steps=24);(current/'side').write_bytes(b'saved');saved=session.save_checkpoint(root/'saved');tree=inspect_tree(current);expected=finish(session,work,value)
    for path in (source,work):self.assertTrue(path.resolve().is_relative_to(root));shutil.rmtree(path)
    (root/'saved').rename(root/'moved');saved=Checkpoint.load(root/'moved');target=root/'restored';value=saved.materialize(target,schema=nf.schema());directory=next(x for x in value.resources if x.owner.key=='D');current=target/directory.relative_path;self.assertFalse((current/'sub/lid.txt').exists());self.assertEqual((current/'sub/alias').read_bytes(),b'prior report')
    with nf.backend(family).session(working_directory=target) as session:session.open_checkpoint(value,schema=nf.schema());restored=session.restore_checkpoint(saved);self.assertTrue(restored.committed);self.assertEqual(inspect_tree(current),tree);session.save_checkpoint(root/'recaptured');self.assertEqual(finish(session,target,value),expected)
    EVIDENCE.append(dict(family=family,kind='moved',original_source_and_workspace_deleted=True,recaptured=True,full_out_lid_trace_equal=True,bytes=len(expected[0])))
 def test_runner_checkpoint_resume_collects_mutable_report_in_result_archive(self):
  for family in ('standard','custom'):
   with self.subTest(family=family),tempfile.TemporaryDirectory() as tmp:
    root=Path(tmp).resolve();source,model=model_at(root,family);original=inspect_tree(source);backend=nf.backend(family);runner=Runner(backends={backend.key:backend},directory_adapters={'test:directory.format':DirectoryAdapter(inspector=valid)});saved=[]
    first=runner.run(model,(configuration if family=='custom' else config)(root/'first',step_batch_size=12),relative_to=root,checkpoints=CheckpointSchedule(directory=FileReference(path=str(root/'saved'),direction='output'),interval=timedelta(seconds=30),on_saved=saved.append));self.assertTrue(first.succeeded,first.failure);self.assertTrue(saved);expected=(first.output.read_bytes(),next(a for a in first.artifacts if a.role=='swmm:lid-detail').read_bytes());again=[]
    resumed=runner.resume(saved[0],ResumeConfig(output_directory=FileReference(path=str(root/'resumed'),direction='output'),step_batch_size=12),schema=nf.schema(),checkpoints=CheckpointSchedule(directory=FileReference(path=str(root/'again'),direction='output'),interval=timedelta(seconds=30),on_saved=again.append));self.assertTrue(resumed.succeeded,resumed.failure);self.assertTrue(again);self.assertEqual((resumed.output.read_bytes(),next(a for a in resumed.artifacts if a.role=='swmm:lid-detail').read_bytes()),expected);resumed.save(root/'archive');loaded=RunResult.load(root/'archive');self.assertEqual(loaded.output.read_bytes(),expected[0]);self.assertEqual(next(a for a in loaded.artifacts if a.role=='swmm:lid-detail').read_bytes(),expected[1]);self.assertEqual(inspect_tree(source),original)
    EVIDENCE.append(dict(family=family,kind='runner',recaptured=True,full_out_lid_equal=True,result_archive_equal=True,source_unchanged=True,bytes=len(expected[0])))
if __name__=='__main__':unittest.main()
