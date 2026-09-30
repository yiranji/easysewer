from dataclasses import replace
from pathlib import Path
import hashlib,shutil,tempfile,unittest
from easysewer.runtime import DirectoryAdapter,Checkpoint
from easysewer.runtime._preparation import inventory,stage
from easysewer.runtime._directory_tree import inspect_tree
from test_checkpoint_container_v2 import snapshot
from test_native_public_directory_checkpoint_v2 import valid,finish
import native_directory_lifecycle_fixture as fixture
EVIDENCE=[]

def prepare(root,family):
 source,oldwork,original=fixture.prepare(root,family);model=original.model(schema=fixture.schema());rows=model.collection('test:directory');rows.replace('D',replace(rows['D'],access='read_write'));work=root/'mutable';work.mkdir();plans=inventory(model,input_directory=oldwork,working_directory=oldwork,directory_adapters={'test:directory.format':DirectoryAdapter(inspector=valid)});resources,_,_=stage(model,plans,work,'assets',checkpoint=lambda:None)
 value=snapshot(work,model=model,backend=original.backend,resources=resources,settings=original.backend_settings);value=replace(value,config_json=original.config_json)
 if family=='custom':
  from easysewer.runtime import RunConfig
  from easysewer.io.json import JsonDocument
  settings=RunConfig.from_json_document(JsonDocument.from_bytes(original.config_json));plan=fixture.backend(family).prepare_run(model,settings,value,artifact_directory=Path('assets/backend'));value=replace(value,backend_settings=plan.parameters.to_bytes());(work/'assets/backend').mkdir()
 (work/'model.inp').write_bytes(value.input_bytes);record=next(x for x in resources if x.owner.collection=='test:directory');current=work/record.relative_path
 return source,oldwork,work,value,current

class NativeWritableDirectoryRestoreTests(unittest.TestCase):
 def test_public_live_restore_rewinds_complete_tree_repeatedly_and_recaptures(self):
  for family in ('standard','custom'):
   with self.subTest(family=family),tempfile.TemporaryDirectory() as tmp:
    root=Path(tmp).resolve();baseline=root/'baseline';baseline.mkdir();_,_,bw,bv,bc=prepare(baseline,family)
    with fixture.backend(family).session(working_directory=bw) as session:session.open_checkpoint(bv,schema=fixture.schema());expected=finish(session)
    run=root/'run';run.mkdir();source,oldwork,work,value,current=prepare(run,family);initial=inspect_tree(source)
    with fixture.backend(family).session(working_directory=work) as session:
     session.open_checkpoint(value,schema=fixture.schema());session.step(max_steps=30);(current/'state').write_bytes(b'at checkpoint');(current/'state-alias').hardlink_to(current/'state');(current/'saved-empty').mkdir();saved=session.save_checkpoint(root/'saved');tree=inspect_tree(current)
     for attempt in range(2):
      (current/'state-alias').unlink();(current/'state-alias').write_bytes(b'broken');(current/'state').write_bytes(b'later');(current/'saved-empty').rmdir();(current/'later').write_bytes(b'new');session.step(max_steps=3);restored=session.restore_checkpoint(saved);self.assertTrue(restored.committed);self.assertEqual(inspect_tree(current),tree);self.assertTrue((current/'state').samefile(current/'state-alias'));again=session.save_checkpoint(root/('again-'+str(attempt)));self.assertEqual(again.simulation_seconds,saved.simulation_seconds)
     self.assertEqual(finish(session),expected)
    self.assertEqual(inspect_tree(source),initial);self.assertFalse(list(work.rglob('retired-*')))
    EVIDENCE.append(dict(family=family,kind='public-live-repeated',restores=2,recaptures=2,periods=10,bytes=len(expected),sha256=hashlib.sha256(expected).hexdigest(),full_output_equal=True,tree_rewound=True,hardlinks=True,source_unchanged=True))
 def test_moved_mutable_checkpoint_restores_after_new_workspace_changes(self):
  for family in ('standard','custom'):
   with self.subTest(family=family),tempfile.TemporaryDirectory() as tmp:
    root=Path(tmp).resolve();source,oldwork,work,value,current=prepare(root,family)
    with fixture.backend(family).session(working_directory=work) as session:
     session.open_checkpoint(value,schema=fixture.schema());session.step(max_steps=30);(current/'state').write_bytes(b'at checkpoint');saved=session.save_checkpoint(root/'saved');tree=inspect_tree(current);expected=finish(session)
    for path in (source,oldwork,work):self.assertTrue(path.resolve().is_relative_to(root));shutil.rmtree(path)
    (root/'saved').rename(root/'moved');saved=Checkpoint.load(root/'moved');snapshot=saved.materialize(root/'restored',schema=fixture.schema());record=next(x for x in snapshot.resources if x.owner.collection=='test:directory');current=root/'restored'/record.relative_path
    with fixture.backend(family).session(working_directory=root/'restored') as session:
     session.open_checkpoint(snapshot,schema=fixture.schema());(current/'state').write_bytes(b'changed after open');(current/'extra').mkdir();restored=session.restore_checkpoint(saved);self.assertTrue(restored.committed);self.assertEqual(inspect_tree(current),tree);session.save_checkpoint(root/'recaptured');self.assertEqual(finish(session),expected)
    EVIDENCE.append(dict(family=family,kind='public-moved-workspace',periods=10,bytes=len(expected),full_output_equal=True,tree_rewound=True,recaptured=True))

if __name__=='__main__':unittest.main()