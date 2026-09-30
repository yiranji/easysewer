"""Model JSON interruption keeps wire values, opaque content and source indexes."""
from copy import deepcopy
from datetime import timedelta
import codecs
import json
import unittest
from unittest.mock import patch

from easysewer.model import Model
from easysewer.model.resources import InlineTimeSeries, SeriesPoint
from easysewer.io.inp import InpDocument
from easysewer.io.json import JsonDocument
from easysewer.io.json import document as jd, model as jm, types as jt
from easysewer.io.json._work import copy_json, equal_json
from easysewer.validation._cooperative import checkpoint_scope
from test_options_v2 import network
from test_json_v2 import entry


class ModelJsonCheckpointTests(unittest.TestCase):
    def test_tree_copy_matches_deepcopy_and_retains_aliases_and_cycles(self):
        shared=[1,{'x':2}];value={'left':shared,'right':shared,'mixed':(shared,), 'number':-0.0}
        for active in (False,True):
            with self.subTest(active=active),checkpoint_scope((lambda:None) if active else None):
                clone=copy_json(value)
            self.assertEqual(clone,deepcopy(value));self.assertIsNot(clone['left'],shared)
            self.assertIs(clone['left'],clone['right']);self.assertIs(clone['mixed'][0],clone['left'])
            clone['left'].append('new');self.assertEqual(len(shared),2)
        cycle=[];cycle.append(cycle)
        with checkpoint_scope(lambda:None):clone=copy_json(cycle)
        self.assertIs(clone[0],clone);self.assertIsNot(clone,cycle)
        error=OSError('custom deepcopy failure')
        class Custom:
            def __deepcopy__(self,memo):raise error
        with self.assertRaises(OSError) as caught:
            with checkpoint_scope(lambda:None):copy_json({'custom':Custom()})
        self.assertIs(caught.exception,error)

    def test_equality_keeps_python_numeric_order_and_identity_semantics(self):
        nan=float('nan')
        cases=[(True,1),(False,-0.),(1,1.),(nan,nan),([nan],[nan]),([1,{'a':[False]}],[True,{'a':[0.]}]),
               ({'a':1,'b':2},{'b':2.,'a':True}),([1],[1,2]),({'a':1},{'b':1}),([0,-0.],[0.,0]),
               ({'a':[1,2]},{'a':[1,3]}),(None,False),([],()),({'a':[]},{'a':()})]
        for left,right in cases:
            with self.subTest(left=left,right=right),checkpoint_scope(lambda:None):
                self.assertEqual(equal_json(left,right),left==right)

    def test_large_tree_clone_and_comparison_interrupt_without_source_changes(self):
        source={'future':list(range(5000))};before=json.dumps(source)
        for operation in (lambda:copy_json(source),lambda:equal_json(source,deepcopy(source))):
            for kind in (ValueError,OSError,KeyboardInterrupt):
                with self.subTest(kind=kind):
                    calls=[];error=kind('stop JSON work');cause=LookupError('cause');error.__cause__=cause
                    def stop():
                        calls.append(1)
                        if len(calls)==5:raise error
                    with self.assertRaises(kind) as caught:
                        with checkpoint_scope(stop):operation()
                    self.assertIs(caught.exception,error);self.assertIs(error.__cause__,cause)
                    self.assertEqual(json.dumps(source),before)
        self.assertEqual(copy_json(source),source)

    def test_public_export_preserves_inp_json_bom_and_unknown_field_bytes(self):
        plain=network();inp=Model.from_document(InpDocument.from_text(plain.to_document().text,source='source.inp'))
        payload=plain.to_json_document().data
        entry(payload,'swmm:nodes','J')['value']['future']={'values':[0,True,1.5,'中文']}
        payload['extensions']['vendor:tree']={'values':list(range(100))}
        document=JsonDocument.from_bytes(codecs.BOM_UTF8+json.dumps(payload,ensure_ascii=False,separators=(',',':')).encode('utf-8'),source='source.json')
        parsed=Model.from_json_document(document)
        for model in (plain,inp,parsed):
            before=model.to_json_document()
            after=model.to_json_document(checkpoint=lambda:None)
            self.assertEqual(after.to_bytes(),before.to_bytes())
            self.assertEqual(model.validate(),model.validate(checkpoint=lambda:None))
        self.assertIs(parsed.to_json_document(checkpoint=lambda:None),document)
        parsed.nodes.update('J',elevation=4)
        expected=parsed.to_json_document().to_bytes()
        actual=parsed.to_json_document(checkpoint=lambda:None)
        self.assertEqual(actual.to_bytes(),expected)
        self.assertEqual(entry(actual.data,'swmm:nodes','J')['value']['future'],{'values':[0,True,1.5,'中文']})
        with self.assertRaises(TypeError):plain.to_json_document(checkpoint=1)

    def test_preserved_model_tree_copy_stops_before_finishing(self):
        payload=network().to_json_document().data;payload['extensions']['vendor:large']=list(range(3000))
        model=Model.from_json_document(JsonDocument.from_data(payload));before=model.to_json_document().to_bytes()
        original=jm.copy_json;active=[];checks=[];completed=[];error=OSError('stop preserved tree')
        def copy(value):
            enabled=type(value) is dict and 'collections' in value and 'extensions' in value
            if enabled:active.append(True)
            try:
                result=original(value)
                if enabled:completed.append(True)
                return result
            finally:
                if enabled:active.pop()
        def stop():
            if active:
                checks.append(1)
                if len(checks)==4:raise error
        with patch.object(jm,'copy_json',copy),self.assertRaises(OSError) as caught:model.to_json_document(checkpoint=stop)
        self.assertIs(caught.exception,error);self.assertFalse(completed)
        self.assertEqual(model.to_json_document().to_bytes(),before)

    def test_shape_scan_and_typed_encoding_do_not_swallow_callback_errors(self):
        model=network();model.timeseries.add(InlineTimeSeries(id='Long',points=tuple(SeriesPoint(time=timedelta(seconds=i),value=1.) for i in range(2000))))
        before=model.to_json_document().to_bytes();original=jt.JsonTypes._check_shape
        active=[];checks=[];completed=[];error=ValueError('stop tuple shape')
        def shape(value,item,rule,path):
            enabled=type(item) is tuple and len(item)==2000
            if enabled:active.append(True)
            try:
                result=original(value,item,rule,path)
                if enabled:completed.append(True)
                return result
            finally:
                if enabled:active.pop()
        def stop():
            if active:
                checks.append(1)
                if len(checks)==3:raise error
        with patch.object(jt.JsonTypes,'_check_shape',shape),self.assertRaises(ValueError) as caught:model.to_json_document(checkpoint=stop)
        self.assertIs(caught.exception,error);self.assertFalse(completed)
        self.assertEqual(model.to_json_document().to_bytes(),before)

    def test_location_scan_and_newline_scan_publish_only_a_complete_index(self):
        document=JsonDocument.from_data({'text':'中文','values':list(range(1000))},source='source.json')
        expected=jd.JsonLocations(document).span(('values',800))
        for phase in ('values','newlines'):
            with self.subTest(phase=phase):
                locations=jd.JsonLocations(document);active=[True] if phase=='values' else []
                calls=[];original=jd.checkpointed;error=ValueError('stop '+phase)
                def checked(values,**kwargs):
                    if phase=='newlines':active.append(True)
                    yield from original(values,**kwargs)
                def stop():
                    if active:
                        calls.append(1)
                        if len(calls)==5:raise error
                with patch.object(jd,'checkpointed',checked),self.assertRaises(ValueError) as caught:
                    with checkpoint_scope(stop):locations.span(('values',800))
                self.assertIs(caught.exception,error);self.assertIsNone(locations.positions)
                self.assertFalse(hasattr(locations,'newlines'))
                self.assertEqual(locations.span(('values',800)),expected)


if __name__=='__main__':unittest.main()
