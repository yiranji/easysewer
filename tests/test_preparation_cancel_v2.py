"""Internal preparation cancellation, original exception semantics and retry reuse."""
import codecs,hashlib,tempfile,unittest
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch
from easysewer.io.inp import InpDocument
from easysewer.io.inp.document import TextEdit
from easysewer.io.inp import lexer,controls,semantic
from easysewer.model.resources import InlineTimeSeries,SeriesPoint
from easysewer.runtime import FileArtifact,swmm_cache_policies
from easysewer.runtime import _preparation as prep
from easysewer.validation._cooperative import checkpoint_scope
from test_options_v2 import network
from test_controls_v2 import controlled,PROGRAM
from test_cache_reuse_v2 import snapshot

class PreparationCancellationTests(unittest.TestCase):
    def interrupt(self,operation,*,at=5,error_type=ValueError):
        calls=[];error=error_type('requested interruption');cause=RuntimeError('original cause');error.__cause__=cause
        def check():
            calls.append(1)
            if len(calls)==at:raise error
        with self.assertRaises(error_type) as caught:operation(check)
        self.assertIs(caught.exception,error);self.assertIs(error.__cause__,cause);self.assertEqual(len(calls),at)

    def test_document_noop_encoding_patch_and_callback_contract(self):
        for encoding in ('utf-8','utf-8-sig','utf-16','gb18030'):
            raw='; 中文\r\n[JUNCTIONS]\r\nJ 2 ; keep\n[BAD\r\nx 1\r\n'.encode(encoding)
            old=InpDocument.from_bytes(raw,encoding=encoding)
            self.assertEqual(InpDocument.from_bytes(raw,encoding=encoding,checkpoint=lambda:None),old)
            self.assertEqual(InpDocument.from_text(old.text,encoding=encoding,checkpoint=lambda:None).to_bytes(),raw)
            edit=old.patch((TextEdit(start=0,end=0,replacement='; inserted\n'),))
            self.assertEqual(old.apply(edit,checkpoint=lambda:None),old.apply(edit));self.assertEqual(old.to_bytes(),raw)
        for action in (lambda:InpDocument.from_text('',checkpoint=1),lambda:network().to_document(checkpoint=1)):
            with self.assertRaises(TypeError):action()

    def test_parse_and_patch_interruption_preserve_error_and_reusable_source(self):
        raw=('[JUNCTIONS]\n'+''.join('J'+str(i)+' 0\n' for i in range(2000))).encode()
        original=InpDocument.from_bytes(raw);edit=original.patch(tuple(TextEdit(start=line.start,end=line.start,replacement='; row\n') for line in original.lines[1:]))
        expected=original.apply(edit)
        for kind in (ValueError,OSError,KeyboardInterrupt):
            self.interrupt(lambda check:InpDocument.from_bytes(raw,checkpoint=check),at=30,error_type=kind)
            self.interrupt(lambda check:original.apply(edit,checkpoint=check),at=5,error_type=kind)
            self.assertEqual(original.to_bytes(),raw);self.assertEqual(original.apply(edit),expected)

    def test_long_single_token_is_interruptible_inside_scanning(self):
        content='x'*200000;done=[]
        def operation(check):
            with checkpoint_scope(check):
                result=lexer.tokenize(content,offset=0,line=1,source=None,section='TITLE');done.append(True)
                return result
        self.interrupt(operation,at=5,error_type=OSError);self.assertFalse(done)
        normal=lexer.tokenize(content,offset=0,line=1,source=None,section='TITLE')
        with checkpoint_scope(lambda:None):self.assertEqual(lexer.tokenize(content,offset=0,line=1,source=None,section='TITLE'),normal)
        self.assertEqual(normal[0][0].value,content)

    def test_inventory_checks_single_large_record_without_file_references(self):
        model=network();model.timeseries.add(InlineTimeSeries(id='Long',points=tuple(SeriesPoint(time=timedelta(seconds=i),value=1.) for i in range(3000))))
        before=model.to_json_document().to_bytes();original=prep.file_references;active=[];done=[];calls=[];error=ValueError('stop recursive references')
        def references(value,*a,**k):
            enabled=isinstance(value,InlineTimeSeries)
            if enabled:active.append(True)
            try:
                yield from original(value,*a,**k)
                if enabled:done.append(True)
            finally:
                if enabled:active.pop()
        def check():
            if active:
                calls.append(1)
                if len(calls)==3:raise error
        with patch.object(prep,'file_references',references),self.assertRaises(ValueError) as caught,checkpoint_scope(check):prep.inventory(model,input_directory=None,working_directory=Path.cwd())
        self.assertIs(caught.exception,error);self.assertEqual(len(calls),3);self.assertFalse(done)
        self.assertEqual(model.to_json_document().to_bytes(),before)
        ordinary=prep.inventory(model,input_directory=None,working_directory=Path.cwd())
        with checkpoint_scope(lambda:None):self.assertEqual(prep.inventory(model,input_directory=None,working_directory=Path.cwd()),ordinary)

    def test_eager_control_clause_list_stops_before_first_rule_output(self):
        model=controlled(PROGRAM);rule=model.controls[('RULE','first')]
        conditions=(rule.conditions[0],)+tuple(replace(rule.conditions[0],conjunction='AND') for _ in range(3000))
        model.controls.update(('RULE','first'),conditions=conditions)
        conditions=model.controls[('RULE','first')].conditions
        codec=next(codec for descriptor,codec in model._schema.bindings if 'CONTROLS' in descriptor.sections)
        original=controls.checkpointed;active=[];done=[];calls=[];error=KeyboardInterrupt('stop clause assembly')
        def values(items,*a,**k):
            enabled=items is conditions
            if enabled:active.append(True)
            try:
                yield from original(items,*a,**k)
                if enabled:done.append(True)
            finally:
                if enabled:active.pop()
        def check():
            if active:
                calls.append(1)
                if len(calls)==3:raise error
        with patch.object(controls,'checkpointed',values),self.assertRaises(KeyboardInterrupt) as caught,checkpoint_scope(check):tuple(codec.encode(model._store,model.profile))
        self.assertIs(caught.exception,error);self.assertEqual(len(calls),3);self.assertFalse(done)
        expected=tuple(codec.encode(model._store,model.profile))
        with checkpoint_scope(lambda:None):self.assertEqual(tuple(codec.encode(model._store,model.profile)),expected)

    def test_row_comparison_and_cache_parse_do_not_translate_callback_error(self):
        model=network();rows=semantic._rows(model._schema,model._store,model.profile)
        left={str(i):tuple(rows.values()) for i in range(3000)};right=dict(left)
        def compare(check):
            with checkpoint_scope(check):return semantic._rows_equal(left,right)
        self.interrupt(compare,at=3);self.assertEqual(left,right)
        captured=snapshot(model);policies=swmm_cache_policies();expected=policies.capture('RUNOFF',captured)
        for kind in (ValueError,OSError,KeyboardInterrupt):
            def parse(check):
                with checkpoint_scope(check):return policies.capture('RUNOFF',captured)
            self.interrupt(parse,at=8,error_type=kind)
            self.assertEqual(policies.capture('RUNOFF',captured),expected)

    def test_artifact_active_read_stops_between_chunks_and_retries_exactly(self):
        data=b'0123456789abcdef'*65536
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'input.bin';path.write_bytes(data)
            artifact=FileArtifact(role='run:test',path=str(path),sha256=hashlib.sha256(data).hexdigest(),size=len(data),complete=True)
            self.interrupt(lambda check:artifact.read_bytes(checkpoint=check),at=4,error_type=OSError)
            self.assertEqual(path.read_bytes(),data);self.assertEqual(artifact.read_bytes(),data);self.assertEqual(artifact.read_bytes(checkpoint=lambda:None),data)
            path.write_bytes(data[:-1])
            with self.assertRaises(ValueError):artifact.read_bytes(checkpoint=lambda:None)

    def test_final_input_verification_catches_short_extra_and_changed_bytes(self):
        payload=b'0123456789abcdef'*65536
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'model.inp';path.write_bytes(payload)
            self.assertTrue(prep.input_matches(path,payload));self.assertTrue(prep.input_matches(path,payload,checkpoint=lambda:None))
            self.interrupt(lambda check:prep.input_matches(path,payload,checkpoint=check),at=4,error_type=KeyboardInterrupt)
            self.assertEqual(path.read_bytes(),payload)
            for value in (payload[:-1],payload+b'extra',payload[:131072]+b'x'+payload[131073:]):
                path.write_bytes(value);self.assertFalse(prep.input_matches(path,payload,checkpoint=lambda:None))
            path.write_bytes(b'');self.assertTrue(prep.input_matches(path,b''));self.assertFalse(prep.input_matches(path,payload))
            path.unlink()
            with self.assertRaises(FileNotFoundError):prep.input_matches(path,payload)

    def test_private_input_short_write_keeps_accurate_context_and_record_default(self):
        from easysewer.io._record_work import write_record_bytes
        class ShortStream:
            def __enter__(self):return self
            def __exit__(self,*args):return False
            def write(self,data):return len(data)-1
        class Target:
            def open(self,mode):return ShortStream()
        with self.assertRaisesRegex(OSError,'Short write while saving execution record'):write_record_bytes(Target(),b'input')
        with self.assertRaisesRegex(OSError,'Short write while saving private INP'):write_record_bytes(Target(),b'input',description='private INP')

if __name__=='__main__':unittest.main()
