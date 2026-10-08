"""Internal hardlink identity, independent copies, durable reconstruction and failure."""
from dataclasses import replace
from pathlib import Path
import hashlib,json,shutil,subprocess,sys,tempfile,unittest
from unittest.mock import patch
import easysewer
from easysewer.runtime import _directory_tree as trees, _preparation as prep, _checkpoint_container as storage, _workspace as workspace
from easysewer.runtime import recover_run
from easysewer.runtime.results import DirectoryArtifact,RunResult
from easysewer.runtime.directory_resources import DirectoryAdapter
from easysewer.runtime._result_codec import Codec
from easysewer.runtime._checkpoint_context import write,read
from easysewer.io.interface_inspection import InterfaceInspection
from directory_roundtrip_fixture import prepared,row,schema
from test_checkpoint_container_v2 import snapshot,native_prefix,rewrite
from test_directory_checkpoint_v2 import record
from test_result_archive_v2 import failure_result
from test_run_recovery_v2 import CHILD
import test_mutable_directory_v2 as mutable

class HardlinkDirectoryTests(unittest.TestCase):
 def tree(self,root,name='source',linked=True):
  source=root/name;source.mkdir();(source/'nested').mkdir();(source/'empty').mkdir();(source/'b').write_bytes(b'initial')
  if linked:(source/'nested/a').hardlink_to(source/'b')
  else:(source/'nested/a').write_bytes(b'initial')
  (source/'c').write_bytes(b'initial');return source
 def setup(self,root,linked=True,readonly=False):
  source=self.tree(root,linked=linked)
  rows=(row('R','source'),) if readonly else (row('R','source'),row('W','source',access='read_write'))
  _,model=prepared(root,rows);work=root/'work';work.mkdir()
  registry={'test:directory.format':DirectoryAdapter(inspector=lambda p,**k:InterfaceInspection(format=k['use'].format,status='validated'))}
  plans=prep.inventory(model,input_directory=root,working_directory=root,directory_adapters=registry)
  resources,_,_=prep.stage(model,plans,work,'assets',checkpoint=lambda:None)
  return source,work,snapshot(work,model=model,resources=resources)
 def save(self,path,value):
  with storage.Builder(path,value) as b:return b.finish(native_prefix(value,b.binding),())
 def coupled(self,path,content=b'initial'):
  self.assertTrue((path/'b').samefile(path/'nested/a'));self.assertFalse((path/'b').samefile(path/'c'))
  self.assertEqual((path/'b').read_bytes(),content);self.assertEqual((path/'nested/a').read_bytes(),content);self.assertEqual((path/'c').read_bytes(),b'initial')
 def delete_private(self,path,root):
  self.assertTrue(path.resolve().is_relative_to(root.resolve()));shutil.rmtree(path)
 def test_manifest_canonical_order_and_logical_budget(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);source=self.tree(root);m=trees.inspect_tree(source);self.assertTrue(m.has_hardlinks);self.assertEqual(m.total_bytes,21)
   self.assertEqual([(e.path,e.hardlink_to) for e in m.entries if e.hardlink_to],[('nested/a','b')])
   (source/'a-x').hardlink_to(source/'b');m=trees.inspect_tree(source)
   self.assertEqual([(e.path,e.hardlink_to) for e in m.entries if e.hardlink_to],[('b','a-x'),('nested/a','a-x')])
   with self.assertRaisesRegex(ValueError,'byte budget'):trees.inspect_tree(source,limits=trees.DirectoryLimits(total_bytes=27))
   captured=trees.capture_tree(source,root/'capture');self.assertEqual(captured,m);self.assertTrue((root/'capture/a-x').samefile(root/'capture/nested/a'))
 def test_manifest_rejects_chains_missing_representative_and_content_mismatch(self):
  digest=hashlib.sha256(b'x').hexdigest();a=trees.DirectoryEntry(path='a',kind='file',sha256=digest,size=1);b=replace(a,path='b',hardlink_to='a');c=replace(a,path='c',hardlink_to='b')
  for entries in ((b,),(a,b,c),(a,replace(b,size=2)),(trees.DirectoryEntry(path='a',kind='directory'),b)):
   with self.subTest(entries=entries),self.assertRaises(ValueError):trees.DirectoryManifest(entries=entries,contract='easysewer:directory-tree:2')
  with self.assertRaises(ValueError):trees.DirectoryManifest(entries=(a,b))
  with self.assertRaises(ValueError):trees.DirectoryManifest(entries=(a,),contract='easysewer:directory-tree:2')
  with self.assertRaises(ValueError):replace(a,hardlink_to='b')
 def test_staging_preserves_aliases_but_isolates_original_initial_and_independent_equal_bytes(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);source,work,value=self.setup(root);r=value.resources[0];initial=work/r.initial_relative_path;current=work/r.relative_path
   (root/'outside').hardlink_to(source/'b');self.coupled(source);self.coupled(initial);self.coupled(current)
   self.assertFalse((source/'b').samefile(initial/'b'));self.assertFalse((initial/'b').samefile(current/'b'))
   (current/'nested/a').write_bytes(b'changed');self.coupled(current,b'changed');self.coupled(initial);self.coupled(source);self.assertEqual((root/'outside').read_bytes(),b'initial')
   prep.verify_resources(value.resources,work,checkpoint=lambda:None)
 def test_verification_detects_split_and_merged_equal_content(self):
  for change in ('split','merge'):
   with self.subTest(change=change),tempfile.TemporaryDirectory() as tmp:
    root=Path(tmp);source=self.tree(root);m=trees.inspect_tree(source)
    if change=='split':(source/'nested/a').unlink();(source/'nested/a').write_bytes(b'initial')
    else:(source/'c').unlink();(source/'c').hardlink_to(source/'b')
    with self.assertRaisesRegex(ValueError,'differs'):trees.verify_tree(source,m)
 def test_archive_relocation_preserves_aliases_without_linking_to_deduplicated_blobs(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);source,work,value=self.setup(root);result=mutable.MutableDirectoryTests().failed(work,value);result.save(root/'saved')
   self.assertEqual(json.loads((root/'saved/result.json').read_bytes())['schema_version'],'1.7')
   self.delete_private(work,root);self.delete_private(source,root);(root/'saved').rename(root/'moved');loaded=RunResult.load(root/'moved');artifact=loaded.directory_artifacts[0]
   before={str(p.relative_to(root/'moved')):hashlib.sha256(p.read_bytes()).hexdigest() for p in (root/'moved').rglob('*') if p.is_file()}
   artifact.materialize(root/'restored');self.coupled(root/'restored');(root/'restored/b').write_bytes(b'changed');self.coupled(root/'restored',b'changed')
   after={str(p.relative_to(root/'moved')):hashlib.sha256(p.read_bytes()).hexdigest() for p in (root/'moved').rglob('*') if p.is_file()};self.assertEqual(before,after)
   artifact.materialize(root/'second');self.coupled(root/'second');loaded.save(root/'again');self.assertEqual(RunResult.load(root/'again').directory_artifacts[0].manifest,artifact.manifest)
 def test_checkpoint_readonly_and_mutable_linked_restoration_and_versions(self):
  for readonly in (False,True):
   with self.subTest(readonly=readonly),tempfile.TemporaryDirectory() as tmp:
    root=Path(tmp);source,work,value=self.setup(root,readonly=readonly);r=value.resources[0]
    digest,_=write(root/'context',value);self.assertEqual(read(root/'context',digest),value);self.assertEqual(json.loads((root/'context/context.json').read_bytes())['version'],5)
    self.assertEqual(json.loads(record(root/'record',value))['schema_version'],'1.5')
    saved=self.save(root/'checkpoint',value);self.assertEqual(saved.data['schema_version'],'1.4');self.delete_private(work,root);self.delete_private(source,root)
    restored=saved.materialize(root/'restored',schema=schema());current=root/'restored'/r.relative_path;self.coupled(current)
    if not readonly:
     initial=root/'restored'/r.initial_relative_path;self.coupled(initial);self.assertFalse((current/'b').samefile(initial/'b'));(current/'b').write_bytes(b'changed');self.coupled(initial)
    again=self.save(root/'again',restored);self.assertEqual(saved.binding,again.binding)
 def test_current_can_gain_or_lose_links_without_changing_initial_binding(self):
  for initially_linked in (False,True):
   with self.subTest(initially_linked=initially_linked),tempfile.TemporaryDirectory() as tmp:
    root=Path(tmp);source,work,value=self.setup(root,linked=initially_linked);r=value.resources[0];current=work/r.relative_path;before=self.save(root/'before',value)
    (current/'nested/a').unlink()
    if initially_linked:(current/'nested/a').write_bytes(b'initial')
    else:(current/'nested/a').hardlink_to(current/'b')
    after=self.save(root/'after',value);self.assertEqual(before.binding,after.binding);self.assertEqual(after.data['schema_version'],'1.4')
    restored=after.materialize(root/'restored',schema=schema());current=root/'restored'/r.relative_path;initial=root/'restored'/r.initial_relative_path
    self.assertEqual((current/'b').samefile(current/'nested/a'),not initially_linked);self.assertEqual((initial/'b').samefile(initial/'nested/a'),initially_linked)
    self.assertEqual(self.save(root/'again',restored).binding,after.binding)
 def test_older_codecs_refuse_link_contract(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);_,work,value=self.setup(root);r=value.resources[0];encoded=Codec(object(),result_version='1.7').encode(r)
   self.assertEqual(Codec(object(),result_version='1.7').decode(encoded),r)
   for version in ('1.4','1.5','1.6'):
    with self.assertRaises(ValueError):Codec(object(),result_version=version).encode(r)
    with self.assertRaises(ValueError):Codec(object(),result_version=version).decode(encoded)
   mutable.MutableDirectoryTests().failed(work,value).save(root/'archive');self.save(root/'checkpoint',value);digest,_=write(root/'context',value)

 def test_current_readers_reject_mislabeled_hardlink_topology_formats(self):
  # Synthetic edits of current output exercise current readers, not old releases.
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);_,work,value=self.setup(root);digest,_=write(root/'context',value)
   mutable.MutableDirectoryTests().failed(work,value).save(root/'archive');self.save(root/'checkpoint',value)
   with patch('ctypes.CDLL',side_effect=AssertionError('Format checks must stay offline')):
    self.assertEqual(RunResult.load(root/'archive').snapshot,value)
    self.assertEqual(read(root/'context',digest),value)
    self.assertEqual(storage.load(root/'checkpoint').snapshot,value)
    formats=(
     ('archive','result.json','schema_version','1.7','1.6',RunResult.load),
     ('context','context.json','version',5,4,lambda path:read(path,hashlib.sha256((path/'context.json').read_bytes()).hexdigest())),
     ('checkpoint','checkpoint.json','schema_version','1.4','1.3',storage.load))
    for name,filename,key,version,older,load in formats:
     data=json.loads((root/name/filename).read_bytes());self.assertEqual(data[key],version)
     for label in (older,99 if name=='context' else '99.0'):
      with self.subTest(format=name,label=label):
       altered=root/(name+'-'+str(label));shutil.copytree(root/name,altered)
       if name=='checkpoint':rewrite(altered,lambda data:data.update(schema_version=label))
       else:
        changed=dict(data);changed[key]=label;(altered/filename).write_text(json.dumps(changed),encoding='utf-8')
       # The context digest/checkpoint commit matches the edited bytes, so the
       # reader must reject the format rather than merely a stale checksum.
       with self.assertRaises(ValueError):load(altered)

 def test_link_failure_has_no_copy_fallback_and_preserves_original_error(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);source,work,value=self.setup(root);manifest=trees.inspect_tree(source);saved=self.save(root/'saved',value);artifact=mutable.MutableDirectoryTests().artifacts(work,value)[0]
   for name,action in (('capture',lambda:trees.capture_tree(source,root/'capture')),('artifact',lambda:artifact.materialize(root/'artifact')),('checkpoint',lambda:saved.materialize(root/'checkpoint',schema=schema()))):
    error=OSError('hardlinks unavailable');error.__cause__=ValueError('cause')
    with self.subTest(name=name),patch.object(trees.os,'link',side_effect=error),self.assertRaises(OSError) as caught:action()
    self.assertIs(caught.exception,error);self.assertIsInstance(caught.exception.__cause__,ValueError);self.assertEqual(trees.inspect_tree(source),manifest)
   self.assertFalse((root/'checkpoint').exists());saved.materialize(root/'retry',schema=schema());self.coupled(root/'retry'/value.resources[0].relative_path)
 def test_capture_rejects_replaced_identical_representative(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);source=self.tree(root);original=trees.copy_input;fired=[]
   def copying(a,b,**kwargs):
    answer=original(a,b,**kwargs)
    if Path(b).name=='b' and not fired:
     fired.append(True);Path(b).rename(root/'held');Path(b).write_bytes(b'initial')
    return answer
   with patch.object(trees,'copy_input',copying),self.assertRaisesRegex(ValueError,'representative was replaced'):trees.capture_tree(source,root/'target')
   self.assertEqual(fired,[True]);self.coupled(source);self.assertFalse((root/'target/nested/a').exists())
 def test_alias_cancellation_preserves_cause_and_allows_independent_retry(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);source=self.tree(root);target=root/'partial';error=RuntimeError('stop after link');error.__cause__=ValueError('cause')
   def cancel():
    if (target/'nested/a').exists():raise error
   with self.assertRaises(RuntimeError) as caught:trees.capture_tree(source,target,checkpoint=cancel)
   self.assertIs(caught.exception,error);self.assertIsInstance(caught.exception.__cause__,ValueError);self.coupled(source);trees.capture_tree(source,root/'retry');self.coupled(root/'retry')
 def test_publication_preserves_links_and_link_failure_rolls_back_outputs(self):
  for fail in (False,True):
   with self.subTest(fail=fail),tempfile.TemporaryDirectory() as tmp:
    root=Path(tmp);source=self.tree(root);manifest=trees.inspect_tree(source);target=root/'published';old=root/'old';old.write_bytes(b'old');new=root/'new';new.write_bytes(b'new');transaction=workspace.OutputTransaction('linked',overwrite=True)
    try:
     transaction.reserve(((old,False),(target,True)))
     if fail:
      error=OSError('link failure')
      with patch.object(trees.os,'link',side_effect=error),self.assertRaises(OSError) as caught:transaction.publish({old:new,target:source},expected_trees={source:manifest})
      self.assertIs(caught.exception,error)
     else:transaction.publish({old:new,target:source},expected_trees={source:manifest})
    finally:transaction.close()
    self.coupled(source);self.assertEqual(old.read_bytes(),b'old' if fail else b'new');self.assertEqual(target.exists(),not fail)
    if not fail:self.coupled(target);self.assertFalse((source/'b').samefile(target/'b'))
 def test_actual_child_exit_recovery_preserves_committed_topology(self):
  child=CHILD.replace("(assets/'data').write_bytes(b'tree-data')","(assets/'data').write_bytes(b'tree-data');(assets/'alias').hardlink_to(assets/'data')")
  child=child.replace('t.publish({a:source,b:source,tree:assets})','from easysewer.runtime._directory_tree import inspect_tree\nt.publish({a:source,b:source,tree:assets},expected_trees={assets:inspect_tree(assets)})')
  self.assertIn("(assets/'alias').hardlink_to",child)
  for phase in ('prepared','all_published','committed'):
   with self.subTest(phase=phase),tempfile.TemporaryDirectory() as tmp:
    root=Path(tmp).resolve();p=subprocess.run([sys.executable,'-I','-B','-c',child,str(Path(easysewer.__file__).parent.parent),str(root),phase],capture_output=True,text=True,timeout=45);self.assertEqual(p.returncode,37,p.stdout+p.stderr)
    journal,=root.glob('.easysewer-recovery-*.json');result=recover_run(journal);self.assertEqual(result.state,'recovered',result.issues);committed=phase=='committed'
    self.assertEqual((root/'a').read_bytes(),b'new' if committed else b'old-a');self.assertEqual((root/'z').exists(),committed)
    if committed:self.assertTrue((root/'z/data').samefile(root/'z/alias'));self.assertFalse((root/'z/data').samefile(root/'source-tree/data'))

if __name__=='__main__':unittest.main()
