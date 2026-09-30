"""Executed-codec ownership and offline directory reconstruction, not native resume."""
from dataclasses import replace
import hashlib,json,os,shutil,subprocess,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from easysewer.model import FileReference,Model,Ref
from easysewer.runtime import Checkpoint
from easysewer.runtime._checkpoint_container import Builder,execution_layout
from easysewer.runtime._directory_tree import inspect_tree
from easysewer.runtime._preparation import inventory
from test_checkpoint_container_v2 import snapshot,native_prefix
from directory_roundtrip_fixture import schema,row,prepared


def tree(root,name='inputs/树目录'):
 p=root/name;p.mkdir(parents=True);(p/'empty').mkdir();(p/'nested').mkdir();(p/'nested/data').write_bytes(b'content\x00\xff');(p/'zero').touch();return p

def saved(target,value):
 with Builder(target,value) as builder:builder.finish(native_prefix(value,builder.binding),(),output_locations={'outputs':[],'trace':None})
 return Checkpoint.load(target)

def updated_input(value,raw):return replace(value,input_bytes=raw,input_sha256=hashlib.sha256(raw).hexdigest())

class DirectoryReconstructionTests(unittest.TestCase):
 def test_real_codec_public_restore_after_move_and_source_deletion(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);original=root/'original';original.mkdir();source=tree(original);value,model=prepared(original,(row('R','inputs/树目录'),row('Shared','inputs/树目录')))
   restored_model=Model.from_json_document(model.to_json_document(),schema=schema());self.assertEqual(restored_model.to_document().text,model.to_document().text)
   item=saved(root/'saved',value);manifest=value.resources[0].tree;shutil.rmtree(original);(root/'saved').rename(root/'moved');item=Checkpoint.load(root/'moved')
   with patch('ctypes.CDLL',side_effect=AssertionError('No native load')):
    first=item.materialize(root/'first',schema=schema());second=item.materialize(root/'second',schema=schema())
   self.assertEqual(first.resources,value.resources);self.assertEqual(first.input_bytes,value.input_bytes)
   for name in ('first','second'):
    self.assertEqual(inspect_tree(root/name/'inputs/树目录'),manifest);self.assertEqual((root/name/'model.inp').read_bytes(),value.input_bytes)
   (root/'first/inputs/树目录/nested/data').write_bytes(b'edited private copy')
   self.assertEqual((root/'second/inputs/树目录/nested/data').read_bytes(),b'content\x00\xff');self.assertEqual(Checkpoint.load(root/'moved'),item)
 def test_missing_codec_or_unmatched_owner_field_and_flags_reject_before_creation(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);tree(root);value,model=prepared(root,(row('R','inputs/树目录'),));item=saved(root/'saved',value)
   with self.assertRaises(ValueError):item.materialize(root/'no-codec')
   self.assertFalse((root/'no-codec').exists());res=value.resources[0]
   variants=[(),(res,res),(replace(res,owner=Ref(collection='test:directory',key='Wrong')),),(replace(res,field=('different',)),),(replace(res,role='test:different'),),(replace(res,format='test:different'),),(replace(res,access='read_write'),),(replace(res,required=False),),(replace(res,tree=None,active=False),),(replace(res,relative_path='inputs/other'),)]
   for index,resources in enumerate(variants):
    with self.subTest(index=index),self.assertRaises(ValueError):execution_layout(replace(value,resources=resources),root/('bad-'+str(index)),schema=schema())
    self.assertFalse((root/('bad-'+str(index))).exists())
   # Snapshot-only resources cannot manufacture undeclared executed inputs.
   raw=__import__('test_options_v2').network().to_document().text.encode()
   with self.assertRaises(ValueError):execution_layout(updated_input(value,raw),root/'unclaimed',schema=schema())
 def test_empty_read_write_output_optional_and_inactive_directory_semantics(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);(root/'empty-input').mkdir();tree(root,'writable-input')
   rows=(row('Empty','empty-input'),row('RW','writable-input',access='read_write'),row('Out','outputs/tree',access='write'),row('Missing','optional/absent',required=False),row('Inactive','inactive/absent',active=False))
   value,model=prepared(root,rows);model.update_options(temp_directory=FileReference(path='scratch/tree',direction='output'));value=snapshot(root,model=model,resources=value.resources)
   saved(root/'saved',value).materialize(root/'rebuilt',schema=schema())
   for name in ('empty-input','outputs/tree','scratch/tree'):self.assertTrue((root/'rebuilt'/name).is_dir(),name)
   for name in ('optional/absent','inactive/absent'):self.assertFalse((root/'rebuilt'/name).exists(),name)
   self.assertEqual(inspect_tree(root/'rebuilt/writable-input'),inspect_tree(root/'writable-input'))
 def test_output_and_scratch_directory_overlap_with_inputs_is_rejected(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);tree(root,'inputs/tree')
   for output in ('inputs','inputs/tree','inputs/tree/empty','model.inp','MODEL.OUT','Inputs/other'):
    with self.subTest(output=output):
     value,model=prepared(root,(row('R','inputs/tree'),row('Out',output,access='write')))
     with self.assertRaises(ValueError):execution_layout(value,root/'bad',schema=schema())
   value,model=prepared(root,(row('R','inputs/tree'),))
   raw=value.input_bytes+b'\n[FILES]\nSAVE HOTSTART "inputs/tree/empty/state.hsf"\n'
   with self.assertRaises(ValueError):execution_layout(updated_input(value,raw),root/'bad',schema=schema())
   for temp in ('inputs','inputs/tree/empty','model.out'):
    model.update_options(temp_directory=FileReference(path=temp,direction='output'));bad=snapshot(root,model=model,resources=value.resources)
    with self.assertRaises(ValueError):execution_layout(bad,root/'bad',schema=schema())
 def test_nested_output_roots_are_owned_while_duplicates_and_external_paths_reject(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp)
   for first,second in (('out','out'),('out','OUT'),('a/x','A/y')):
    value,_=prepared(root,(row('A',first,access='write'),row('B',second,access='write')))
    with self.assertRaises(ValueError):execution_layout(value,root/'bad',schema=schema())
   value,_=prepared(root,(row('A','out',access='write'),row('B','out/nested',access='write')))
   _,directories,_=execution_layout(value,root/'nested',schema=schema());self.assertIn(Path('out'),directories);self.assertIn(Path('out/nested'),directories)
   for path in ('../outside','/outside','C:/external','NUL'):
    value,_=prepared(root,(row('A',path,access='write'),))
    with self.assertRaises(ValueError):execution_layout(value,root/'bad',schema=schema())
 def test_cancelled_or_failed_materialization_cleans_owned_partial_and_retries(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);tree(root);value,_=prepared(root,(row('R','inputs/树目录'),));item=saved(root/'saved',value);before={p.relative_to(item.directory).as_posix():p.read_bytes() for p in item.directory.rglob('*') if p.is_file()};error=OSError('cancel reconstruction');error.__cause__=RuntimeError('original cause');target=root/'partial';fired=[]
   def stop():
    if (target/'inputs/树目录/nested/data').exists():fired.append(True);raise error
   with self.assertRaises(OSError) as caught:item.materialize(target,schema=schema(),checkpoint=stop)
   self.assertIs(caught.exception,error);self.assertIsInstance(caught.exception.__cause__,RuntimeError);self.assertEqual(fired,[True]);self.assertFalse(target.exists())
   with patch('easysewer.runtime._checkpoint_container.os.fsync',side_effect=OSError('full disk')):
    with self.assertRaises(OSError):item.materialize(target,schema=schema())
   self.assertFalse(target.exists());self.assertEqual(before,{p.relative_to(item.directory).as_posix():p.read_bytes() for p in item.directory.rglob('*') if p.is_file()})
   item.materialize(root/'retry',schema=schema());self.assertEqual(inspect_tree(root/'retry/inputs/树目录'),value.resources[0].tree)
   with self.assertRaises(FileExistsError):item.materialize(root/'retry',schema=schema())
 def test_fresh_process_restore_needs_only_archive_and_explicit_codec(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);original=root/'original';original.mkdir();tree(original);value,_=prepared(original,(row('R','inputs/树目录'),));saved(root/'saved',value);shutil.rmtree(original)
   import easysewer,directory_roundtrip_fixture,test_checkpoint_container_v2
   code="import sys;from pathlib import Path;sys.path[:0]=sys.argv[1:4];from easysewer.runtime import Checkpoint;from directory_roundtrip_fixture import schema;from easysewer.runtime._directory_tree import inspect_tree;root=Path(sys.argv[4]);item=Checkpoint.load(root/'saved');value=item.materialize(root/'child',schema=schema());assert inspect_tree(root/'child/inputs/树目录')==value.resources[0].tree;assert (root/'child/model.inp').read_bytes()==value.input_bytes"
   p=subprocess.run([sys.executable,'-I','-B','-c',code,str(Path(easysewer.__file__).parent.parent),str(Path(directory_roundtrip_fixture.__file__).parent),str(Path(test_checkpoint_container_v2.__file__).parent),str(root)],capture_output=True,text=True);self.assertEqual(p.returncode,0,p.stdout+p.stderr)
 def test_missing_optional_or_inactive_directory_cannot_be_created_by_other_output(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp)
   for flags in ({'required':False},{'active':False}):
    value,_=prepared(root,(row('Missing','optional/absent',**flags),row('Out','optional/absent/output',access='write')))
    with self.assertRaisesRegex(ValueError,'absent directory'):execution_layout(value,root/'bad',schema=schema())
    value,_=prepared(root,(row('Missing','optional/absent',**flags),))
    raw=value.input_bytes+b'\n[FILES]\nSAVE HOTSTART "optional/absent/state"\n'
    with self.assertRaisesRegex(ValueError,'absent directory'):execution_layout(updated_input(value,raw),root/'bad',schema=schema())
    self.assertFalse((root/'bad').exists())
 def test_public_materialize_callback_interrupts_input_parsing_before_workspace(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);tree(root);value,_=prepared(root,(row('R','inputs/树目录'),))
   value=updated_input(value,value.input_bytes+b'\n; large preserved source\n'*10000);item=saved(root/'saved',value)
   from easysewer.runtime import _checkpoint_container as storage
   original=storage.execution_layout;inside=[];calls=[];error=OSError('cancel layout');error.__cause__=RuntimeError('cause')
   def observed(*args,**kwargs):
    inside.append(True)
    try:return original(*args,**kwargs)
    finally:inside.pop()
   def check():
    if inside:
     calls.append(True)
     if len(calls)==8:raise error
   with patch.object(storage,'execution_layout',observed),self.assertRaises(OSError) as caught:item.materialize(root/'cancelled',schema=schema(),checkpoint=check)
   self.assertIs(caught.exception,error);self.assertIsInstance(caught.exception.__cause__,RuntimeError);self.assertEqual(len(calls),8);self.assertFalse((root/'cancelled').exists())
   item.materialize(root/'retry',schema=schema());self.assertEqual((root/'retry/model.inp').read_bytes(),value.input_bytes)
 def test_runner_inventory_still_requires_live_directory_adapter(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);tree(root);value,model=prepared(root,(row('R','inputs/树目录'),))
   with self.assertRaisesRegex(ValueError,'runtime staging adapter'):inventory(model,input_directory=root,working_directory=root)
   plans=inventory(model,input_directory=root,working_directory=root,_captured_directories=value.resources);self.assertEqual(len(plans),1)

if __name__=='__main__':unittest.main()
