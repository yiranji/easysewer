"""Model JSON capture interruption before inventory, with real backend probing."""
from datetime import timedelta
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from easysewer import get_native_capabilities
from easysewer.model import Model
from easysewer.model.resources import InlineTimeSeries,SeriesPoint
from easysewer.io.json import JsonDocument
from easysewer.io.json import model as jm, types as jt
from easysewer.runtime import Runner,RunResult
from easysewer.runtime import runner as rm
from test_json_v2 import entry
from test_options_v2 import network
from test_runner_v2 import config

EVIDENCE=[]

@unittest.skipUnless(get_native_capabilities()['swmm_solver'] and get_native_capabilities()['flexible_ponding'],'Native backends unavailable')
class NativeModelJsonCancellationTests(unittest.TestCase):
    def test_capture_copy_shape_merge_and_comparison_cancel_or_timeout(self):
        for backend in ('swmm:standard','easysewer:flexible-ponding'):
            for phase in ('copy','shape','merge','equal'):
                for status in ('cancelled','timed_out'):
                    for keep in (False,True):
                        with self.subTest(backend=backend,phase=phase,status=status,keep=keep),tempfile.TemporaryDirectory() as folder:
                            root=Path(folder);target=root/'out';target.mkdir()
                            for name in ('model.inp','model.rpt','model.out'):(target/name).write_bytes(b'previous')
                            model=network()
                            if backend=='easysewer:flexible-ponding':model.update_options(flow_routing='DYNWAVE',allow_ponding=True)
                            if phase=='shape':
                                model.timeseries.add(InlineTimeSeries(id='Long',points=tuple(SeriesPoint(time=timedelta(seconds=i),value=1.) for i in range(2000))))
                            else:
                                data=model.to_json_document().data
                                if phase=='merge':entry(data,'swmm:nodes','J')['value']['future']={'values':list(range(3000))}
                                else:data['extensions']['research:future']={'values':list(range(3000))}
                                model=Model.from_json_document(JsonDocument.from_data(data))
                                if phase=='merge':model.nodes.update('J',elevation=model.nodes['J'].elevation+1)
                            before=model.to_json_document().to_bytes()
                            serializing=[];active=[];checks=[];triggered=[];event=threading.Event()
                            check_original=rm._Cancellation.check;serialize_original=Model.to_json_document
                            module,name=(jt.JsonTypes,'_check_shape') if phase=='shape' else (jm,{'copy':'copy_json','merge':'_merge_extras','equal':'equal_json'}[phase])
                            original=getattr(module,name)
                            # Deliberately retains the pre-checkpoint method signature.
                            def serialize(value):
                                serializing.append(True)
                                try:return serialize_original(value)
                                finally:serializing.pop()
                            def entered(*args,**kwargs):
                                enabled=bool(serializing)
                                if phase in ('copy','equal'):enabled=enabled and type(args[0]) is dict and 'collections' in args[0] and 'extensions' in args[0]
                                if phase=='shape':enabled=enabled and type(args[1]) is tuple and len(args[1])==2000
                                if enabled:active.append(True)
                                try:return original(*args,**kwargs)
                                finally:
                                    if enabled:active.pop()
                            def check(control):
                                if active:
                                    checks.append(1)
                                    if len(checks)==3:
                                        triggered.append(time.monotonic())
                                        if status=='cancelled':event.set()
                                        else:control.deadline=time.monotonic()-1
                                return check_original(control)
                            with patch.object(Model,'to_json_document',serialize),patch.object(module,name,entered),patch.object(rm._Cancellation,'check',check):
                                result=Runner().run(model,config(target,backend=backend,overwrite=True,keep_failed_artifacts=keep),cancel_event=event)
                            returned=time.monotonic()
                            self.assertEqual(result.status,status,result.failure);self.assertEqual(result.failure.stage,'capability')
                            self.assertFalse(result.native_completed);self.assertEqual(len(checks),3)
                            self.assertEqual(model.to_json_document().to_bytes(),before)
                            for name in ('model.inp','model.rpt','model.out'):self.assertEqual((target/name).read_bytes(),b'previous')
                            self.assertFalse(list(target.glob('.easysewer-*')));self.assertIsNone(result.retained_directory)
                            result.save(root/'saved');restored=RunResult.load(root/'saved')
                            self.assertEqual(restored.status,result.status);self.assertEqual(restored.failure,result.failure)
                            EVIDENCE.append(dict(backend=backend,phase=phase,status=status,keep=keep,checks=3,model_unchanged=True,old_outputs_preserved=True,stage=result.failure.stage,native_completed=False,return_seconds=returned-triggered[0]))

if __name__=='__main__':unittest.main()
