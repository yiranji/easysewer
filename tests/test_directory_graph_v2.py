"""Graph foundation qualification; public staging integration remains pending."""
from dataclasses import replace
from pathlib import Path
import json,os,shutil,tempfile,unittest
from unittest.mock import patch
from easysewer.runtime import _directory_graph as graph, _directory_tree as trees
from easysewer.runtime.results import DirectoryArtifact,RunResult
from easysewer.model import Ref
from test_result_archive_v2 import failure_result

class DirectoryGraphTests(unittest.TestCase):
 def req(self,key,path,access='read',required=True,limits=trees.DirectoryLimits()):
  return graph.DirectoryRequest(key=key,source=path,access=access,required=required,limits=limits)
 def nested(self,root):
  source=root/'source';source.mkdir();(source/'child').mkdir();(source/'child/data').write_bytes(b'initial');return source
 def cross(self,root):
  a=root/'a';b=root/'b';a.mkdir();b.mkdir();(a/'data').write_bytes(b'initial');(b/'alias').hardlink_to(a/'data');(b/'equal').write_bytes(b'initial');return a,b
 def private(self,root,plan,name='current'):
  state=graph.capture_graph(plan,root/name)
  return state,{v.key:root/name/v.path for v in state.layout.views}
 def test_parent_child_views_share_create_modify_and_delete(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp).resolve();source=self.nested(root);plans=graph.plan_graphs((self.req('child',source/'child','read_write'),self.req('parent',source)))
   self.assertEqual(len(plans),1);plan=plans[0];self.assertTrue(plan.state.layout.mutable);self.assertEqual(len(plan.state.layout.roots),1)
   initial,ip=self.private(root,plan,'initial');current,p=self.private(root,plan)
   self.assertEqual(p['child'],p['parent']/'child');self.assertTrue((p['child']/'data').samefile(p['parent']/'child/data'))
   (p['child']/'data').write_bytes(b'changed');self.assertEqual((p['parent']/'child/data').read_bytes(),b'changed')
   (p['parent']/'child/new').write_bytes(b'new');self.assertEqual((p['child']/'new').read_bytes(),b'new')
   (p['child']/'data').unlink();self.assertFalse((p['parent']/'child/data').exists());self.assertEqual((source/'child/data').read_bytes(),b'initial');self.assertEqual((ip['child']/'data').read_bytes(),b'initial')
   observed=graph.inspect_graph(root/'current',current.layout);self.assertIsNotNone(observed.view('child'));self.assertNotEqual(observed.tree,initial.tree)
 def test_cross_root_links_preserve_coupling_without_content_merging_or_external_links(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp).resolve();a,b=self.cross(root);outside=root/'outside';outside.hardlink_to(a/'data');plan,=graph.plan_graphs((self.req('a',a,'read_write'),self.req('b',b)))
   state,p=self.private(root,plan);self.assertEqual(len(state.layout.roots),2);self.assertTrue(state.tree.has_hardlinks)
   self.assertTrue((p['a']/'data').samefile(p['b']/'alias'));self.assertFalse((p['b']/'alias').samefile(p['b']/'equal'));self.assertFalse((p['a']/'data').samefile(outside))
   (p['a']/'data').write_bytes(b'changed');self.assertEqual((p['b']/'alias').read_bytes(),b'changed');self.assertEqual((p['b']/'equal').read_bytes(),b'initial');self.assertEqual(outside.read_bytes(),b'initial')
 def test_transitive_alias_groups_leave_unrelated_roots_independent(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp).resolve();a,b=self.cross(root);c=root/'c';c.mkdir();(c/'data').hardlink_to(b/'alias');d=root/'d';d.mkdir();(d/'data').write_bytes(b'initial')
   plans=graph.plan_graphs(tuple(self.req(k,p,'read_write' if k=='c' else 'read') for k,p in (('a',a),('b',b),('c',c),('d',d))))
   self.assertEqual([[v.key for v in p.state.layout.views] for p in plans],[['a','b','c'],['d']]);self.assertTrue(plans[0].state.layout.mutable);self.assertFalse(plans[1].state.layout.mutable)
   _,p=self.private(root,plans[0]);self.assertTrue((p['a']/'data').samefile(p['c']/'data'))
 def test_identical_source_consumers_use_one_view_without_double_counting(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp).resolve();source=self.nested(root);plan,=graph.plan_graphs((self.req('a',source),self.req('b',source,'read_write')),limits=trees.DirectoryLimits(total_bytes=7,entries=3,depth=3))
   self.assertEqual(plan.state.tree.total_bytes,7);self.assertEqual(plan.state.layout.view('a').path,plan.state.layout.view('b').path);self.assertTrue(plan.state.layout.mutable)
   with self.assertRaisesRegex(ValueError,'Duplicate'):graph.plan_graphs((self.req('a',source),self.req('a',source)))
 def test_optional_absent_parent_and_child_keep_a_shared_future_view(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp).resolve();source=root/'absent';plan,=graph.plan_graphs((self.req('child',source/'child','read_write',False),self.req('parent',source,'read',False)))
   state,p=self.private(root,plan);self.assertEqual(state.tree.entries,());self.assertFalse(p['parent'].exists());self.assertIsNone(state.view('child'))
   p['child'].mkdir(parents=True);(p['child']/'new').write_bytes(b'new');self.assertEqual((p['parent']/'child/new').read_bytes(),b'new');updated=graph.inspect_graph(root/'current',state.layout);self.assertIsNotNone(updated.view('parent'));self.assertIsNotNone(updated.view('child'));self.assertFalse(source.exists())
 def test_absent_child_of_existing_parent_and_required_missing_are_distinct(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp).resolve();source=self.nested(root)
   requests=(self.req('parent',source,'read_write'),self.req('missing',source/'new','read',False))
   plan,=graph.plan_graphs(requests);state,p=self.private(root,plan);self.assertIsNone(state.view('missing'));p['missing'].mkdir();self.assertTrue((p['parent']/'new').is_dir())
   with self.assertRaises(FileNotFoundError):graph.plan_graphs((replace(requests[1],required=True),))
 def test_each_view_and_aggregate_limits_apply(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp).resolve();source=self.nested(root)
   with self.assertRaisesRegex(ValueError,'byte budget'):graph.plan_graphs((self.req('parent',source),self.req('child',source/'child',limits=trees.DirectoryLimits(total_bytes=6))))
   a,b=self.cross(root)
   with self.assertRaisesRegex(ValueError,'budget'):graph.plan_graphs((self.req('a',a),self.req('b',b)),limits=trees.DirectoryLimits(total_bytes=20))
   with self.assertRaisesRegex(ValueError,'budget'):graph.plan_graphs((self.req('parent',source),),limits=trees.DirectoryLimits(depth=2))
 def test_only_declared_roots_are_traversed(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp).resolve();a,b=self.cross(root);unrelated=root/'unrelated';unrelated.mkdir();(unrelated/'untouched').write_bytes(b'other');original=graph._scan;seen=[]
   def scan(path,limits):
    seen.append(Path(path));self.assertIn(Path(path),(a,b));return original(path,limits)
   with patch.object(graph,'_scan',scan):plan,=graph.plan_graphs((self.req('a',a),self.req('b',b)))
   self.assertTrue(seen);self.private(root,plan);self.assertEqual((unrelated/'untouched').read_bytes(),b'other')
 def test_source_change_between_observation_and_capture_is_rejected_before_target(self):
  for change in ('split','new','replace'):
   with self.subTest(change=change),tempfile.TemporaryDirectory() as tmp:
    root=Path(tmp).resolve();a,b=self.cross(root);plan,=graph.plan_graphs((self.req('a',a),self.req('b',b)))
    if change=='split':(b/'alias').unlink();(b/'alias').write_bytes(b'initial')
    elif change=='new':(b/'new').mkdir()
    else:(a/'data').rename(root/'held');(a/'data').write_bytes(b'initial')
    with self.assertRaisesRegex(ValueError,'changed'):graph.capture_graph(plan,root/'target')
    self.assertFalse((root/'target').exists())
 def test_source_change_during_copy_refuses_graph_and_keeps_changes(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp).resolve();a,b=self.cross(root);plan,=graph.plan_graphs((self.req('a',a),self.req('b',b)));original=graph.copy_input;fired=[]
   def copy(*args,**kwargs):
    result=original(*args,**kwargs)
    if not fired:fired.append(True);(b/'new').mkdir()
    return result
   with patch.object(graph,'copy_input',copy),self.assertRaises(ValueError):graph.capture_graph(plan,root/'target')
   self.assertEqual(fired,[True]);self.assertTrue((b/'new').is_dir());self.assertEqual((a/'data').read_bytes(),b'initial')
 def test_cancel_and_link_failure_keep_error_identity_without_copy_fallback(self):
  for phase in ('cancel','link'):
   with self.subTest(phase=phase),tempfile.TemporaryDirectory() as tmp:
    root=Path(tmp).resolve();a,b=self.cross(root);plan,=graph.plan_graphs((self.req('a',a),self.req('b',b)));target=root/'target';error=OSError('stopped');error.__cause__=ValueError('cause')
    def stop():
     if (target/plan.state.layout.view('a').path/'data').exists():raise error
    with self.assertRaises(OSError) as caught:
     if phase=='cancel':graph.capture_graph(plan,target,checkpoint=stop)
     else:
      with patch.object(trees.os,'link',side_effect=error):graph.capture_graph(plan,target)
    self.assertIs(caught.exception,error);self.assertIsInstance(caught.exception.__cause__,ValueError);self.assertTrue((a/'data').samefile(b/'alias'));self.assertEqual((a/'data').read_bytes(),b'initial');self.private(root,plan,'retry')
 def test_destination_and_source_overlap_or_existing_root_never_modified(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp).resolve();source=self.nested(root);plan,=graph.plan_graphs((self.req('p',source),));before=trees.inspect_tree(source)
   for target in (source/'private',root):
    with self.assertRaisesRegex(ValueError,'overlaps'):graph.capture_graph(plan,target)
   existing=root/'existing';existing.mkdir();(existing/'keep').write_bytes(b'keep')
   with self.assertRaises(FileExistsError):graph.capture_graph(plan,existing)
   self.assertEqual((existing/'keep').read_bytes(),b'keep');self.assertEqual(trees.inspect_tree(source),before)
 def test_view_projection_recanonicalizes_links_whose_original_representative_is_outside_view(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp).resolve();a,b=self.cross(root);(b/'second').hardlink_to(a/'data');plan,=graph.plan_graphs((self.req('a',a),self.req('b',b)))
   view=plan.state.view('b');self.assertTrue(view.has_hardlinks);self.assertEqual([(e.path,e.hardlink_to) for e in view.entries if e.hardlink_to],[('second','alias')]);self.assertEqual(view,trees.inspect_tree(b))
 def test_live_state_supports_root_deletion_and_recreation_without_losing_layout(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp).resolve();a,b=self.cross(root);plan,=graph.plan_graphs((self.req('a',a,'read_write'),self.req('b',b)));state,p=self.private(root,plan)
   self.assertTrue(p['a'].resolve().is_relative_to((root/'current').resolve()));shutil.rmtree(p['a']);deleted=graph.inspect_graph(root/'current',state.layout);self.assertIsNone(deleted.view('a'));self.assertIsNotNone(deleted.view('b'))
   p['a'].mkdir();(p['a']/'new').write_bytes(b'new');created=graph.inspect_graph(root/'current',state.layout);self.assertIsNotNone(created.view('a'));self.assertEqual((p['b']/'alias').read_bytes(),b'initial')
 def test_state_rejects_undeclared_roots_non_directory_views_and_budget_growth(self):
  for change in ('foreign','kind','budget'):
   with self.subTest(change=change),tempfile.TemporaryDirectory() as tmp:
    root=Path(tmp).resolve();source=self.nested(root);plan,=graph.plan_graphs((self.req('p',source,limits=trees.DirectoryLimits(total_bytes=7)),self.req('c',source/'child')));state,p=self.private(root,plan)
    if change=='foreign':(root/'current/foreign').mkdir()
    elif change=='kind':(p['c']/'data').unlink();p['c'].rmdir();p['c'].write_bytes(b'file')
    else:(p['p']/'new').write_bytes(b'new')
    with self.assertRaises(ValueError):graph.inspect_graph(root/'current',state.layout)
 def test_forest_artifact_roundtrip_retains_cross_root_links_with_explicit_retained_layout(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp).resolve();a,b=self.cross(root);plan,=graph.plan_graphs((self.req('a',a,'read_write'),self.req('b',b)));state,p=self.private(root,plan)
   artifact=DirectoryArtifact.from_path(root/'current',role='run:resource',owner=Ref(collection='test:resources',key='graph'),field=('file',),complete=False);replace(failure_result(),directory_artifacts=(artifact,)).save(root/'saved')
   for path in (root/'current',a,b):
    self.assertTrue(path.resolve().is_relative_to(root));shutil.rmtree(path)
   (root/'saved').rename(root/'moved');restored=RunResult.load(root/'moved').directory_artifacts[0].materialize(root/'restored');self.assertEqual(graph.inspect_graph(root/'restored',state.layout),state)
   pa=root/'restored'/state.layout.view('a').path;pb=root/'restored'/state.layout.view('b').path;(pa/'data').write_bytes(b'changed');self.assertEqual((pb/'alias').read_bytes(),b'changed');self.assertEqual((pb/'equal').read_bytes(),b'initial')
   # Layout deliberately retained by caller; it is not yet part of RunSnapshot wire.
   self.assertEqual(json.loads((root/'moved/result.json').read_bytes())['schema_version'],'1.7')

if __name__=='__main__':unittest.main()
