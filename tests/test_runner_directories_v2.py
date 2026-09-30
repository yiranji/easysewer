"""Public Runner directory consumers with an explicit synthetic protocol backend."""
from dataclasses import replace
from pathlib import Path
import os,shutil,tempfile,threading,unittest
from unittest.mock import patch
from easysewer.runtime import (Runner,RunResult,DirectoryAdapter,DirectoryLimits,EngineObjects,MassBalance,StepResult)
from easysewer.io.inp import InpDocument
from easysewer.io.interface_inspection import InterfaceInspection
from directory_roundtrip_fixture import prepared,row
from test_runner_v2 import config
from test_checkpoint_container_v2 import snapshot
from test_output_v2 import binary_output

def valid(data,**kwargs):return InterfaceInspection(format=kwargs['use'].format,status='validated')

class DirectoryBackend:
 def __init__(self,model,action=None):self.info=snapshot(Path.cwd(),model=model).backend;self.action=action;self.sessions=[];self.report_names={name:{ref.key for ref in getattr(model.effective_report,name)} for name in ('subcatchments','nodes','links')}
 def probe(self,**kwargs):return self.info
 def session(self,**kwargs):
  session=DirectorySession(self,Path(kwargs['working_directory']));self.sessions.append(session);return session

class DirectorySession:
 def __init__(self,backend,root):self.backend=backend;self.info=backend.info;self.root=root;self.cleanup_errors=();self.flow_units=0;self.closed=False;self.paths={}
 def __enter__(self):return self
 def __exit__(self,*args):self.closed=True
 def open(self,input,report,output,*,expected):
  document=InpDocument.from_bytes(Path(input).read_bytes());self.paths={line.values[0]:self.root/line.values[1] for line in document.records('TEST_DIRECTORY')};self.report_path=Path(report);self.output_path=Path(output)
  self.groups=tuple(expected);self.flow_units=0;self.report_path.write_bytes(b'Synthetic protocol report\n')
  names={name:tuple(v for v in dict(expected)['swmm:'+name] if v in self.backend.report_names[name]) for name in ('subcatchments','nodes','links')};self.output_path.write_bytes(binary_output(names=(names['subcatchments'],names['nodes'],names['links'],())))
  return EngineObjects(groups=self.groups)
 def start(self,**kwargs):pass
 def step(self,**kwargs):
  if self.backend.action:self.backend.action(self)
  return StepResult(elapsed_days=0,finished=True,steps=1)
 def end(self):return MassBalance(runoff_percent=0.,flow_percent=0.,quality_percent=0.)
 def report(self):pass

