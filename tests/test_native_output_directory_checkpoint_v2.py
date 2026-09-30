from pathlib import Path
from datetime import timedelta
import hashlib,json,shutil,tempfile,unittest
from easysewer.runtime import Checkpoint,Runner,DirectoryAdapter,CheckpointSchedule,ResumeConfig,RunResult
from easysewer.model import Model,FileReference
from easysewer.io.inp import InpDocument
from easysewer.runtime._directory_tree import inspect_tree
from easysewer.runtime._checkpoint_directory_outputs import decode
from native_output_checkpoint_fixture import prepare,nf,lid_fixture,row,valid
from test_runner_v2 import config
from test_flexible_v2 import configuration
EVIDENCE=[]

def finish(session):
 for _ in range(1000):
  if session.step(max_steps=3).finished:break
 else:raise AssertionError('native completion timeout')
 outputs=session.checkpoint_outputs;session.end();session.report();session.close()
 raw=next(x.path for x in outputs if x.role=='run:output').read_bytes();lid=next(x.path for x in outputs if x.role=='swmm:lid_report').read_bytes()
 return raw,lid

class NativeOutputDirectoryCheckpointTests(unittest.TestCase):
 def test_repeated_public_restore_rewinds_sidefiles_and_native_streams_then_recaptures(self):
  for family in ('standard','custom'):
   with self.subTest(family=family),tempfile.TemporaryDirectory() as tmp:
    root=Path(tmp).resolve();base=root/'base';base.mkdir();bw,bv=prepare(base,family)
    with nf.backend(family).session(working_directory=bw) as session:session.open_checkpoint(bv,schema=nf.schema());expected=finish(session)
    run=root/'run';run.mkdir();work,value=prepare(run,family);target=work/'assets/out'
    with nf.backend(family).session(working_directory=work) as session:
     session.open_checkpoint(value,schema=nf.schema());session.step(max_steps=24);(target/'side').write_bytes(b'saved');(target/'alias').hardlink_to(target/'side');(target/'empty').mkdir();saved=session.save_checkpoint(root/'saved');self.assertEqual(saved._archive.data['schema_version'],'1.7');tree=inspect_tree(target);states,bindings=decode(saved._archive.data,value);self.assertEqual(states,{'assets/out':tree});self.assertEqual(len(bindings['outputs']),1);self.assertEqual(bindings['trace'] is not None,family=='custom')
     for attempt in range(2):
      session.step(max_steps=3);(target/'alias').unlink();(target/'side').write_bytes(b'later');(target/'extra').write_bytes(b'extra');(target/'empty').rmdir();restored=session.restore_checkpoint(saved);self.assertTrue(restored.committed);self.assertEqual(inspect_tree(target),tree);self.assertTrue((target/'side').samefile(target/'alias'))
      for output in restored.outputs:
       if output.directory is not None:self.assertTrue(output.path.samefile(output.destination));self.assertEqual(output.directory,target)
      again=session.save_checkpoint(root/('again-'+str(attempt)));self.assertEqual(decode(again._archive.data,value), (states,bindings));self.assertEqual(again.simulation_seconds,saved.simulation_seconds)
     actual=finish(session);self.assertEqual(actual,expected);self.assertEqual((target/'sub/lid.txt').read_bytes(),expected[1])
    EVIDENCE.append(dict(family=family,kind='public-live',restores=2,recaptures=2,side_tree_rewound=True,hardlinks=True,nested_roots=True,trace_bound=family=='custom',full_out_equal=True,full_lid_equal=True,out_bytes=len(actual[0]),lid_bytes=len(actual[1])))
 def test_moved_archive_rebuilds_empty_startup_then_restores_outputs(self):
  for family in ('standard','custom'):
   with self.subTest(family=family),tempfile.TemporaryDirectory() as tmp:
    root=Path(tmp).resolve();work,value=prepare(root,family);target=work/'assets/out'
    with nf.backend(family).session(working_directory=work) as session:
     session.open_checkpoint(value,schema=nf.schema());session.step(max_steps=24);(target/'side').write_bytes(b'saved');saved=session.save_checkpoint(root/'saved');tree=inspect_tree(target);expected=finish(session)
    self.assertTrue(work.resolve().is_relative_to(root));shutil.rmtree(work);(root/'saved').rename(root/'moved');saved=Checkpoint.load(root/'moved');value=saved.materialize(root/'restored',schema=nf.schema());target=root/'restored/assets/out';self.assertFalse((target/'sub/lid.txt').exists());self.assertFalse((target/'side').exists())
    with nf.backend(family).session(working_directory=root/'restored') as session:
     session.open_checkpoint(value,schema=nf.schema());session.restore_checkpoint(saved);self.assertEqual(inspect_tree(target),tree);session.save_checkpoint(root/'again');self.assertEqual(finish(session),expected)
    EVIDENCE.append(dict(family=family,kind='public-moved',startup_outputs_absent=True,side_tree_rewound=True,recaptured=True,full_out_equal=True,full_lid_equal=True))
 def test_runner_resume_adopts_bound_streams_and_archives_output_tree(self):
  for family in ('standard','custom'):
   with self.subTest(family=family),tempfile.TemporaryDirectory() as tmp:
    root=Path(tmp).resolve();target=root/'reports';model=Model.from_document(InpDocument.from_text(lid_fixture(detail=str(target/'sub/lid.txt'))),schema=nf.schema(),strict=True);model.collection('test:directory').add(row('O',str(target),access='write'));model.collection('test:directory').add(row('N',str(target/'sub'),access='write'))
    if family=='custom':model.update_options(allow_ponding=True)
    backend=nf.backend(family);runner=Runner(backends={backend.key:backend},directory_adapters={'test:directory.format':DirectoryAdapter(inspector=valid)});saved=[]
    def after_save(value):
     saved.append(value);record=next(r for r in value.snapshot.resources if r.owner.collection=='test:directory' and r.owner.key=='O');path=Path(value.snapshot.execution_directory)/record.relative_path;(path/'after-first-save').write_bytes(b'later')
    first=runner.run(model,(configuration if family=='custom' else config)(root/'first',step_batch_size=12),relative_to=root,checkpoints=CheckpointSchedule(directory=FileReference(path=str(root/'saved'),direction='output'),interval=timedelta(seconds=30),on_saved=after_save));self.assertTrue(first.succeeded,first.failure);self.assertGreater(len(saved),0);expected=first.output.read_bytes();expected_lid=next(a for a in first.artifacts if a.role=='swmm:lid-detail').read_bytes();again=[]
    resumed=runner.resume(saved[0],ResumeConfig(output_directory=FileReference(path=str(root/'resumed'),direction='output'),step_batch_size=12),schema=nf.schema(),checkpoints=CheckpointSchedule(directory=FileReference(path=str(root/'again'),direction='output'),interval=timedelta(seconds=30),on_saved=again.append));self.assertTrue(resumed.succeeded,resumed.failure);self.assertTrue(again);self.assertEqual(resumed.output.read_bytes(),expected);self.assertEqual(next(a for a in resumed.artifacts if a.role=='swmm:lid-detail').read_bytes(),expected_lid)
    dirs={a.owner.key:a for a in resumed.directory_artifacts};self.assertEqual(dirs['O'].file('sub/lid.txt').read_bytes(),expected_lid);self.assertNotIn('after-first-save',{e.path for e in dirs['O'].manifest.entries});resumed.save(root/'result');loaded=RunResult.load(root/'result');self.assertEqual(loaded.output.read_bytes(),expected);self.assertEqual(next(a for a in loaded.directory_artifacts if a.owner.key=='O').file('sub/lid.txt').read_bytes(),expected_lid)
    EVIDENCE.append(dict(family=family,kind='runner',restored_streams_adopted=True,recaptured=True,later_side_file_removed=True,full_out_equal=True,full_lid_equal=True,result_archive_equal=True))

if __name__=='__main__':unittest.main()
