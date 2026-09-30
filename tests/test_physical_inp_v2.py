"""Execution UTF-8 byte limits preserve editable documents and source locations."""
import inspect,unittest
from easysewer.io.inp import InpDocument
from easysewer.io.inp.semantic import native_document_diagnostics
from easysewer.model import Model
from easysewer.schema.profiles import EPA_SWMM_5_2_4
from test_junction_gates_v2 import source,load,portable

KINDS=('junction','comment','blank','header','title','tag')
def fixture(kind,length,encoding='ascii',ending=b'\n'):
    base=source(' 1 .2 .3 20').encode()
    if kind=='junction':
        base=base.replace(b'[JUNCTIONS]\nJ 0 1 .2 .3 20 ; junction\n',b'')+b'[JUNCTIONS]\n';prefix=b'J 0 1 .2 .3 20 ; '
    elif kind=='comment':prefix=b'; '
    elif kind=='blank':prefix=b''
    elif kind=='header':prefix=b'[REPORT] ; '
    elif kind=='title':base+=b'[TITLE]\n';prefix=b''
    else:base+=b'[TAGS]\n';prefix=b'Node J "a" ; '
    budget=length-len(prefix)
    padding=(b' '*budget if kind=='blank' else b'x'*budget) if encoding=='ascii' else '雨'.encode()*(budget//3)+b'x'*(budget%3)
    line=prefix+padding;assert len(line)==length
    return base+line+ending,base.count(b'\n')+1,len(line.decode())

def physical_issues(document):
    return tuple(d for d in native_document_diagnostics(document,EPA_SWMM_5_2_4) if d.code.startswith(('inp.native_line_','inp.native_control_')))

class PhysicalInpTests(unittest.TestCase):
    def test_all_record_kinds_count_bytes_exclude_terminators_and_label_sources(self):
        for kind in KINDS:
            for encoding in (('ascii',) if kind=='blank' else ('ascii','utf8')):
                for length in (1022,1023,1024,1025):
                    for ending in (b'',b'\n',b'\r\n',b'\r'):
                        raw,line,chars=fixture(kind,length,encoding,ending)
                        document=InpDocument.from_bytes(raw,source='physical.inp')
                        issues=physical_issues(document)
                        with self.subTest(kind=kind,encoding=encoding,length=length,ending=ending):
                            self.assertEqual(len(issues),int(length>=1024))
                            self.assertEqual(document.to_bytes(),raw)
                            if issues:
                                d=issues[0];self.assertEqual(d.code,'inp.native_line_length')
                                self.assertEqual((d.span.source,d.span.line,d.span.column,d.span.end_column),('physical.inp',line,1,chars+1))
                                self.assertIn(str(length),d.message)

    def test_utf8_execution_bytes_override_source_codec_and_bom_is_not_counted(self):
        text=source(' 1 .2 .3 20')+'; '+'雨'*400+'\n'
        for codec in ('utf-8','utf-8-sig','gbk','utf-16'):
            raw=text.encode(codec);document=InpDocument.from_bytes(raw,encoding=codec,source=codec+'.inp')
            model=Model.from_document(document)
            self.assertEqual(model.to_document().to_bytes(),raw)
            self.assertTrue(model.validate().is_valid)
            d,=[d for d in model.validate(for_run=True).errors if d.code=='inp.native_line_length']
            self.assertEqual((d.span.line,d.span.column,d.span.end_column),(22,1,403))
            self.assertIn('1202',d.message)
        raw,_,_=fixture('junction',1023)
        self.assertFalse(physical_issues(InpDocument.from_bytes(b'\xef\xbb\xbf'+raw)))

    def test_controls_in_comments_and_lone_cr_are_execution_errors(self):
        for character in ('\x00','\x1a'):
            document=InpDocument.from_text(source()+'; 雨'+character+'tail\n',source='controls.inp')
            d,=[d for d in physical_issues(document) if d.code=='inp.native_control_character']
            self.assertEqual((d.span.line,d.span.column,d.span.end_column),(22,4,5))
            self.assertIn(f'U+{ord(character):04X}',d.message)
        document=InpDocument.from_text(source().replace('\n','\r'))
        issues=physical_issues(document)
        self.assertEqual(len(issues),len(document.lines)-1)
        self.assertTrue(all(d.code=='inp.native_line_ending' for d in issues))

    def test_document_draft_json_and_rendered_normalization_preserve_contract(self):
        raw,_,_=fixture('junction',1024,'utf8');model=load(raw);before=model.to_json_document()
        self.assertTrue(model.validate().is_valid)
        self.assertEqual(model.to_document().to_bytes(),raw)
        self.assertFalse(model.validate(for_run=True).is_valid)
        self.assertFalse(portable(model).validate(for_run=True).is_valid)
        for normalize in (False,True):
            rendered=model.to_document(normalize=normalize)
            expected=physical_issues(rendered)
            actual=tuple(d for d in model.validate(for_run=True,normalize=normalize).errors if d.code.startswith('inp.native_line_'))
            self.assertEqual(actual,expected)
        self.assertEqual(model.to_json_document(),before)

    def test_checks_can_cancel_and_retry_without_document_changes(self):
        from easysewer.validation._cooperative import checkpoint_scope
        raw,_,_=fixture('comment',1024);document=InpDocument.from_bytes(raw)
        interruption=OSError('cancel physical-line check')
        def stop():
            frame=inspect.currentframe()
            while frame:
                if frame.f_code.co_name=='native_document_diagnostics':raise interruption
                frame=frame.f_back
        with self.assertRaises(OSError) as caught:
            with checkpoint_scope(stop):physical_issues(document)
        self.assertIs(caught.exception,interruption)
        self.assertEqual(document.to_bytes(),raw)
        self.assertEqual(len(physical_issues(document)),1)