class RunnerDirectoriesTests(unittest.TestCase):
 def setup(self,root,kind='mixed',readonly=False,absent=False):
  source=root/'source'
  if not absent:
   source.mkdir();(source/'child').mkdir();(source/'child/data').write_bytes(b'initial');(source/'child/alias').hardlink_to(source/'child/data')
  rows=[row('D','source',access='read' if readonly else 'read_write',required=not absent)]
  if kind=='mixed':rows.append(row('F','source/child/data',kind='file',required=not absent))
  elif kind=='alias':
   (root/'file').hardlink_to(source/'child/data');rows.append(row('F','file',kind='file'))
  elif kind=='nested':rows.append(row('R','source/child',required=not absent))
  elif kind=='output':rows.append(row('O','published-directory',access='write'))
  _,model=prepared(root,tuple(rows));return source,model
 def runner(self,model,action=None,**kwargs):
  backend=DirectoryBackend(model,action);runner=Runner(backends={'swmm:standard':backend},inspectors={'test:directory.format':valid},directory_adapters={'test:directory.format':DirectoryAdapter(inspector=valid)},**kwargs);return runner,backend
 def run_model(self,root,model,action=None,**kwargs):
  runner,backend=self.runner(model,action);result=runner.run(model,config(root/'published',**kwargs),relative_to=root);return result,backend
 def published(self,result):
  root=Path(result.input.path).parent;return {r.owner.key:root/r.relative_path for r in result.snapshot.resources if r.owner.collection=='test:directory'}
 def test_public_success_keeps_shared_paths_and_links_after_workspace_cleanup(self):
  for kind in ('mixed','alias','nested','single'):
   with self.subTest(kind=kind),tempfile.TemporaryDirectory() as tmp:
    root=Path(tmp).resolve();source,model=self.setup(root,kind);original=model.to_json_document().to_bytes()
    result,backend=self.run_model(root,model,lambda s:(s.paths['D']/'child/data').write_bytes(b'changed'))
    self.assertTrue(result.succeeded,result.failure);self.assertTrue(backend.sessions[0].closed);self.assertFalse(Path(result.snapshot.execution_directory).exists());p=self.published(result)
    self.assertEqual((p['D']/'child/data').read_bytes(),b'changed');self.assertTrue((p['D']/'child/data').samefile(p['D']/'child/alias'))
    if kind in ('mixed','alias'):self.assertTrue(p['F'].samefile(p['D']/'child/data'))
    if kind=='nested':self.assertEqual(p['R'],p['D']/'child')
    self.assertEqual((source/'child/data').read_bytes(),b'initial');self.assertEqual(model.to_json_document().to_bytes(),original)
    result.save(root/'saved');self.assertTrue(RunResult.load(root/'saved').succeeded)
 def test_readonly_and_optional_absent_inputs_produce_bound_artifacts(self):
  for absent in (False,True):
   with self.subTest(absent=absent),tempfile.TemporaryDirectory() as tmp:
    root=Path(tmp).resolve();_,model=self.setup(root,'single',readonly=True,absent=absent);result,_=self.run_model(root,model)
    self.assertTrue(result.succeeded,result.failure);self.assertEqual(len(result.directory_artifacts),1);artifact=result.directory_artifacts[0];self.assertEqual(artifact.manifest is None,absent);artifact.verify();result.save(root/'saved');self.assertTrue(RunResult.load(root/'saved').succeeded)
 def test_optional_mutable_directory_creation_and_deletion_are_published(self):
  for absent in (False,True):
   with self.subTest(absent=absent),tempfile.TemporaryDirectory() as tmp:
    root=Path(tmp).resolve();source,model=self.setup(root,'single',absent=absent)
    def action(session):
     path=session.paths['D']
     if absent:path.mkdir();(path/'created').write_bytes(b'new')
     else:self.assertTrue(path.resolve().is_relative_to(session.root));shutil.rmtree(path)
    result,_=self.run_model(root,model,action);self.assertTrue(result.succeeded,result.failure);self.assertEqual(len(result.directory_artifacts),2);current=next(a for a in result.directory_artifacts if a.role=='run:resource_state');self.assertEqual(current.manifest is None,not absent);result.save(root/'saved');self.assertTrue(RunResult.load(root/'saved').succeeded);self.assertEqual(source.exists(),not absent)
 def test_new_directory_output_publishes_complete_tree_and_artifact(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp).resolve();_,model=self.setup(root,'output');result,_=self.run_model(root,model,lambda s:(s.paths['O']/'data').write_bytes(b'output'))
   self.assertTrue(result.succeeded,result.failure);self.assertEqual((root/'published-directory/data').read_bytes(),b'output');artifact=next(a for a in result.directory_artifacts if a.owner.key=='O');artifact.verify();self.assertEqual(artifact.file('data').read_bytes(),b'output');result.save(root/'saved')
 def test_existing_directory_output_requires_explicit_overwrite_and_replaces_complete_tree(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp).resolve();source,model=self.setup(root,'output');original=model.to_json_document().to_bytes();target=root/'published-directory';target.mkdir();(target/'keep').write_bytes(b'old');(target/'empty').mkdir()
   result,backend=self.run_model(root,model,overwrite=False)
   self.assertFalse(result.succeeded);self.assertEqual((target/'keep').read_bytes(),b'old');self.assertTrue((target/'empty').is_dir());self.assertFalse(backend.sessions)
   result,backend=self.run_model(root,model,lambda s:(s.paths['O']/'new').write_bytes(b'new output'),overwrite=True)
   self.assertTrue(result.succeeded,result.failure);self.assertEqual({p.name for p in target.iterdir()},{'new'});self.assertEqual((target/'new').read_bytes(),b'new output');self.assertTrue(backend.sessions[0].closed)
   artifact=next(a for a in result.directory_artifacts if a.owner.key=='O');artifact.verify();self.assertEqual(artifact.file('new').read_bytes(),b'new output');result.save(root/'saved');self.assertTrue(RunResult.load(root/'saved').succeeded)
   self.assertEqual(model.to_json_document().to_bytes(),original);self.assertEqual((source/'child/data').read_bytes(),b'initial')
 def test_mutating_file_inspector_rejected_before_session_open(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp).resolve();source,model=self.setup(root);runner,backend=self.runner(model)
   def changing(data,**kwargs):Path(kwargs['source']).write_bytes(b'inspector changed');return valid(data,**kwargs)
   runner.inspectors['test:directory.format']=changing;result=runner.run(model,config(root/'published'),relative_to=root)
   self.assertFalse(result.succeeded);self.assertEqual(result.failure.stage,'open');self.assertFalse(backend.sessions);self.assertEqual((source/'child/data').read_bytes(),b'initial')
 def test_unvalidated_directory_inspector_and_missing_adapter_refuse(self):
  for missing in (False,True):
   with self.subTest(missing=missing),tempfile.TemporaryDirectory() as tmp:
    root=Path(tmp).resolve();_,model=self.setup(root,'single');runner,backend=self.runner(model)
    if missing:runner.directory_adapters.clear()
    else:runner.directory_adapters['test:directory.format']=DirectoryAdapter(inspector=lambda p,**k:InterfaceInspection(format=k['use'].format,status='limited'))
    result=runner.run(model,config(root/'published'),relative_to=root);self.assertEqual(result.status,'rejected');self.assertFalse(backend.sessions)
 def test_failure_retains_directory_artifacts_and_primary_exception(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp).resolve();source,model=self.setup(root);error=OSError('consumer failed')
   def action(session):(session.paths['D']/'child/data').write_bytes(b'partial');raise error
   result,backend=self.run_model(root,model,action,keep_failed_artifacts=True)
   self.assertEqual(result.status,'failed');self.assertEqual(result.failure.message,str(error));self.assertEqual(len(result.directory_group_artifacts),2);self.assertTrue(Path(result.retained_directory).exists());self.assertTrue(all(not a.complete for a in result.directory_group_artifacts));result.save(root/'failed');loaded=RunResult.load(root/'failed');self.assertEqual(loaded.failure,result.failure);self.assertEqual((source/'child/data').read_bytes(),b'initial')
 def test_cancelled_run_cleanup_and_retention_follow_existing_policy(self):
  for keep in (False,True):
   with self.subTest(keep=keep),tempfile.TemporaryDirectory() as tmp:
    root=Path(tmp).resolve();_,model=self.setup(root);event=threading.Event();runner,backend=self.runner(model,lambda s:event.set());result=runner.run(model,config(root/'published',keep_failed_artifacts=keep),relative_to=root,cancel_event=event)
    self.assertEqual(result.status,'cancelled');self.assertTrue(backend.sessions[0].closed);self.assertEqual(bool(result.directory_group_artifacts),keep);self.assertEqual(Path(result.snapshot.execution_directory).exists(),keep);self.assertFalse((root/'published/model.out').exists())
 def test_output_failure_rolls_back_main_files_and_preserves_failed_group(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp).resolve();_,model=self.setup(root);out=root/'published';out.mkdir();(out/'model.out').write_bytes(b'old');original=os.replace
   def fail(source,target):
    if Path(target)==out/'model.out' and Path(source).name.startswith('.easysewer-publish-'):raise OSError('publish failure')
    return original(source,target)
   with patch('easysewer.runtime._workspace.os.replace',side_effect=fail):result,_=self.run_model(root,model,overwrite=True,keep_failed_artifacts=True)
   self.assertFalse(result.succeeded);self.assertIn('publish failure',result.failure.message);self.assertEqual(result.failure.stage,'publication');self.assertEqual((out/'model.out').read_bytes(),b'old');self.assertEqual(len(result.directory_group_artifacts),2);self.assertFalse(list(out.glob('easysewer-assets-*')))
 def test_initial_current_cross_alias_is_rejected_before_commit(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp).resolve();_,model=self.setup(root,'single')
   def action(session):
    current=session.paths['D']/'child/data';initial=Path(str(current).replace(os.sep+'inputs'+os.sep,os.sep+'initial'+os.sep));current.unlink();current.hardlink_to(initial)
   result,_=self.run_model(root,model,action);self.assertFalse(result.succeeded);self.assertIn('Initial resource evidence aliases',result.failure.message);self.assertFalse((root/'published/model.out').exists())
 def test_readonly_absence_cannot_silently_become_a_new_directory(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp).resolve();source,model=self.setup(root,'single',readonly=True,absent=True);result,_=self.run_model(root,model,lambda s:s.paths['D'].mkdir());self.assertFalse(result.succeeded);self.assertIn('expected absence',result.failure.message);self.assertFalse(source.exists())
 def test_public_budget_validation_and_capture_limit_precede_native_execution(self):
  for bad in (1,{},True):
   with self.assertRaises(TypeError):Runner(directory_limits=bad)
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp).resolve();_,model=self.setup(root,'single');runner,backend=self.runner(model,directory_limits=DirectoryLimits(total_bytes=1));result=runner.run(model,config(root/'published'),relative_to=root);self.assertFalse(result.succeeded);self.assertFalse(backend.sessions)
 def test_output_inside_optional_missing_input_directory_is_protected(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp).resolve();source=root/'missing';_,model=prepared(root,(row('D','missing',required=False),row('O','missing/new',access='write')));runner,backend=self.runner(model);result=runner.run(model,config(root/'published'),relative_to=root);self.assertFalse(result.succeeded);self.assertFalse(source.exists());self.assertFalse(backend.sessions)

 def test_inspector_model_changes_refuse_before_starting_session(self):
  from datetime import timedelta
  for directory in (False,True):
   with self.subTest(directory=directory),tempfile.TemporaryDirectory() as tmp:
    root=Path(tmp).resolve();_,model=self.setup(root);original=model.to_json_document().to_bytes();runner,backend=self.runner(model)
    def changing(data,**kwargs):kwargs['model'].update_options(rule_step=timedelta(seconds=7));return valid(data,**kwargs)
    if directory:runner.directory_adapters['test:directory.format']=DirectoryAdapter(inspector=changing)
    else:runner.inspectors['test:directory.format']=changing
    result=runner.run(model,config(root/'published'),relative_to=root);self.assertEqual(result.status,'rejected');self.assertIn('run.inspector_model_changed',result.failure.message);self.assertFalse(backend.sessions);self.assertEqual(model.to_json_document().to_bytes(),original);self.assertIsNotNone(result.snapshot);self.assertEqual(len(result.directory_group_artifacts),2)

 def test_finalizing_callback_cannot_change_completed_resource_evidence(self):
  for kind in ('single','mixed','alias','nested','output'):
   for keep in (False,True):
    with self.subTest(kind=kind,keep=keep),tempfile.TemporaryDirectory() as tmp:
     root=Path(tmp).resolve();source,model=self.setup(root,kind);runner,backend=self.runner(model);out=root/'published';out.mkdir();(out/'model.out').write_bytes(b'old')
     def late(progress):
      if progress.phase=='finalizing':
       session=backend.sessions[0];self.assertTrue(session.closed);target=session.paths['O']/'data' if kind=='output' else session.paths['D']/'child/data';target.write_bytes(b'late callback change')
     result=runner.run(model,config(out,overwrite=True,keep_failed_artifacts=keep),relative_to=root,progress=late)
     self.assertEqual(result.status,'failed',result.failure);self.assertEqual(result.failure.stage,'publication');self.assertIn('changed before publication',result.failure.message);self.assertTrue(result.native_completed);self.assertEqual((out/'model.out').read_bytes(),b'old');self.assertFalse((root/'published-directory').exists());self.assertEqual((source/'child/data').read_bytes(),b'initial');self.assertEqual(bool(result.directory_artifacts or result.directory_group_artifacts),keep);self.assertTrue(all(not a.complete for a in (*result.directory_artifacts,*result.directory_group_artifacts)));self.assertEqual(Path(result.snapshot.execution_directory).exists(),keep);result.save(root/'failed');self.assertEqual(RunResult.load(root/'failed').failure,result.failure)

 def test_finalizing_callback_cannot_change_tree_membership_aliases_or_absence(self):
  for change in ('add','delete','break-alias','remove-tree','create-absent'):
   with self.subTest(change=change),tempfile.TemporaryDirectory() as tmp:
    root=Path(tmp).resolve();source,model=self.setup(root,'single',absent=change=='create-absent');runner,backend=self.runner(model)
    def late(progress):
     if progress.phase!='finalizing':return
     path=backend.sessions[0].paths['D']
     if change=='add':(path/'new').write_bytes(b'new')
     elif change=='delete':(path/'child/alias').unlink()
     elif change=='break-alias':alias=path/'child/alias';content=alias.read_bytes();alias.unlink();alias.write_bytes(content)
     elif change=='remove-tree':self.assertTrue(path.resolve().is_relative_to(backend.sessions[0].root.resolve()));shutil.rmtree(path)
     else:path.mkdir()
    result=runner.run(model,config(root/'published'),relative_to=root,progress=late);self.assertEqual(result.status,'failed',result.failure);self.assertIn('changed before publication',result.failure.message);self.assertFalse((root/'published/model.out').exists());self.assertEqual(source.exists(),change!='create-absent');result.save(root/'failed')

 def test_report_extension_cannot_change_completed_directory_before_finalizing(self):
  from easysewer.io.report_document import ReportDocument
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp).resolve();_,model=self.setup(root);runner,backend=self.runner(model);read=ReportDocument.read;calls=[]
   def changed(*args,**kwargs):
    document=read(*args,**kwargs);session=backend.sessions[0];self.assertTrue(session.closed);(session.paths['D']/'child/data').write_bytes(b'changed while reading report');calls.append(True);return document
   with patch.object(ReportDocument,'read',side_effect=changed):result=runner.run(model,config(root/'published'),relative_to=root)
   self.assertTrue(calls);self.assertEqual(result.status,'failed',result.failure);self.assertIn('changed before publication',result.failure.message);self.assertFalse((root/'published/model.out').exists())

 def test_cancellation_during_final_resource_capture_retains_incomplete_artifacts(self):
  from easysewer.runtime._runner_directories import collect
  for keep in (False,True):
   with self.subTest(keep=keep),tempfile.TemporaryDirectory() as tmp:
    root=Path(tmp).resolve();_,model=self.setup(root);runner,backend=self.runner(model);event=threading.Event()
    def interrupted(*args,**kwargs):
     if kwargs['complete']:event.set();kwargs['checkpoint']()
     return collect(*args,**kwargs)
    with patch('easysewer.runtime._runner_directories.collect',side_effect=interrupted):result=runner.run(model,config(root/'published',keep_failed_artifacts=keep),relative_to=root,cancel_event=event)
    self.assertEqual(result.status,'cancelled');self.assertEqual(result.failure.stage,'verify');self.assertTrue(result.native_completed);self.assertTrue(backend.sessions[0].closed);self.assertEqual(bool(result.directory_group_artifacts),keep);self.assertTrue(all(not a.complete for a in result.directory_group_artifacts));self.assertFalse((root/'published/model.out').exists());self.assertEqual(Path(result.snapshot.execution_directory).exists(),keep)

if __name__=='__main__':unittest.main()
