from pathlib import Path
import copy,hashlib,json,tempfile,unittest
from unittest.mock import patch
from easysewer.runtime._checkpoint_context import write,read
from easysewer.runtime._checkpoint_declarations import prepare,decode,encode
from easysewer.runtime._checkpoint_container import execution_layout,Limits
from test_checkpoint_container_v2 import snapshot
import test_mixed_resource_graph_v2 as mixed
from directory_roundtrip_fixture import schema

class CheckpointDeclarationTests(unittest.TestCase):
 def fixture(self,root):return mixed.MixedResourceGraphTests().setup(root)[3]
 def test_explicit_schema_roundtrip_carries_only_verified_data(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp).resolve();value=self.fixture(root);data=prepare(value,schema());uses=decode(value,data);self.assertEqual(encode(value,uses),data);self.assertEqual(execution_layout(value,Path(value.execution_directory),schema=schema()),execution_layout(value,Path(value.execution_directory),_declarations=uses));digest,_=write(root/'context',value,schema=schema());loaded,actual=read(root/'context',digest,_with_declarations=True);self.assertEqual(loaded,value);self.assertEqual(actual,uses);self.assertEqual(json.loads((root/'context/context.json').read_bytes())['version'],8)
 def test_legacy_context_shape_and_reader_api_are_preserved_without_schema(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp).resolve();value=snapshot(root);digest,_=write(root/'context',value);data=json.loads((root/'context/context.json').read_bytes());self.assertEqual(set(data),{'kind','version','snapshot','blobs'});self.assertEqual(data['version'],1);self.assertEqual(read(root/'context',digest),value);self.assertEqual(read(root/'context',digest,_with_declarations=True),(value,None))
 def test_parent_missing_codec_rejects_before_creating_context(self):
  from easysewer.io.inp.network import default_schema
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp).resolve();value=self.fixture(root)
   with self.assertRaises(Exception):write(root/'context',value,schema=default_schema())
   self.assertFalse((root/'context').exists())
 def test_declaration_changes_and_escapes_reject(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp).resolve();value=self.fixture(root);base=prepare(value,schema());index=next(i for i,x in enumerate(base['resources']) if x['owner']['collection']=='test:directory')
   variants=[]
   for field,new in (('role','test:wrong'),('kind','file'),('access','write'),('active',False),('required',False)):
    data=copy.deepcopy(base);data['resources'][index][field]=new;variants.append(data)
   for path in ('../escape','C:/escape','/absolute','assets/../escape'):
    data=copy.deepcopy(base);data['resources'][index]['file']['path']=path;variants.append(data)
   data=copy.deepcopy(base);data['resources'].pop(index);variants.append(data)
   data=copy.deepcopy(base);data['resources'].append(copy.deepcopy(data['resources'][index]));variants.append(data)
   data=copy.deepcopy(base);data['input_sha256']='0'*64;variants.append(data)
   data=copy.deepcopy(base);data['resources'][index]['field']=[True];variants.append(data)
   for data in variants:
    with self.subTest(data=data),self.assertRaises((ValueError,TypeError)):decode(value,data)
 def test_reader_revalidates_declarations_after_manifest_digest_is_recomputed(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp).resolve();value=self.fixture(root);digest,_=write(root/'context',value,schema=schema());path=root/'context/context.json';data=json.loads(path.read_bytes());data['declarations']['resources']=[];raw=json.dumps(data).encode();path.write_bytes(raw)
   with self.assertRaises(ValueError):read(root/'context',hashlib.sha256(raw).hexdigest())
 def test_decoder_does_not_reparse_or_load_extension_code(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp).resolve();value=self.fixture(root);digest,_=write(root/'context',value,schema=schema())
   with patch('easysewer.model.Model.from_document',side_effect=AssertionError('worker must use data-only declarations')):self.assertEqual(read(root/'context',digest),value)
 def test_context_budget_failure_preserves_inputs_and_cleans_private_context(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp).resolve();value=self.fixture(root);before={p:hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob('*') if p.is_file()}
   with self.assertRaises(ValueError):write(root/'context',value,schema=schema(),limits=Limits(manifest_bytes=1))
   self.assertFalse((root/'context').exists());self.assertEqual(before,{p:hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob('*') if p.is_file()})
if __name__=='__main__':unittest.main()
