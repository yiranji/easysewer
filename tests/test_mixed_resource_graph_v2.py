"""Mixed file/directory relationships through staging and portable recovery."""
from dataclasses import replace
from pathlib import Path
import json,subprocess,sys,tempfile,unittest
from unittest.mock import patch
from easysewer.runtime import _preparation as prep, _checkpoint_container as storage
from easysewer.runtime import _directory_graph as graph
from easysewer.runtime._directory_tree import DirectoryLimits,inspect_tree
from easysewer.runtime.directory_resources import DirectoryAdapter
from easysewer.runtime.results import RunResult,FileArtifact
from easysewer.runtime._result_codec import Codec
from easysewer.runtime._checkpoint_context import write,read
from easysewer.io.interface_inspection import InterfaceInspection
from directory_history_fixture import historical_package
from directory_roundtrip_fixture import prepared,row,schema
from test_checkpoint_container_v2 import snapshot
from test_directory_checkpoint_v2 import record
import test_directory_group_integration_v2 as groups

class MixedResourceGraphTests(unittest.TestCase):
 def setup(self,root,kind='nested',readonly=False,file_writer=False):
  source=root/'source';source.mkdir();(source/'child').mkdir();(source/'child/data').write_bytes(b'initial');external=root/'file'
  if kind=='alias':external.hardlink_to(source/'child/data')
  elif kind=='independent':external.write_bytes(b'other')
  path='file' if kind in ('alias','independent','absent-root') else 'source/new/data' if kind=='absent-parent' else 'source/child/missing' if kind=='absent' else 'source/child/data'
  absent=kind.startswith('absent')
  rows=(row('W','source',access='read' if readonly or file_writer else 'read_write'),row('F',path,kind='file',access='read_write' if file_writer else 'read',required=not absent))
  _,model=prepared(root,rows);work=root/'work';work.mkdir();adapters={'test:directory.format':DirectoryAdapter(inspector=lambda p,**k:InterfaceInspection(format=k['use'].format,status='validated'))}
  plans=prep.inventory(model,input_directory=root,working_directory=root,directory_adapters=adapters);resources,outputs,issues=prep.stage(model,plans,work,'assets',checkpoint=lambda:None)
  value=snapshot(work,model=model,resources=resources);group,=graph.resource_groups(resources).values();self.assertFalse(outputs)
  return source,external,work,value,group
 def helper(self):return groups.DirectoryGroupIntegrationTests()
 def paths(self,work,value):return {r.owner.key:work/r.relative_path for r in value.resources}
 def test_nested_file_view_preserves_modify_replace_delete_and_source_isolation(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp).resolve();source,_,work,value,group=self.setup(root);p=self.paths(work,value)
   self.assertEqual(p['F'],p['W']/'child/data');self.assertTrue(p['F'].samefile(p['W']/'child/data'));self.assertFalse(p['F'].samefile(source/'child/data'))
   p['F'].write_bytes(b'changed');self.assertEqual((p['W']/'child/data').read_bytes(),b'changed');p['F'].unlink();(p['W']/'child/data').write_bytes(b'replaced');self.assertEqual(p['F'].read_bytes(),b'replaced')
   prep.verify_resources(value.resources,work,checkpoint=lambda:None)
   with self.assertRaises(ValueError):prep.verify_resources(value.resources,work,checkpoint=lambda:None,initial_execution=True)
   self.assertEqual((source/'child/data').read_bytes(),b'initial')
 def test_external_file_alias_uses_private_link_and_replacement_breaks_only_that_alias(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp).resolve();source,external,work,value,group=self.setup(root,'alias');p=self.paths(work,value)
   self.assertTrue(p['F'].samefile(p['W']/'child/data'));p['F'].write_bytes(b'changed');self.assertEqual((p['W']/'child/data').read_bytes(),b'changed');p['F'].unlink();p['F'].write_bytes(b'new')
   self.assertFalse(p['F'].samefile(p['W']/'child/data'));self.assertEqual((source/'child/data').read_bytes(),b'initial');self.assertEqual(external.read_bytes(),b'initial')
 def test_all_mixed_checkpoint_views_restore_after_source_and_work_deletion(self):
  for kind in ('nested','alias','independent','absent','absent-parent','absent-root'):
   with self.subTest(kind=kind),tempfile.TemporaryDirectory() as tmp:
    root=Path(tmp).resolve();source,external,work,value,group=self.setup(root,kind);p=self.paths(work,value);h=self.helper();before=h.save(root/'before',value)
    if kind=='independent':p['F'].unlink();p['F'].hardlink_to(p['W']/'child/data')
    elif kind.startswith('absent'):p['F'].parent.mkdir(parents=True,exist_ok=True);p['F'].write_bytes(b'created')
    else:p['F'].write_bytes(b'changed')
    saved=h.save(root/'saved',value);self.assertEqual(saved.binding,before.binding);self.assertEqual(saved.data['schema_version'],'1.6')
    h.remove(work,root);h.remove(source,root)
    if external.exists():external.unlink()
    (root/'saved').rename(root/'moved')
    with patch('ctypes.CDLL',side_effect=AssertionError('offline')):restored=storage.load(root/'moved').materialize(root/'restored',schema=schema())
    q=self.paths(root/'restored',restored);self.assertEqual(q['F'].read_bytes(),b'created' if kind.startswith('absent') else b'initial' if kind=='independent' else b'changed')
    if kind in ('nested','alias','independent'):self.assertTrue(q['F'].samefile(q['W']/'child/data'))
    self.assertEqual(inspect_tree(root/'restored'/group.initial_relative_path),group.tree);self.assertEqual(h.save(root/'again',restored).binding,saved.binding)
 def test_missing_and_deleted_file_views_do_not_create_missing_parents(self):
  for kind in ('nested','alias','absent','absent-parent','absent-root'):
   with self.subTest(kind=kind),tempfile.TemporaryDirectory() as tmp:
    root=Path(tmp).resolve();_,_,work,value,group=self.setup(root,kind);p=self.paths(work,value)
    if p['F'].exists():p['F'].unlink()
    saved=self.helper().save(root/'saved',value);restored=saved.materialize(root/'restored',schema=schema());q=self.paths(root/'restored',restored);self.assertFalse(q['F'].exists())
    if kind=='absent-parent':self.assertFalse(q['F'].parent.exists())
 def test_file_writer_makes_readonly_directory_share_independent_initial_evidence(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp).resolve();source,_,work,value,group=self.setup(root,file_writer=True);p=self.paths(work,value)
   self.assertIsNotNone(group.initial_relative_path);p['F'].write_bytes(b'changed');self.assertEqual((p['W']/'child/data').read_bytes(),b'changed');prep.verify_resources(value.resources,work,checkpoint=lambda:None);self.assertEqual((source/'child/data').read_bytes(),b'initial')
 def test_readonly_mixed_graph_uses_one_forest_and_archive_restores_file_view(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp).resolve();_,_,work,value,group=self.setup(root,'alias',readonly=True);h=self.helper();self.assertIsNone(group.initial_relative_path);h.failed(work,value,group).save(root/'archive');result=RunResult.load(root/'archive');self.assertEqual(len(result.directory_group_artifacts),1)
   item=result.directory_group_artifacts[0].materialize(root/'artifact');r={r.owner.key:r for r in value.resources};self.assertTrue(item.view_path(r['F'].owner,r['F'].field).samefile(item.view_path(r['W'].owner,r['W'].field)/'child/data'))
 def test_current_alias_creation_and_breakage_survive_result_archives(self):
  for kind in ('alias','independent'):
   with self.subTest(kind=kind),tempfile.TemporaryDirectory() as tmp:
    root=Path(tmp).resolve();_,_,work,value,group=self.setup(root,kind);p=self.paths(work,value);p['F'].unlink()
    if kind=='alias':p['F'].write_bytes(b'initial')
    else:p['F'].hardlink_to(p['W']/'child/data')
    self.helper().failed(work,value,group).save(root/'archive');result=RunResult.load(root/'archive');item=next(a for a in result.directory_group_artifacts if a.current).materialize(root/'artifact');r={r.owner.key:r for r in value.resources};self.assertEqual(item.view_path(r['F'].owner,r['F'].field).samefile(item.view_path(r['W'].owner,r['W'].field)/'child/data'),kind=='independent')
 def test_mixed_resource_hash_kind_and_consumer_binding_cannot_be_forged(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp).resolve();_,_,work,value,group=self.setup(root);resource=next(r for r in value.resources if r.kind=='file')
   for change in ({'sha256':'0'*64},{'size':1},{'kind':'directory'},{'initial_relative_path':None},{'relative_path':'assets/unbound'},{'owner':replace(resource.owner,key='other')}):
    with self.subTest(change=change),self.assertRaises((ValueError,TypeError)):replace(resource,**change)
   encoded=Codec(object(),result_version='1.9').encode(resource);self.assertEqual(Codec(object(),result_version='1.9').decode(encoded),resource)
   with self.assertRaises(ValueError):Codec(object(),result_version='1.8').encode(resource)
   with self.assertRaises(ValueError):Codec(object(),result_version='1.8').decode(encoded)
 def test_mixed_wire_versions(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp).resolve();_,_,work,value,group=self.setup(root);h=self.helper();digest,_=write(root/'context',value);self.assertEqual(read(root/'context',digest),value);self.assertEqual(json.loads((root/'context/context.json').read_bytes())['version'],7)
   self.assertEqual(json.loads(record(root/'record',value))['schema_version'],'1.7');h.failed(work,value,group).save(root/'archive');self.assertEqual(json.loads((root/'archive/result.json').read_bytes())['schema_version'],'1.9');h.save(root/'saved',value)

 def test_historical_candidate_v22_readers_reject_mixed_views(self):
  old=historical_package('candidate-v22')
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp).resolve();_,_,work,value,group=self.setup(root);h=self.helper();digest,_=write(root/'context',value)
   h.failed(work,value,group).save(root/'archive');h.save(root/'saved',value)
   code="import sys;from pathlib import Path;sys.path.insert(0,sys.argv[1]);import easysewer;from pathlib import Path;assert Path(easysewer.__file__).resolve().is_relative_to(Path(sys.argv[1]).resolve());from easysewer.runtime import RunResult;from easysewer.runtime._checkpoint_context import read;from easysewer.runtime._checkpoint_container import load;root=Path(sys.argv[2]);\nfor action in (lambda:RunResult.load(root/'archive'),lambda:read(root/'context',sys.argv[3]),lambda:load(root/'saved')):\n try:action()\n except ValueError:pass\n else:raise AssertionError('Old reader accepted mixed views')"
   child=subprocess.run([sys.executable,'-I','-B','-c',code,str(old),str(root),digest],capture_output=True,text=True,timeout=45);self.assertEqual(child.returncode,0,child.stdout+child.stderr)
 def test_wrong_current_file_kind_rejects_checkpoint_without_removing_work(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp).resolve();source,_,work,value,group=self.setup(root);p=self.paths(work,value);p['F'].unlink();p['F'].mkdir()
   with self.assertRaises(ValueError):self.helper().save(root/'bad',value)
   self.assertFalse((root/'bad').exists());self.assertTrue(p['F'].is_dir());self.assertEqual((source/'child/data').read_bytes(),b'initial')
 def test_success_requires_group_artifacts_and_rejects_detached_file_artifact(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp).resolve();_,_,work,value,group=self.setup(root);h=self.helper();result=h.success(work,value,group);result.save(root/'archive');self.assertTrue(RunResult.load(root/'archive').succeeded)
   r=next(r for r in value.resources if r.kind=='file');a=FileArtifact(role='run:resource',path=str(work/r.initial_relative_path),sha256=r.sha256,size=r.size,owner=r.owner,field=r.field,complete=True)
   with self.assertRaisesRegex(ValueError,'complete group'):replace(result,artifacts=(*result.artifacts,a))
 def test_file_budget_source_change_and_copy_error_do_not_modify_source(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp).resolve();source=root/'source';source.mkdir();(source/'data').write_bytes(b'initial');requests=(graph.DirectoryRequest(key='directory',source=source,access='read_write',required=True),graph.DirectoryRequest(key='file',source=source/'data',access='read',required=True,kind='file'))
   with self.assertRaisesRegex(ValueError,'budget'):graph.plan_graphs((requests[0],replace(requests[1],limits=DirectoryLimits(total_bytes=6))))
   plan,=graph.plan_graphs(requests);error=OSError('copy failure')
   with patch.object(graph,'copy_input',side_effect=error),self.assertRaises(OSError) as caught:graph.capture_graph(plan,root/'failed')
   self.assertIs(caught.exception,error);self.assertEqual((source/'data').read_bytes(),b'initial');(source/'data').write_bytes(b'changed')
   with self.assertRaises(ValueError):graph.capture_graph(plan,root/'changed')
   self.assertFalse((root/'changed').exists())
 def test_file_only_staging_keeps_existing_ungrouped_layout(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp).resolve();(root/'file').write_bytes(b'initial');_,model=prepared(root,(row('F','file',kind='file'),));work=root/'work';work.mkdir();plans=prep.inventory(model,input_directory=root,working_directory=root);resources,_,_=prep.stage(model,plans,work,'assets',checkpoint=lambda:None)
   self.assertIsNone(resources[0].directory_group);self.assertIsNone(resources[0].initial_relative_path);self.assertEqual(resources[0].relative_path,'assets/inputs/r0.dat')

if __name__=='__main__':unittest.main()
