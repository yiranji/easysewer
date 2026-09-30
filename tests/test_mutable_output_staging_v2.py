from pathlib import Path
import os,tempfile,unittest
from dataclasses import replace
from unittest.mock import patch
from easysewer.runtime import RunResult
from easysewer.runtime._preparation import inventory,stage,verify_resources
from easysewer.runtime._directory_outputs import layout
from easysewer.runtime._directory_tree import inspect_tree
from directory_roundtrip_fixture import prepared,row
import test_runner_directories_v2 as runner_fixture

class MutableOutputStagingTests(unittest.TestCase):
 def setup(self,root,*,nested=False,old=False,alias=False,absent=False,reverse=False):
  source=root/'source'
  if not absent:
   (source/'sub').mkdir(parents=True);(source/'state').write_bytes(b'initial')
   if old:
    (source/'sub/report').write_bytes(b'old report')
    if alias:(source/'sub/alias').hardlink_to(source/'sub/report')
  rows=[row('F','source/sub/report',kind='file',access='write'),row('D','source',access='read_write',required=not absent)]
  if nested:rows += [row('N','source/sub',access='read_write',required=not absent),row('R','source',required=not absent)]
  if reverse:rows.reverse()
  _,model=prepared(root,tuple(rows));return source,model
 def test_staging_owns_private_child_and_preserves_original_initial_evidence(self):
  for nested,reverse,old in ((False,False,False),(False,True,True),(True,False,True),(True,True,False)):
   with self.subTest(nested=nested,reverse=reverse,old=old),tempfile.TemporaryDirectory() as tmp:
    root=Path(tmp).resolve();source,model=self.setup(root,nested=nested,reverse=reverse,old=old);initial=inspect_tree(source);helper=runner_fixture.RunnerDirectoriesTests();runner,_=helper.runner(model);plans=inventory(model,input_directory=root,working_directory=root,directory_adapters=runner.directory_adapters);out=layout(plans,'assets');self.assertEqual(out.targets,());work=root/'work';work.mkdir();records,outputs,_=stage(model,plans,work,'assets',checkpoint=lambda:None);m={r.owner.key:r for r in records if r.owner.collection=='test:directory'}
    self.assertEqual(Path(m['F'].relative_path),Path(m['D'].relative_path)/'sub/report');self.assertFalse((work/m['F'].relative_path).exists());self.assertEqual(inspect_tree(work/m['D'].initial_relative_path),initial);self.assertEqual(inspect_tree(source),initial);self.assertIsNone(outputs[0][1]);verify_resources(records,work,checkpoint=lambda:None,initial_execution=True)
 def test_public_success_archive_keeps_current_and_initial_bytes_separate(self):
  for nested,old,alias in ((False,False,False),(False,True,False),(False,True,True),(True,True,True)):
   with self.subTest(nested=nested,old=old,alias=alias),tempfile.TemporaryDirectory() as tmp:
    root=Path(tmp).resolve();source,model=self.setup(root,nested=nested,old=old,alias=alias);initial=inspect_tree(source);original=model.to_json_document().to_bytes();helper=runner_fixture.RunnerDirectoriesTests()
    def action(s):
     self.assertEqual(s.paths['F'],s.paths['D']/'sub/report');self.assertFalse(s.paths['F'].exists());s.paths['F'].write_bytes(b'new output')
     if alias:self.assertEqual((s.paths['D']/'sub/alias').read_bytes(),b'old report');self.assertFalse(s.paths['F'].samefile(s.paths['D']/'sub/alias'))
    result,_=helper.run_model(root,model,action);self.assertTrue(result.succeeded,result.failure);self.assertEqual(inspect_tree(source),initial);self.assertEqual(model.to_json_document().to_bytes(),original);paths=helper.published(result);self.assertEqual(paths['F'].read_bytes(),b'new output');result.save(root/'archive');loaded=RunResult.load(root/'archive');self.assertEqual(loaded.output.read_bytes(),result.output.read_bytes());self.assertEqual(loaded.artifact('test:directory.consumer',owner=next(r.owner for r in result.snapshot.resources if r.owner.key=='F')).read_bytes(),b'new output')
 def test_missing_optional_mutable_tree_materializes_only_current_parents(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp).resolve();source,model=self.setup(root,absent=True);result,_=runner_fixture.RunnerDirectoriesTests().run_model(root,model,lambda s:s.paths['F'].write_bytes(b'output'));self.assertTrue(result.succeeded,result.failure);self.assertFalse(source.exists());result.save(root/'archive');self.assertTrue(RunResult.load(root/'archive').succeeded)
 def test_output_directory_children_keep_one_mutable_owner(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp).resolve();source,model=self.setup(root);model.collection('test:directory').add(row('O','source/new',access='write'));model.collection('test:directory').add(row('G','source/new/deep/result',kind='file',access='write'))
   def action(s):s.paths['F'].write_bytes(b'first');s.paths['G'].write_bytes(b'second')
   result,_=runner_fixture.RunnerDirectoriesTests().run_model(root,model,action);self.assertTrue(result.succeeded,result.failure);self.assertFalse((source/'new').exists());result.save(root/'archive');self.assertTrue(RunResult.load(root/'archive').succeeded)
 def test_readonly_inactive_equal_and_file_input_aliases_reject_before_session(self):
  for kind in ('readonly','inactive','equal','file','hardlink','case'):
   with self.subTest(kind=kind),tempfile.TemporaryDirectory() as tmp:
    root=Path(tmp).resolve();source,model=self.setup(root,old=True);rows=model.collection('test:directory')
    if kind in ('readonly','inactive'):rows.replace('D',replace(rows['D'],access='read' if kind=='readonly' else 'read_write',active=kind!='inactive'))
    elif kind=='equal':rows.replace('F',row('F','source',kind='file',access='write'))
    elif kind=='file':rows.add(row('I','source/sub/report',kind='file'))
    elif kind=='hardlink':(source/'input').hardlink_to(source/'sub/report');rows.add(row('I','source/input',kind='file'))
    else:rows.add(row('G','source/sub/REPORT',kind='file',access='write'))
    initial=inspect_tree(source);result,backend=runner_fixture.RunnerDirectoriesTests().run_model(root,model);self.assertFalse(result.succeeded);self.assertFalse(backend.sessions);self.assertEqual(inspect_tree(source),initial)
 def test_failure_and_publish_rollback_preserve_source_and_prior_output(self):
  for publication in (False,True):
   with self.subTest(publication=publication),tempfile.TemporaryDirectory() as tmp:
    root=Path(tmp).resolve();source,model=self.setup(root,old=True);initial=inspect_tree(source);out=root/'published';out.mkdir();(out/'model.out').write_bytes(b'keep');original=os.replace
    def action(s):
     s.paths['F'].write_bytes(b'partial')
     if not publication:raise OSError('native child failure')
    def failing(a,b):
     if publication and Path(b)==out/'model.out' and Path(a).name.startswith('.easysewer-publish-'):raise OSError('publication failure')
     return original(a,b)
    with patch('easysewer.runtime._workspace.os.replace',side_effect=failing):result,_=runner_fixture.RunnerDirectoriesTests().run_model(root,model,action,overwrite=True,keep_failed_artifacts=True)
    self.assertFalse(result.succeeded);self.assertEqual(inspect_tree(source),initial);self.assertEqual((out/'model.out').read_bytes(),b'keep');result.save(root/'archive');self.assertEqual(RunResult.load(root/'archive').failure,result.failure)

if __name__=='__main__':unittest.main()
