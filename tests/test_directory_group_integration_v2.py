"""Group binding through actual staging, archives and offline checkpoints."""
from dataclasses import replace
from pathlib import Path
import hashlib,io,json,shutil,subprocess,sys,tempfile,unittest
from unittest.mock import patch
from easysewer.runtime import _preparation as prep, _checkpoint_container as storage
from easysewer.runtime._directory_graph import resource_groups,consumer_key,inspect_graph
from easysewer.runtime._directory_tree import inspect_tree
from easysewer.runtime.directory_resources import DirectoryAdapter
from easysewer.runtime.results import RunResult,DirectoryArtifact,DirectoryGroupArtifact,FileArtifact
from easysewer.runtime._result_codec import Codec
from easysewer.runtime._checkpoint_context import write,read
from easysewer.io.interface_inspection import InterfaceInspection
from directory_history_fixture import historical_package
from directory_roundtrip_fixture import prepared,row,schema
from test_checkpoint_container_v2 import snapshot,native_prefix,rewrite
from test_directory_checkpoint_v2 import record
from test_result_archive_v2 import failure_result

class DirectoryGroupIntegrationTests(unittest.TestCase):
 def setup(self,root,kind='nested',readonly=False):
  source=root/'source';other=None
  if kind!='absent':source.mkdir();(source/'child').mkdir();(source/'child/data').write_bytes(b'initial')
  if kind in ('cross','independent'):
   other=root/'other';other.mkdir();(other/'equal').write_bytes(b'initial');rpath='other'
   if kind=='cross':(other/'alias').hardlink_to(source/'child/data')
   else:(other/'alias').write_bytes(b'other')
  else:rpath='source/child'
  rows=(row('W','source',access='read' if readonly else 'read_write',required=kind!='absent'),row('R',rpath,access='read_write' if kind=='independent' else 'read',required=kind!='absent'))
  _,model=prepared(root,rows);work=root/'work';work.mkdir();adapters={'test:directory.format':DirectoryAdapter(inspector=lambda p,**k:InterfaceInspection(format=k['use'].format,status='validated'))}
  plans=prep.inventory(model,input_directory=root,working_directory=root,directory_adapters=adapters);resources,outputs,issues=prep.stage(model,plans,work,'assets',checkpoint=lambda:None)
  value=snapshot(work,model=model,resources=resources);group,=resource_groups(resources).values();self.assertFalse(outputs)
  return source,other,work,value,group
 def paths(self,work,value):return {r.owner.key:work/r.relative_path for r in value.resources if r.owner.collection=='test:directory'}
 def artifacts(self,work,group,complete=False):
  result=[DirectoryGroupArtifact.from_path(group,work/(group.initial_relative_path or group.relative_path),complete=complete)]
  if group.initial_relative_path is not None:result.append(DirectoryGroupArtifact.from_path(group,work/group.relative_path,current=True,complete=complete))
  return tuple(result)
 def failed(self,work,value,group):return replace(failure_result(),run_id=value.run_id,snapshot=value,backend=value.backend,directory_group_artifacts=self.artifacts(work,group))
 def save(self,path,value):
  with storage.Builder(path,value) as b:return b.finish(native_prefix(value,b.binding),())
 def remove(self,path,root):
  if path is not None and path.exists():self.assertTrue(path.resolve().is_relative_to(root.resolve()));shutil.rmtree(path)
 def success(self,work,value,group):
  from easysewer.runtime import EngineObjects,MassBalance
  from easysewer.validation import ValidationReport
  from easysewer.io.output_metadata import OutputMetadata,FLOW_UNITS
  from easysewer.io.report_document import ReportDocument
  from test_output_v2 import binary_output
  raw=binary_output(flow_units=FLOW_UNITS.index(value.units.flow_units));report=ReportDocument.from_bytes(b'report\r\n');meta=replace(OutputMetadata.from_stream(io.BytesIO(raw)),producer=value.backend.key,semantics=value.backend.output_semantics);files=[]
  for role,name,data in (('run:input','model.inp',value.input_bytes),('run:report','model.rpt',report.raw),('run:output','model.out',raw)):
   path=work/name;path.write_bytes(data);files.append(FileArtifact(role=role,path=str(path),sha256=hashlib.sha256(data).hexdigest(),size=len(data),complete=True))
  return RunResult(run_id=value.run_id,status='succeeded',diagnostics=ValidationReport(),native_completed=True,snapshot=value,backend=value.backend,artifacts=tuple(files),directory_group_artifacts=self.artifacts(work,group,True),engine_objects=EngineObjects(groups=meta.groups),mass_balance=MassBalance(runoff_percent=0.,flow_percent=0.,quality_percent=0.),output_metadata=meta,report_document=report)
 def test_actual_staging_keeps_nested_and_cross_root_views_isolated_from_original(self):
  for kind in ('nested','cross'):
   with self.subTest(kind=kind),tempfile.TemporaryDirectory() as tmp:
    root=Path(tmp).resolve();source,other,work,value,group=self.setup(root,kind);p=self.paths(work,value);writer=p['W']/'child/data';reader=p['R']/('data' if kind=='nested' else 'alias')
    self.assertTrue(writer.samefile(reader));self.assertFalse(writer.samefile(source/'child/data'));writer.write_bytes(b'changed');self.assertEqual(reader.read_bytes(),b'changed');self.assertEqual((source/'child/data').read_bytes(),b'initial')
    if kind=='nested':(p['W']/'child/new').write_bytes(b'new');self.assertEqual((p['R']/'new').read_bytes(),b'new')
    else:self.assertEqual((p['R']/'equal').read_bytes(),b'initial')
    prep.verify_resources(value.resources,work,checkpoint=lambda:None)
    with self.assertRaises(ValueError):prep.verify_resources(value.resources,work,checkpoint=lambda:None,initial_execution=True)
 def test_member_paths_policies_trees_and_exact_group_coverage_are_bound(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp).resolve();_,_,work,value,group=self.setup(root);a,b=value.resources
   for change in ({'relative_path':'assets/elsewhere'},{'access':'read'},{'required':False},{'initial_relative_path':None},{'tree':None},{'owner':replace(a.owner,key='foreign')}):
    with self.subTest(change=change),self.assertRaises((ValueError,TypeError)):replace(a,**change)
   with self.assertRaisesRegex(ValueError,'cover exactly'):replace(value,resources=(a,))
   with self.assertRaises(ValueError):replace(value,resources=(a,replace(b,directory_group=None)))
   other=replace(group,key='different');changed=replace(a,directory_group=other)
   with self.assertRaises(ValueError):replace(value,resources=(changed,b))
 def test_codec_context_record_versions_reject_older_group_formats(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp).resolve();_,_,work,value,group=self.setup(root,'cross');r=value.resources[0];encoded=Codec(object(),result_version='1.8').encode(r);self.assertEqual(Codec(object(),result_version='1.8').decode(encoded),r)
   for version in ('1.4','1.5','1.6','1.7'):
    with self.assertRaises(ValueError):Codec(object(),result_version=version).encode(r)
    with self.assertRaises(ValueError):Codec(object(),result_version=version).decode(encoded)
   digest,_=write(root/'context',value);self.assertEqual(read(root/'context',digest),value);self.assertEqual(json.loads((root/'context/context.json').read_bytes())['version'],6)
   self.assertEqual(json.loads(record(root/'record',value))['schema_version'],'1.6');self.failed(work,value,group).save(root/'archive');self.save(root/'checkpoint',value)

 def test_historical_candidate_v20_readers_reject_grouped_directories(self):
  old=historical_package('candidate-v20')
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp).resolve();_,_,work,value,group=self.setup(root,'cross');digest,_=write(root/'context',value)
   self.failed(work,value,group).save(root/'archive');self.save(root/'checkpoint',value)
   code="import sys;from pathlib import Path;sys.path.insert(0,sys.argv[1]);import easysewer;from pathlib import Path;assert Path(easysewer.__file__).resolve().is_relative_to(Path(sys.argv[1]).resolve());from easysewer.runtime import RunResult;from easysewer.runtime._checkpoint_context import read;from easysewer.runtime._checkpoint_container import load;root=Path(sys.argv[2]);\nfor action in (lambda:RunResult.load(root/'archive'),lambda:read(root/'context',sys.argv[3]),lambda:load(root/'checkpoint')):\n try:action()\n except ValueError:pass\n else:raise AssertionError('Old reader accepted grouped directories')"
   child=subprocess.run([sys.executable,'-I','-B','-c',code,str(old),str(root),digest],capture_output=True,text=True,timeout=45);self.assertEqual(child.returncode,0,child.stdout+child.stderr)
 def test_checkpoint_relocation_keeps_initial_current_aliases_and_stable_execution_binding(self):
  for kind in ('nested','cross'):
   with self.subTest(kind=kind),tempfile.TemporaryDirectory() as tmp:
    root=Path(tmp).resolve();source,other,work,value,group=self.setup(root,kind);before=self.save(root/'before',value);paths=self.paths(work,value);(paths['W']/'child/data').write_bytes(b'changed');after=self.save(root/'after',value)
    self.assertEqual(after.data['schema_version'],'1.5');self.assertEqual(before.binding,after.binding);self.assertEqual(len(after.data['directory_states']),1)
    for path in (work,source,other):self.remove(path,root)
    (root/'after').rename(root/'moved');loaded=storage.load(root/'moved')
    with patch('ctypes.CDLL',side_effect=AssertionError('Offline fixture')):restored=loaded.materialize(root/'restored',schema=schema())
    paths=self.paths(root/'restored',restored);reader=paths['R']/('data' if kind=='nested' else 'alias');self.assertTrue((paths['W']/'child/data').samefile(reader));self.assertEqual(reader.read_bytes(),b'changed')
    group2,=resource_groups(restored.resources).values();self.assertEqual(inspect_tree(root/'restored'/group2.initial_relative_path),group.tree);self.assertEqual(self.save(root/'again',restored).binding,after.binding)
 def test_result_group_archive_restores_layout_and_aliases_without_original_paths(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp).resolve();source,other,work,value,group=self.setup(root,'cross');paths=self.paths(work,value);(paths['W']/'child/data').write_bytes(b'changed');self.failed(work,value,group).save(root/'saved');self.assertEqual(json.loads((root/'saved/result.json').read_bytes())['schema_version'],'1.8')
   for path in (work,source,other):self.remove(path,root)
   (root/'saved').rename(root/'moved');loaded=RunResult.load(root/'moved');self.assertEqual(len(loaded.directory_group_artifacts),2)
   for index,item in enumerate(loaded.directory_group_artifacts):
    with self.assertRaises(ValueError):item.view_path(value.resources[0].owner,value.resources[0].field)
    restored=item.materialize(root/('restored'+str(index)));a,b=value.resources;writer=restored.view_path(a.owner,a.field)/'child/data';reader=restored.view_path(b.owner,b.field)/'alias';self.assertTrue(writer.samefile(reader));self.assertEqual(reader.read_bytes(),b'changed' if item.current else b'initial')
   loaded.save(root/'resaved');self.assertEqual(RunResult.load(root/'resaved').snapshot,value)
 def test_success_requires_complete_initial_current_group_evidence_and_rejects_split_view_artifacts(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp).resolve();_,_,work,value,group=self.setup(root,'cross');result=self.success(work,value,group);result.save(root/'success');self.assertTrue(RunResult.load(root/'success').succeeded)
   for artifacts in ((),result.directory_group_artifacts[:1],result.directory_group_artifacts[1:]):
    with self.assertRaisesRegex(ValueError,'group artifacts'):replace(result,directory_group_artifacts=artifacts)
   r=value.resources[0];view=DirectoryArtifact.from_path(work/r.initial_relative_path,role='run:resource',owner=r.owner,field=r.field,complete=True)
   with self.assertRaisesRegex(ValueError,'complete group'):replace(result,directory_artifacts=(view,))
   with self.assertRaises(ValueError):replace(result,directory_group_artifacts=(*result.directory_group_artifacts,result.directory_group_artifacts[0]))
 def test_readonly_shared_graph_has_one_forest_and_one_artifact(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp).resolve();_,_,work,value,group=self.setup(root,'cross',readonly=True);self.assertIsNone(group.initial_relative_path);saved=self.save(root/'saved',value);self.assertEqual(saved.data['directory_states'],[]);restored=saved.materialize(root/'restored',schema=schema());p=self.paths(root/'restored',restored);self.assertTrue((p['W']/'child/data').samefile(p['R']/'alias'));self.assertEqual(len(self.artifacts(work,group)),1)
   with self.assertRaises(ValueError):DirectoryGroupArtifact.from_path(group,work/group.relative_path,current=True)
 def test_initial_absence_current_creation_and_child_deletion_reconstruct_as_views(self):
  for state in ('absent','created','deleted'):
   with self.subTest(state=state),tempfile.TemporaryDirectory() as tmp:
    root=Path(tmp).resolve();source,_,work,value,group=self.setup(root,'absent');p=self.paths(work,value)
    if state!='absent':p['R'].mkdir(parents=True);(p['R']/'data').write_bytes(b'created')
    if state=='deleted':(p['R']/'data').unlink();p['R'].rmdir()
    saved=self.save(root/'saved',value);restored=saved.materialize(root/'restored',schema=schema());q=self.paths(root/'restored',restored)
    self.assertEqual(q['W'].exists(),state!='absent');self.assertEqual(q['R'].exists(),state=='created');self.assertFalse(source.exists());self.assertEqual(inspect_tree(root/'restored'/group.initial_relative_path).entries,())
 def test_current_topology_can_change_while_initial_identity_remains_bound(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp).resolve();_,_,work,value,group=self.setup(root,'cross');before=self.save(root/'before',value);p=self.paths(work,value);(p['R']/'alias').unlink();(p['R']/'alias').write_bytes(b'initial');after=self.save(root/'after',value);self.assertEqual(before.binding,after.binding)
   restored=after.materialize(root/'restored',schema=schema());q=self.paths(root/'restored',restored);self.assertFalse((q['W']/'child/data').samefile(q['R']/'alias'));self.assertEqual(inspect_tree(root/'restored'/group.initial_relative_path),group.tree)
 def test_current_foreign_root_or_wrong_view_kind_refuses_checkpoint(self):
  for kind in ('foreign','file'):
   with self.subTest(kind=kind),tempfile.TemporaryDirectory() as tmp:
    root=Path(tmp).resolve();_,_,work,value,group=self.setup(root);p=self.paths(work,value)
    if kind=='foreign':(work/group.relative_path/'foreign').mkdir()
    else:(p['R']/'data').unlink();p['R'].rmdir();p['R'].write_bytes(b'file')
    with self.assertRaises(ValueError):self.save(root/'bad',value)
    self.assertFalse((root/'bad').exists())
 def test_executed_codec_and_tampered_group_state_refuse_before_creating_restore_root(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp).resolve();_,_,work,value,group=self.setup(root,'cross');saved=self.save(root/'saved',value)
   with self.assertRaises(Exception):saved.materialize(root/'without-codec')
   self.assertFalse((root/'without-codec').exists())
   rewrite(root/'saved',lambda d:d.update(directory_states=[]))
   with self.assertRaises(ValueError):storage.load(root/'saved')
 def test_checkpoint_capture_and_restore_failure_keep_primary_error_and_source(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp).resolve();source,_,work,value,group=self.setup(root,'cross');saved=self.save(root/'saved',value);error=OSError('cannot link restored group');error.__cause__=ValueError('cause')
   from easysewer.runtime import _directory_tree as trees
   with patch.object(trees.os,'link',side_effect=error),self.assertRaises(OSError) as caught:saved.materialize(root/'failed',schema=schema())
   self.assertIs(caught.exception,error);self.assertIsInstance(caught.exception.__cause__,ValueError);self.assertFalse((root/'failed').exists());self.assertEqual((source/'child/data').read_bytes(),b'initial');saved.materialize(root/'retry',schema=schema())

 def test_runtime_links_between_initially_independent_roots_survive_checkpoint_and_result(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp).resolve();source,other,work,value,group=self.setup(root,'independent');self.assertFalse(group.tree.has_hardlinks);before=self.save(root/'before',value);p=self.paths(work,value)
   self.assertFalse((p['W']/'child/data').samefile(p['R']/'alias'));(p['R']/'alias').unlink();(p['R']/'alias').hardlink_to(p['W']/'child/data');after=self.save(root/'after',value);self.assertEqual(before.binding,after.binding)
   restored=after.materialize(root/'restored',schema=schema());q=self.paths(root/'restored',restored);self.assertTrue((q['W']/'child/data').samefile(q['R']/'alias'));(q['W']/'child/data').write_bytes(b'changed');self.assertEqual((q['R']/'alias').read_bytes(),b'changed')
   self.failed(work,value,group).save(root/'result');loaded=RunResult.load(root/'result');current=next(a for a in loaded.directory_group_artifacts if a.current).materialize(root/'artifact');a,b=value.resources;self.assertTrue((current.view_path(a.owner,a.field)/'child/data').samefile(current.view_path(b.owner,b.field)/'alias'))
   self.assertEqual((source/'child/data').read_bytes(),b'initial');self.assertEqual((other/'alias').read_bytes(),b'other');self.assertEqual(inspect_tree(work/group.initial_relative_path),group.tree)

if __name__=='__main__':unittest.main()
