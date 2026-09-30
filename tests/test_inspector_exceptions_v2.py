import tempfile,unittest
from pathlib import Path
from easysewer.runtime import check_files,DirectoryAdapter
from easysewer.io.interface_inspection import InterfaceInspection
from directory_fixture import model,row
class InspectorExceptionTests(unittest.TestCase):
 def setup_case(self,root,kind):
  source=root/'source';source.mkdir();(source/'data').write_bytes(b'expected');return source,model(row('input',source if kind=='directory' else source/'data',kind=kind))
 def registry(self,kind,callback):
  return {'directory_adapters':{'test:directory.format':DirectoryAdapter(inspector=callback)}} if kind=='directory' else {'inspectors':{'test:directory.format':callback}}
 def accepted(self,*a,**k):return InterfaceInspection(format=k['use'].format,status='validated')
 def test_callback_errors_preserve_object_cause_and_do_not_repeat(self):
  for kind in ('directory','file'):
   for error_type in (OSError,FileNotFoundError,ValueError,RuntimeError,TypeError,KeyboardInterrupt):
    for checkpoint in (None,lambda:None):
     with self.subTest(kind=kind,error_type=error_type,active=checkpoint is not None),tempfile.TemporaryDirectory() as tmp:
      source,value=self.setup_case(Path(tmp),kind);cause=LookupError('root cause');error=error_type('caller failure');error.__cause__=cause;calls=[]
      def inspector(*a,**k):calls.append(True);raise error
      with self.assertRaises(error_type) as caught:check_files(value,checkpoint=checkpoint,**self.registry(kind,inspector))
      self.assertIs(caught.exception,error);self.assertIs(error.__cause__,cause);self.assertEqual(calls,[True]);self.assertEqual((source/'data').read_bytes(),b'expected')
      self.assertTrue(check_files(value,**self.registry(kind,self.accepted)).complete)
 def test_direct_adapter_preserves_original_error(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);source,value=self.setup_case(root,'directory');error=OSError('direct');cause=ValueError('cause');error.__cause__=cause
   def inspector(*a,**k):raise error
   with self.assertRaises(OSError) as caught:DirectoryAdapter(inspector=inspector).inspect(source,use=value.file_uses()[0],model=value,encoding='utf-8',source=str(source),max_bytes=100)
   self.assertIs(caught.exception,error);self.assertIs(error.__cause__,cause)
 def test_framework_budget_and_content_errors_remain_diagnostics(self):
  with tempfile.TemporaryDirectory() as tmp:
   source,value=self.setup_case(Path(tmp),'directory');calls=[]
   def inspector(*a,**k):calls.append(True);return self.accepted(*a,**k)
   small=check_files(value,max_bytes=7,**self.registry('directory',inspector));self.assertFalse(small.report.is_valid);self.assertEqual(calls,[])
   def wrong(*a,**k):return InterfaceInspection(format='test:other',status='validated')
   self.assertFalse(check_files(value,**self.registry('directory',wrong)).report.is_valid)
   def mutate(*a,**k):(source/'extra').write_bytes(b'changed');return self.accepted(*a,**k)
   self.assertFalse(check_files(value,**self.registry('directory',mutate)).report.is_valid)
 def test_callback_can_handle_its_own_error_and_return_content_diagnostic(self):
  with tempfile.TemporaryDirectory() as tmp:
   source,value=self.setup_case(Path(tmp),'directory')
   def inspector(*a,**k):
    try:raise OSError('handled internally')
    except OSError:pass
    return InterfaceInspection(format='test:other',status='validated')
   checked=check_files(value,**self.registry('directory',inspector));self.assertFalse(checked.report.is_valid);self.assertIn('files.unavailable',{d.code for d in checked.report.errors})
 def test_nested_preflight_error_can_be_caught_by_outer_inspector(self):
  with tempfile.TemporaryDirectory() as tmp:
   source,value=self.setup_case(Path(tmp),'directory');error=ValueError('nested');calls=[]
   def fail(*a,**k):raise error
   def inspector(*a,**k):
    with self.assertRaises(ValueError) as caught:check_files(value,**self.registry('directory',fail))
    self.assertIs(caught.exception,error);calls.append(True);return self.accepted(*a,**k)
   self.assertTrue(check_files(value,**self.registry('directory',inspector)).complete);self.assertEqual(calls,[True])
 def test_success_does_not_replace_registered_inspector_or_result(self):
  for kind in ('file','directory'):
   with self.subTest(kind=kind),tempfile.TemporaryDirectory() as tmp:
    source,value=self.setup_case(Path(tmp),kind);calls=[]
    def inspector(*a,**k):calls.append(a[0]);return self.accepted(*a,**k)
    registry=self.registry(kind,inspector);adapter=registry.get('directory_adapters',{}).get('test:directory.format');result=check_files(value,**registry)
    self.assertTrue(result.complete);self.assertEqual(len(calls),1)
    if adapter:self.assertIs(registry['directory_adapters']['test:directory.format'],adapter);self.assertIs(adapter.inspector,inspector)
    else:self.assertIs(registry['inspectors']['test:directory.format'],inspector)
