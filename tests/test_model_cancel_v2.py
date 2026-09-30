"""Initial model capture and validation cooperative work contracts."""
from datetime import timedelta
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from easysewer.model import Model
from easysewer.model.network import Junction
from easysewer.model.resources import InlineTimeSeries, SeriesPoint
from easysewer.model import store as stores, identity
from easysewer.io.inp.resources import ResourcesCodec
from easysewer.io.inp import resources as resources_module
from easysewer.runtime import Runner
from easysewer.validation._cooperative import checkpoint_scope
from test_options_v2 import network
from test_runner_v2 import config


class ModelCheckpointTests(unittest.TestCase):
    def test_normal_reports_json_and_copy_independence(self):
        source=network()
        for model in (source,Model.from_json_document(source.to_json_document())):
            before=model.to_json_document().to_bytes()
            for for_run in (False,True):
                self.assertEqual(model.validate(for_run=for_run),model.validate(for_run=for_run,checkpoint=lambda:None))
            clone=model.copy(checkpoint=lambda:None)
            self.assertEqual(clone.to_json_document().to_bytes(),before)
            self.assertIs(clone.nodes['J'],model.nodes['J'])
            clone.nodes.update('J',elevation=model.nodes['J'].elevation+1)
            self.assertEqual(model.to_json_document().to_bytes(),before)
            for operation in (model.copy,model.validate):
                with self.assertRaises(TypeError):operation(checkpoint=1)

    def test_interrupted_mapping_copy_keeps_source_and_original_error(self):
        model=network()
        for i in range(1000):model.nodes.add(Junction(id='Extra'+str(i),elevation=0))
        before=model.to_json_document().to_bytes()
        original=stores.checkpointed
        for kind in (ValueError,OSError,KeyboardInterrupt):
            with self.subTest(kind=kind):
                seen=[];error=kind('stop copying')
                def checked(values,**kwargs):
                    for item in original(values,**kwargs):
                        if isinstance(item,tuple) and len(item)==2 and isinstance(item[1],Junction):seen.append(item[0])
                        yield item
                def stop():
                    if len(seen)>=300:raise error
                with patch.object(stores,'checkpointed',checked),self.assertRaises(kind) as caught:
                    model.copy(checkpoint=stop)
                self.assertIs(caught.exception,error)
                self.assertGreaterEqual(len(seen),300);self.assertLess(len(seen),800)
                self.assertEqual(model.to_json_document().to_bytes(),before)
        self.assertEqual(model.copy().to_json_document().to_bytes(),before)

    def test_large_single_record_index_and_calendar_validation_interrupt(self):
        model=network()
        model.timeseries.add(InlineTimeSeries(id='Long',points=tuple(SeriesPoint(time=timedelta(seconds=i),value=1.) for i in range(2000))))
        before=model.to_json_document().to_bytes()
        for phase in ('index','calendar'):
            with self.subTest(phase=phase):
                model._store._reference_index=None
                module=identity if phase=='index' else resources_module
                original=module.checkpointed;seen=[];error=ValueError('stop '+phase)
                def checked(values,**kwargs):
                    for item in original(values,**kwargs):
                        if isinstance(item,tuple) and len(item)==2 and isinstance(item[1],SeriesPoint):seen.append(item[0])
                        yield item
                def stop():
                    if len(seen)>=300:raise error
                with patch.object(module,'checkpointed',checked),self.assertRaises(ValueError) as caught:
                    model.validate(checkpoint=stop)
                self.assertIs(caught.exception,error)
                self.assertGreaterEqual(len(seen),300);self.assertLess(len(seen),800)
                if phase=='index':self.assertIsNone(model._store._reference_index)
                self.assertEqual(model.to_json_document().to_bytes(),before)
        self.assertTrue(model.validate().is_valid)

    def test_nested_validation_interruption_rolls_back_transaction_and_context(self):
        model=network();before=model.to_json_document().to_bytes()
        for kind in (ValueError,OSError,KeyboardInterrupt):
            with self.subTest(kind=kind):
                armed=[];cause=LookupError('cause');error=kind('validation interrupted');error.__cause__=cause
                def stop():
                    if armed:raise error
                with self.assertRaises(kind) as caught:
                    with checkpoint_scope(stop):
                        with model.transaction():
                            model.nodes.update('J',elevation=99)
                            armed.append(True)
                            model.validate()
                self.assertIs(caught.exception,error);self.assertIs(error.__cause__,cause)
                self.assertEqual(model.to_json_document().to_bytes(),before)
                self.assertTrue(model.validate().is_valid)

    def test_collection_iterator_snapshots_order_before_first_yield(self):
        model=network();expected=list(model.nodes)
        with checkpoint_scope(lambda:None):
            iterator=iter(model.nodes)
            model.nodes.add(Junction(id='Later',elevation=0))
            self.assertEqual(list(iterator),expected)
        self.assertEqual(list(model.nodes),expected+['Later'])

    def test_runner_pre_cancel_skips_copy_and_legacy_override_keeps_signature(self):
        class Backend:
            def probe(self,**kwargs):raise AssertionError('Backend must not start')
        runner=Runner(backends={'swmm:standard':Backend()})
        model=network();event=threading.Event();event.set()
        with tempfile.TemporaryDirectory() as directory:
            target=Path(directory)/'output'
            with patch.object(Model,'copy',side_effect=AssertionError('copy must not start')):
                result=runner.run(model,config(target),cancel_event=event)
            self.assertEqual(result.status,'cancelled');self.assertEqual(result.failure.stage,'validation')
            self.assertFalse(target.exists())
            event.clear();calls=[]
            class Legacy(Model):
                def copy(self):
                    calls.append(True);value=super().copy();event.set();return value
            legacy=Legacy()
            result=runner.run(legacy,config(target),cancel_event=event)
            self.assertEqual(result.status,'cancelled');self.assertEqual(result.failure.stage,'validation')
            self.assertEqual(calls,[True]);self.assertFalse(target.exists())

    def test_runner_copy_failure_is_captured_before_any_backend_or_outputs(self):
        error=OSError('copy source unavailable')
        class Broken(Model):
            def copy(self):raise error
        with tempfile.TemporaryDirectory() as directory:
            target=Path(directory)/'output'
            result=Runner(backends={}).run(Broken(),config(target))
            self.assertEqual(result.status,'failed');self.assertEqual(result.failure.stage,'validation')
            self.assertIn(str(error),result.failure.message);self.assertFalse(target.exists())


if __name__=='__main__':unittest.main()
