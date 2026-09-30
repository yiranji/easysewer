"""Explicit directory preflight is separate from runtime ownership acceptance."""
import tempfile,unittest
from pathlib import Path
from dataclasses import replace
from easysewer.runtime import check_files
from easysewer.runtime.directory_resources import DirectoryAdapter,DirectoryLimits,directory_adapters
from easysewer.io.interface_inspection import InterfaceInspection
from easysewer.validation import ValidationReport,Diagnostic,Severity
from directory_fixture import model,row

class DirectoryPreflightTests(unittest.TestCase):
 def accepted(self,path,**kwargs):
  self.assertEqual((path/'data').read_bytes(),b'expected');self.assertEqual(kwargs['manifest'].total_bytes,8)
  return InterfaceInspection(format=kwargs['use'].format,status='validated')
 def tree(self,parent):
  root=parent/'source';root.mkdir();(root/'empty').mkdir();(root/'data').write_bytes(b'expected');return root
 def test_missing_adapter_is_visible_incomplete_and_registered_format_validates(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=self.tree(Path(tmp));m=model(row('input',root));before=sorted(root.iterdir());missing=check_files(m)
   self.assertTrue(missing.report.is_valid);self.assertFalse(missing.complete);self.assertIn('files.directory_adapter_missing',{v.code for v in missing.report.diagnostics})
   supplied=check_files(m,directory_adapters={'test:directory.format':DirectoryAdapter(inspector=self.accepted)})
   self.assertTrue(supplied.complete,supplied.report);self.assertEqual(supplied.checks[0].inspection.status,'validated');self.assertEqual(sorted(root.iterdir()),before)
   no_data=check_files(m,inspect_data=False,directory_adapters={'test:directory.format':DirectoryAdapter(inspector=self.accepted)})
   self.assertTrue(no_data.report.is_valid);self.assertFalse(no_data.complete)
 def test_directory_containment_collisions_include_inactive_inputs_and_outputs(self):
  with tempfile.TemporaryDirectory() as tmp:
   parent=Path(tmp);root=self.tree(parent)
   cases=[(row('input',root),row('out',root/'data',kind='file',access='write')),
          (row('input',root,active=False),row('out',root/'future',kind='file',access='write')),
          (row('input',root/'data',kind='file'),row('out',root,access='write')),
          (row('first',root,access='write'),row('second',root/'nested',access='write'))]
   for rows in cases:
    checked=check_files(model(*rows),overwrite=True,inspect_data=False)
    errors=[d for d in checked.report.errors if d.code=='files.path_collision'];self.assertTrue(errors);self.assertTrue(errors[0].subject);self.assertTrue(errors[0].related)
   self.assertEqual((root/'data').read_bytes(),b'expected');self.assertFalse((root/'future').exists())
 def test_inactive_missing_directory_requires_no_adapter_or_filesystem_inspection(self):
  with tempfile.TemporaryDirectory() as tmp:
   missing=Path(tmp)/'missing';checked=check_files(model(row('inactive',missing,active=False)))
   self.assertTrue(checked.complete);self.assertEqual([v.code for v in checked.report.diagnostics],['files.inactive_consumer']);self.assertFalse(missing.exists())
 def test_inspector_capabilities_and_diagnostics_are_resolved(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=self.tree(Path(tmp));m=model(row('input',root))
   def inspector(path,**kwargs):return InterfaceInspection(format=kwargs['use'].format,status='validated',required_capabilities=('test:directory:1',))
   registry={'test:directory.format':DirectoryAdapter(inspector=inspector)}
   pending=check_files(m,directory_adapters=registry);self.assertFalse(pending.complete);self.assertIn('files.backend_capabilities_pending',{v.code for v in pending.report.diagnostics})
   missing=check_files(m,directory_adapters=registry,backend_capabilities=());self.assertFalse(missing.report.is_valid)
   present=check_files(m,directory_adapters=registry,backend_capabilities=('test:directory:1',));self.assertTrue(present.complete)
 def test_budget_wrong_format_mutation_and_wrong_return_are_not_accepted(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=self.tree(Path(tmp));m=model(row('input',root));registry={'test:directory.format':DirectoryAdapter(inspector=self.accepted)}
   small=check_files(m,max_bytes=7,directory_adapters=registry);self.assertFalse(small.report.is_valid);self.assertIsNone(small.checks[0].inspection)
   wrong={'test:directory.format':DirectoryAdapter(inspector=lambda *a,**k:InterfaceInspection(format='test:other',status='validated'))}
   self.assertFalse(check_files(m,directory_adapters=wrong).report.is_valid)
   invalid={'test:directory.format':DirectoryAdapter(inspector=lambda *a,**k:None)}
   with self.assertRaises(TypeError):check_files(m,directory_adapters=invalid)
   def mutate(path,**kwargs):
    result=self.accepted(path,**kwargs);(path/'extra').write_bytes(b'changed');return result
   changed=check_files(m,directory_adapters={'test:directory.format':DirectoryAdapter(inspector=mutate)})
   self.assertFalse(changed.report.is_valid);self.assertEqual((root/'extra').read_bytes(),b'changed')
 def test_checkpoint_identity_propagates_from_inside_custom_inspector(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=self.tree(Path(tmp));m=model(row('input',root));active=[];error=OSError('cancel after inspector')
   def inspector(path,**kwargs):active.append(True);return self.accepted(path,**kwargs)
   def cancel():
    if active:raise error
   with self.assertRaises(OSError) as caught:check_files(m,directory_adapters={'test:directory.format':DirectoryAdapter(inspector=inspector)},checkpoint=cancel)
   self.assertIs(caught.exception,error);self.assertEqual((root/'data').read_bytes(),b'expected')
 def test_hardlink_outside_tree_and_reverse_directory_alias_are_rejected(self):
  with tempfile.TemporaryDirectory() as tmp:
   parent=Path(tmp);root=self.tree(parent);alias=parent/'external'
   try:alias.hardlink_to(root/'data')
   except OSError as error:self.skipTest('Hardlinks unavailable: '+str(error))
   cases=[(row('input',root),row('output',alias,kind='file',access='write')),
          (row('input',root,active=False),row('output',alias,kind='file',access='write')),
          (row('input',alias,kind='file'),row('output',root,access='write'))]
   for rows in cases:
    result=check_files(model(*rows),overwrite=True,inspect_data=False)
    self.assertIn('files.path_collision',{d.code for d in result.report.errors})
   self.assertEqual(alias.read_bytes(),b'expected');self.assertEqual((root/'data').read_bytes(),b'expected')
 def test_alias_budget_cannot_silently_certify_no_collision(self):
  with tempfile.TemporaryDirectory() as tmp:
   parent=Path(tmp);root=self.tree(parent);output=parent/'outside';output.write_bytes(b'old')
   registry={'test:directory.format':DirectoryAdapter(inspector=self.accepted,limits=DirectoryLimits(entries=1))}
   result=check_files(model(row('input',root),row('output',output,kind='file',access='write')),
       overwrite=True,inspect_data=False,directory_adapters=registry)
   self.assertIn('files.directory_alias_incomplete',{d.code for d in result.report.errors})
   self.assertEqual(output.read_bytes(),b'old')
 def test_registry_validates_and_copies_caller_mapping(self):
  adapter=DirectoryAdapter(inspector=self.accepted);values={'test:directory.format':adapter};copied=directory_adapters(values);values.clear();self.assertEqual(copied,{'test:directory.format':adapter})
  with self.assertRaises(TypeError):DirectoryAdapter(inspector=None)
  with self.assertRaises(TypeError):DirectoryAdapter(inspector=self.accepted,limits=None)
  with self.assertRaises(TypeError):directory_adapters({'test:directory.format':lambda:None})
  with self.assertRaises(ValueError):directory_adapters({'plain':adapter})
if __name__=='__main__':unittest.main()
