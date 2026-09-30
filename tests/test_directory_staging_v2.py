"""Explicit directory capture before public Runner publication integration."""
from dataclasses import replace
import os,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from easysewer.model import Ref
from easysewer.runtime import check_files
from easysewer.runtime import _preparation as prep
from easysewer.runtime.directory_resources import DirectoryAdapter,DirectoryLimits
from easysewer.runtime._directory_tree import DirectoryManifest,DirectoryEntry,inspect_tree
from easysewer.io.interface_inspection import InterfaceInspection
from easysewer.validation import ValidationError
from directory_roundtrip_fixture import prepared,row
from test_directory_reconstruction_v2 import tree

class DirectoryStagingTests(unittest.TestCase):
 def registry(self,**limits):return {'test:directory.format':DirectoryAdapter(inspector=lambda p,**k:InterfaceInspection(format=k['use'].format,status='validated'),limits=DirectoryLimits(**limits))}
 def setup_capture(self,root,rows,registry=None,name='work'):
  _,model=prepared(root,rows);before=model.to_json_document().to_bytes();captured=model.copy();registry=self.registry() if registry is None else registry
  plans=prep.inventory(captured,input_directory=root,working_directory=root/'published',directory_adapters=registry);workspace=root/name;workspace.mkdir();return model,before,captured,plans,workspace
 def test_private_tree_exact_capture_preflight_rebase_and_original_model_unchanged(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);source=tree(root);expected=inspect_tree(source);model,before,captured,plans,workspace=self.setup_capture(root,(row('R','inputs/树目录'),));records,outputs,issues=prep.stage(captured,plans,workspace,'assets',checkpoint=lambda:None)
   self.assertFalse(outputs or issues);self.assertEqual(len(records),1);record=records[0];self.assertEqual(record.tree,expected);self.assertIsNone(record.sha256);self.assertEqual(record.relative_path,'assets/inputs/r0.dir')
   self.assertEqual(captured.collection('test:directory')['R'].file.path,record.relative_path);self.assertEqual(inspect_tree(workspace/record.relative_path),expected);self.assertEqual(inspect_tree(source),expected);self.assertEqual(model.to_json_document().to_bytes(),before)
   paths=[]
   def inspector(path,**kwargs):paths.append(path);self.assertEqual(kwargs['manifest'],expected);return InterfaceInspection(format=kwargs['use'].format,status='validated')
   checked=check_files(captured,input_directory=workspace,working_directory=workspace,directory_adapters={'test:directory.format':DirectoryAdapter(inspector=inspector)})
   self.assertTrue(checked.complete,checked.report);self.assertEqual(paths,[workspace/record.relative_path]);prep.verify_resources(records,workspace,checkpoint=lambda:None)
 def test_shared_consumers_capture_once_and_each_consumer_budget_applies(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);tree(root);model,before,captured,plans,workspace=self.setup_capture(root,(row('R','inputs/树目录'),row('S','inputs/树目录')))
   registry=self.registry();plans=prep.inventory(captured,input_directory=root,working_directory=root,directory_adapters=registry);registry.clear()
   with patch.object(prep,'capture_tree',wraps=prep.capture_tree) as copy:records,_,_=prep.stage(captured,plans,workspace,'assets',checkpoint=lambda:None)
   self.assertEqual(copy.call_count,1);self.assertEqual(records[0].relative_path,records[1].relative_path);self.assertEqual(records[0].tree,records[1].tree)
   captured=model.copy();plans=prep.inventory(captured,input_directory=root,working_directory=root,directory_adapters=self.registry());plans=(plans[0],replace(plans[1],directory_adapter=self.registry(entries=1)['test:directory.format']));work2=root/'smaller';work2.mkdir()
   with self.assertRaises(ValueError):prep.stage(captured,plans,work2,'assets',checkpoint=lambda:None)
   self.assertEqual(model.to_json_document().to_bytes(),before)
 def test_output_optional_and_inactive_directories_preserve_original_destinations(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);original=root/'old-output';original.mkdir();(original/'keep').write_bytes(b'old')
   rows=(row('Out','old-output',access='write'),row('Missing','missing',required=False),row('Inactive','inactive',active=False))
   model,before,captured,plans,workspace=self.setup_capture(root,rows);records,outputs,issues=prep.stage(captured,plans,workspace,'assets',checkpoint=lambda:None)
   self.assertEqual(len(outputs),1);self.assertEqual(outputs[0][1],original);self.assertTrue((workspace/records[0].relative_path).is_dir());self.assertEqual(list((workspace/records[0].relative_path).iterdir()),[])
   for r in records[1:]:self.assertFalse((workspace/r.relative_path).exists());self.assertIsNone(r.tree)
   self.assertEqual([v.code for v in issues],['run.optional_resource_missing']);self.assertEqual((original/'keep').read_bytes(),b'old');self.assertFalse((root/'missing').exists());self.assertFalse((root/'inactive').exists());self.assertEqual(model.to_json_document().to_bytes(),before)
   _,inactive=prepared(root,(row('Only','not-read',active=False),));plans=prep.inventory(inactive,input_directory=root,working_directory=root);work=root/'inactive-work';work.mkdir()
   with patch.object(prep,'capture_tree',side_effect=AssertionError('Inactive capture')):prep.stage(inactive,plans,work,'assets',checkpoint=lambda:None)
 def test_read_write_is_private_and_modified_capture_is_not_stable_initial_evidence(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);source=tree(root);expected=inspect_tree(source);_,_,captured,plans,workspace=self.setup_capture(root,(row('RW','inputs/树目录',access='read_write'),));records,_,_=prep.stage(captured,plans,workspace,'assets',checkpoint=lambda:None)
   private=workspace/records[0].relative_path;(private/'nested/data').write_bytes(b'changed\x00\xff');self.assertEqual(inspect_tree(source),expected)
   prep.verify_resources(records,workspace,checkpoint=lambda:None)
   self.assertEqual(inspect_tree(workspace/records[0].initial_relative_path),expected)
   with self.assertRaises(ValueError):prep.verify_resources(records,workspace,checkpoint=lambda:None,initial_execution=True)
 def test_member_changes_empty_directory_changes_and_all_consumer_diagnostics(self):
  for change in ('content','empty-add','empty-remove','file-remove'):
   with self.subTest(change=change),tempfile.TemporaryDirectory() as tmp:
    root=Path(tmp);source=tree(root);model,before,captured,plans,workspace=self.setup_capture(root,(row('R','inputs/树目录'),row('S','inputs/树目录')));records,_,_=prep.stage(captured,plans,workspace,'assets',checkpoint=lambda:None);private=workspace/records[0].relative_path
    if change=='content':(private/'nested/data').write_bytes(b'CONTENT\x00\xff')
    elif change=='empty-add':(private/'extra').mkdir()
    elif change=='empty-remove':(private/'empty').rmdir()
    else:(private/'zero').unlink()
    with self.assertRaises(ValueError) as caught:prep.verify_resources(records,workspace,checkpoint=lambda:None)
    error=caught.exception;report=error.report if isinstance(error,ValidationError) else error._easysewer_resource_diagnostics
    diagnostic=report.diagnostics[0];self.assertEqual({diagnostic.subject.key,*(r.key for r in diagnostic.related)},{'R','S'});self.assertEqual(inspect_tree(source),records[0].tree);self.assertEqual(model.to_json_document().to_bytes(),before)
 def test_required_missing_wrong_kind_and_unknown_adapter_reject(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);(root/'file').write_bytes(b'regular')
   for name in ('missing','file'):
    _,_,captured,plans,workspace=self.setup_capture(root,(row('R',name),),name='work-'+name)
    with self.assertRaises(ValueError):prep.stage(captured,plans,workspace,'assets',checkpoint=lambda:None)
   _,model=prepared(root,(row('R','missing'),))
   with self.assertRaisesRegex(ValueError,'runtime staging adapter'):prep.inventory(model,input_directory=root,working_directory=root)
   with self.assertRaises(TypeError):prep.inventory(model,input_directory=root,working_directory=root,directory_adapters={'test:directory.format':lambda:None})
 def test_capture_byte_entry_and_depth_limits_never_truncate_to_success(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);source=tree(root);expected=inspect_tree(source)
   for index,limits in enumerate(({'total_bytes':1},{'entries':1},{'depth':1},{})):
    _,_,captured,plans,workspace=self.setup_capture(root,(row('R','inputs/树目录'),),registry=self.registry(**limits),name='limited-'+str(index))
    with self.assertRaises(ValueError):prep.stage(captured,plans,workspace,'assets',checkpoint=lambda:None,max_bytes=1 if not limits else None)
    self.assertEqual(inspect_tree(source),expected)
   _,_,captured,plans,workspace=self.setup_capture(root,(row('R','inputs/树目录'),),name='retry');records,_,_=prep.stage(captured,plans,workspace,'assets',checkpoint=lambda:None);self.assertEqual(records[0].tree,expected)
 def test_capture_cancellation_preserves_original_exception_model_and_source(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);source=tree(root);expected=inspect_tree(source);model,before,captured,plans,workspace=self.setup_capture(root,(row('R','inputs/树目录'),));error=OSError('stop capture');error.__cause__=ValueError('cause');calls=[]
   def stop():
    if (workspace/'assets/inputs/r0.dir').exists():calls.append(True);raise error
   with self.assertRaises(OSError) as caught:prep.stage(captured,plans,workspace,'assets',checkpoint=stop)
   self.assertIs(caught.exception,error);self.assertIsInstance(caught.exception.__cause__,ValueError);self.assertEqual(calls,[True]);self.assertEqual(inspect_tree(source),expected);self.assertEqual(model.to_json_document().to_bytes(),before)
   _,_,captured,plans,work2=self.setup_capture(root,(row('R','inputs/树目录'),),name='retry');prep.stage(captured,plans,work2,'assets',checkpoint=lambda:None)
 def test_conflicting_shared_records_and_checkpoint_plans_cannot_be_staged(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);tree(root);_,_,captured,plans,workspace=self.setup_capture(root,(row('R','inputs/树目录'),row('S','inputs/树目录')));records,_,_=prep.stage(captured,plans,workspace,'assets',checkpoint=lambda:None)
   wrong=replace(records[1],tree=DirectoryManifest(entries=()))
   with self.assertRaisesRegex(ValueError,'disagree'):prep.verify_resources((records[0],wrong),workspace,checkpoint=lambda:None)
   value,model=prepared(root,(row('R','inputs/树目录'),));claims=prep.inventory(model,input_directory=root,working_directory=root,_captured_directories=value.resources);target=root/'unadapted';target.mkdir()
   with self.assertRaisesRegex(ValueError,'runtime staging adapter'):prep.stage(model,claims,target,'assets',checkpoint=lambda:None)
 def test_workspace_overlap_is_rejected_before_creating_asset_subtrees(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);source=tree(root);work=source/'work';work.mkdir();_,model=prepared(root,(row('R','inputs/树目录'),));plans=prep.inventory(model,input_directory=root,working_directory=root,directory_adapters=self.registry());before=inspect_tree(source)
   with self.assertRaisesRegex(ValueError,'workspace overlaps'):prep.stage(model,plans,work,'assets',checkpoint=lambda:None)
   self.assertFalse((work/'assets').exists());self.assertEqual(inspect_tree(source),before)
 def test_symlink_root_is_not_hidden_by_shared_source_resolution(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);source=tree(root);link=root/'alias'
   try:link.symlink_to(source,target_is_directory=True)
   except OSError as error:self.skipTest('Directory symlink privilege unavailable: '+str(error))
   # Do not inspect the link in fixture preparation; it is precisely the rejected input.
   _,model=prepared(root,(row('R','inputs/树目录'),));model.collection('test:directory').add(row('Alias','alias'));plans=prep.inventory(model,input_directory=root,working_directory=root,directory_adapters=self.registry());work=root/'work';work.mkdir()
   with self.assertRaises(ValueError):prep.stage(model,plans,work,'assets',checkpoint=lambda:None)
   self.assertTrue(link.is_symlink());self.assertEqual((source/'nested/data').read_bytes(),b'content\x00\xff')

if __name__=='__main__':unittest.main()
