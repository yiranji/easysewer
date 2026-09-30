from pathlib import Path
from dataclasses import replace
import tempfile,unittest
from easysewer.runtime import RunResult
from easysewer.runtime._preparation import inventory,stage
from easysewer.runtime._directory_outputs import layout
from directory_roundtrip_fixture import prepared,row
import test_runner_directories_v2 as runner_fixture

class OutputContainmentTests(unittest.TestCase):
 def model(self,root,order='file-first'):
  rows=[row('F','target/sub/data',kind='file',access='write'),row('N','target/sub',access='write'),row('D','target',access='write')]
  if order=='directory-first':rows.reverse()
  return prepared(root,tuple(rows))[1]
 def test_stage_preserves_nested_paths_independent_of_declaration_order(self):
  for order in ('file-first','directory-first'):
   with self.subTest(order=order),tempfile.TemporaryDirectory() as tmp:
    root=Path(tmp).resolve();model=self.model(root,order);runner,_=runner_fixture.RunnerDirectoriesTests().runner(model);plans=inventory(model,input_directory=root,working_directory=root,directory_adapters=runner.directory_adapters);out=layout(plans,'assets');self.assertEqual(out.targets,((root/'target',True),));work=root/'work';work.mkdir();resources,outputs,_=stage(model,plans,work,'assets',checkpoint=lambda:None);paths={r.owner.key:work/r.relative_path for r in resources if r.owner.collection=='test:directory'}
    self.assertEqual(paths['N'],paths['D']/'sub');self.assertEqual(paths['F'],paths['N']/'data');self.assertTrue(paths['N'].is_dir());self.assertFalse((root/'target').exists());self.assertEqual(len(outputs),3)
 def test_public_runner_publishes_tree_and_child_artifacts_once(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp).resolve();model=self.model(root);original=model.to_json_document().to_bytes();helper=runner_fixture.RunnerDirectoriesTests()
   def action(s):
    self.assertEqual(s.paths['F'],s.paths['D']/'sub/data');(s.paths['N']/'empty').mkdir();s.paths['F'].write_bytes(b'full output')
   result,backend=helper.run_model(root,model,action);self.assertTrue(result.succeeded,result.failure);self.assertEqual((root/'target/sub/data').read_bytes(),b'full output');self.assertTrue((root/'target/sub/empty').is_dir());paths=helper.published(result);self.assertEqual(paths['F'],paths['D']/'sub/data');self.assertEqual(model.to_json_document().to_bytes(),original)
   result.save(root/'archive');loaded=RunResult.load(root/'archive');self.assertTrue(loaded.succeeded);self.assertEqual(len(loaded.directory_artifacts),2);self.assertEqual(loaded.artifact('test:directory.consumer',owner=next(r.owner for r in result.snapshot.resources if r.owner.key=='F')).read_bytes(),b'full output')
 def test_duplicate_or_file_directory_collision_rejects_before_session(self):
  for rows in ((row('A','target/file',kind='file',access='write'),row('B','target/file',kind='file',access='write'),row('D','target',access='write')),(row('A','target/sub',kind='file',access='write'),row('D','target',access='write'),row('B','target/sub/file',kind='file',access='write'))):
   with self.subTest(rows=rows),tempfile.TemporaryDirectory() as tmp:
    root=Path(tmp).resolve();_,model=prepared(root,rows);result,backend=runner_fixture.RunnerDirectoriesTests().run_model(root,model);self.assertFalse(result.succeeded);self.assertFalse(backend.sessions);self.assertFalse((root/'target').exists())
 def test_nested_write_does_not_gain_permission_over_input_members(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp).resolve();target=root/'target';target.mkdir();(target/'input').write_bytes(b'keep');_,model=prepared(root,(row('D','target',access='write'),row('F','target/other',kind='file',access='write'),row('R','target/input',kind='file')));result,backend=runner_fixture.RunnerDirectoriesTests().run_model(root,model);self.assertFalse(result.succeeded);self.assertFalse(backend.sessions);self.assertEqual((target/'input').read_bytes(),b'keep')
 def test_failed_native_work_never_publishes_partial_child_tree(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp).resolve();model=self.model(root)
   def fail(s):s.paths['F'].write_bytes(b'partial');raise OSError('child output failed')
   result,backend=runner_fixture.RunnerDirectoriesTests().run_model(root,model,fail,keep_failed_artifacts=True);self.assertFalse(result.succeeded);self.assertIn('child output failed',result.failure.message);self.assertFalse((root/'target').exists());self.assertTrue(result.directory_artifacts);result.save(root/'archive');self.assertEqual(RunResult.load(root/'archive').failure,result.failure)
 def test_file_only_and_unrelated_output_locations_keep_original_layout(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp).resolve();_,model=prepared(root,(row('F','file',kind='file',access='write'),));plans=inventory(model,input_directory=root,working_directory=root);out=layout(plans,'assets');self.assertEqual(out.relative,{});self.assertEqual(out.targets,((root/'file',False),))

if __name__=='__main__':unittest.main()