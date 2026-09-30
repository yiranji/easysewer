from pathlib import Path
import shutil,tempfile,unittest
from easysewer.runtime._checkpoint_lifecycle import CheckpointLifecycle
from easysewer.runtime._checkpoint_container import load
import native_directory_lifecycle_fixture as fixture
EVIDENCE=[]
class NativeDirectoryInputsTests(unittest.TestCase):
 def test_actual_native_directory_consumption_and_moved_checkpoint_restore(self):
  for family in ('standard','custom'):
   with self.subTest(family=family),tempfile.TemporaryDirectory() as tmp:
    root=Path(tmp).resolve();source,work,value=fixture.prepare(root,family)
    with fixture.working(work):
     owner=CheckpointLifecycle(fixture.new_solver(family),value,schema=fixture.schema())
     try:
      owner.open_start();self.assertIsNotNone(owner.directory_inputs);self.assertEqual(len(owner.originals),1);owner.step(30);saved=owner.capture(root/'saved');self.assertGreaterEqual(saved.simulation_seconds,120);expected=fixture.finish(owner)
     finally:
      if owner.state!='CLOSED':owner.cleanup()
    for path in (source,work):self.assertTrue(path.resolve().is_relative_to(root));shutil.rmtree(path)
    (root/'saved').rename(root/'moved');saved=load(root/'moved');restored=saved.materialize(root/'restored',schema=fixture.schema())
    with fixture.working(root/'restored'):
     owner=CheckpointLifecycle(fixture.new_solver(family),restored,schema=fixture.schema())
     try:owner.open_start();outcome=owner.restore(saved);self.assertTrue(outcome.native.committed);self.assertEqual(fixture.finish(owner),expected)
     finally:
      if owner.state!='CLOSED':owner.cleanup()
    EVIDENCE.append(dict(family=family,restored=True,full_output_equal=True,bytes=len(expected),periods=10,nodes=2,links=1,checkpoint_seconds=saved.simulation_seconds,public_worker=False))
 def test_changed_membership_stops_real_native_lifecycle_before_next_step(self):
  for family in ('standard','custom'):
   with self.subTest(family=family),tempfile.TemporaryDirectory() as tmp:
    root=Path(tmp).resolve();_,work,value=fixture.prepare(root,family)
    with fixture.working(work):
     owner=CheckpointLifecycle(fixture.new_solver(family),value,schema=fixture.schema())
     try:
      owner.open_start();owner.step(30);record=next(r for r in value.resources if r.kind=='directory' and r.owner.collection=='test:directory');(work/record.relative_path/'extra').mkdir()
      with self.assertRaises((ValueError,OSError)):owner.step(30)
      self.assertEqual(owner.state,'FAILED')
     finally:owner.cleanup()
    EVIDENCE.append(dict(family=family,membership_rejected=True,public_worker=False))
if __name__=='__main__':unittest.main()
