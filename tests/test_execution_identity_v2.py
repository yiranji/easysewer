from dataclasses import replace
from pathlib import Path
import hashlib,tempfile,unittest
from unittest.mock import patch
from easysewer.runtime._execution_model import execution_model
from easysewer.runtime._checkpoint_declarations import prepare,decode
from easysewer.runtime._checkpoint_container import execution_layout
from easysewer.runtime._checkpoint_context import write,read
from easysewer.runtime._directory_tree import inspect_tree
from easysewer.model import FileReference
from test_lid_v2 import usage
from execution_identity_fixture import prepared,nf

class ExecutionIdentityTests(unittest.TestCase):
 def test_custom_swapped_and_reordered_consumer_ids_reproduce_exact_execution(self):
  for mode in ('custom','swapped','reordered'):
   for normalize in (False,True):
    with self.subTest(mode=mode,normalize=normalize),tempfile.TemporaryDirectory() as tmp:
     root=Path(tmp).resolve();source,work,value,staged=prepared(root,mode,normalize);initial=inspect_tree(source);actual=execution_model(value,schema=nf.schema());self.assertEqual(tuple(actual.lid_usage),tuple(staged.lid_usage));self.assertEqual(actual.to_document(normalize=normalize).text.encode('utf-8'),value.input_bytes);data=prepare(value,nf.schema());uses=decode(value,data);self.assertEqual({u.owner for u in uses if u.role=='swmm:lid-detail'},{r.owner for r in value.resources if r.role=='swmm:lid-detail'});self.assertEqual(execution_layout(value,work,schema=nf.schema()),execution_layout(value,work,_declarations=uses));self.assertEqual(inspect_tree(source),initial)
 def test_rehashed_model_with_different_physics_cannot_supply_owner_proof(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp).resolve();_,_,value,_=prepared(root);model=value.model(schema=nf.schema());model.subcatchments.update('S',area=model.subcatchments['S'].area*2);raw=model.to_json_document().to_bytes();bad=replace(value,model_json=raw,model_sha256=hashlib.sha256(raw).hexdigest())
   with self.assertRaisesRegex(ValueError,'exact executed INP'):execution_model(bad,schema=nf.schema())
 def test_unrecorded_original_consumer_and_unknown_owner_are_not_inferred_from_paths(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp).resolve();_,_,value,_=prepared(root);model=value.model(schema=nf.schema());model.lid_usage.add(usage('third',area=1,report_file=FileReference(path='third.txt',direction='output')));raw=model.to_json_document().to_bytes();bad=replace(value,model_json=raw,model_sha256=hashlib.sha256(raw).hexdigest())
   with self.assertRaisesRegex(ValueError,'unrecorded'):execution_model(bad,schema=nf.schema())
   model=value.model(schema=nf.schema());model.lid_usage.rename('second','different');raw=model.to_json_document().to_bytes();bad=replace(value,model_json=raw,model_sha256=hashlib.sha256(raw).hexdigest())
   with self.assertRaises(ValueError):execution_model(bad,schema=nf.schema())
 def test_resource_path_and_policy_changes_keep_strict_rejection(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp).resolve();_,_,value,_=prepared(root);index=next(i for i,r in enumerate(value.resources) if r.owner.key=='second');row=value.resources[index]
   for change in ({'relative_path':row.relative_path+'.changed'},{'role':'test:changed'},{'format':'test:changed'}):
    rows=list(value.resources);rows[index]=replace(row,**change);bad=replace(value,resources=tuple(rows))
    with self.subTest(change=change),self.assertRaises(ValueError):execution_model(bad,schema=nf.schema())
 def test_layout_deferral_still_rejects_unrecorded_writer_before_creating_workspace(self):
  from easysewer.runtime._checkpoint_container import Builder
  from test_checkpoint_container_v2 import native_prefix
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp).resolve();value=snapshot_for_unrecorded_writer(root)
   with self.assertRaisesRegex(ValueError,'absent from snapshot'):execution_layout(value,root/'layout',schema=nf.schema())
   with Builder(root/'saved',value) as builder:saved=builder.finish(native_prefix(value,builder.binding),())
   with self.assertRaisesRegex(ValueError,'absent from snapshot'):saved.materialize(root/'restored',schema=nf.schema())
   self.assertFalse((root/'layout').exists());self.assertFalse((root/'restored').exists())
 def test_worker_context_preserves_verified_owners_without_loading_custom_code(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp).resolve();_,_,value,_=prepared(root);digest,_=write(root/'context',value,schema=nf.schema())
   with patch('easysewer.model.Model.from_document',side_effect=AssertionError('no worker extension decode')),patch('easysewer.model.Model.from_json_document',side_effect=AssertionError('no worker extension JSON decode')):loaded,uses=read(root/'context',digest,_with_declarations=True)
   self.assertEqual(loaded,value);self.assertIn('second',{u.owner.key for u in uses});self.assertNotIn('lid-usage-2',{u.owner.key for u in uses})
def snapshot_for_unrecorded_writer(root):
 from test_checkpoint_container_v2 import snapshot
 value=snapshot(root);raw=value.input_bytes+b'\n[FILES]\nSAVE HOTSTART safe-unrecorded.hsf\n'
 return replace(value,input_bytes=raw,input_sha256=hashlib.sha256(raw).hexdigest())

if __name__=='__main__':unittest.main()
