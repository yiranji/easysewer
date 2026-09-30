"""Real backend probes with preparation event/deadline interruption and retention."""
from pathlib import Path
import tempfile,threading,time,unittest
from unittest.mock import patch
from easysewer import get_native_capabilities
from easysewer.io.inp import InpDocument
from easysewer.io.inp import semantic
from easysewer.model import Model
from easysewer.model.network import Junction
from easysewer.model.resources import FileTimeSeries
from easysewer.model import FileReference
from easysewer.runtime import Runner,RunResult
from easysewer.runtime import runner as rm,cache_reuse
from test_options_v2 import network
from test_files_v2 import bind
from test_runner_v2 import config
from test_cache_reuse_v2 import model as cache_model
EVIDENCE=[]
PHASES=('inventory','encoding','reparse','cache','write','rebase','input_verify')
@unittest.skipUnless(get_native_capabilities()['swmm_solver'] and get_native_capabilities()['flexible_ponding'],'Both real backends required')
class NativePreparationCancellationTests(unittest.TestCase):
    def test_inventory_encoding_reparse_cache_and_private_write(self):
        for backend in ('swmm:standard','easysewer:flexible-ponding'):
            for phase in PHASES:
                for status in ('cancelled','timed_out'):
                    for keep in (False,True):
                        with self.subTest(backend=backend,phase=phase,status=status,keep=keep),tempfile.TemporaryDirectory() as directory:
                            root=Path(directory);target=root/'out';target.mkdir()
                            for name in ('model.inp','model.rpt','model.out'):(target/name).write_bytes(b'previous')
                            model=cache_model() if phase=='cache' else Model.from_document(network().to_document())
                            if backend=='easysewer:flexible-ponding':model.update_options(flow_routing='DYNWAVE',allow_ponding=True)
                            if phase=='write':
                                for index in range(3000):model.nodes.add(Junction(id='Extra'+str(index),elevation=0))
                            if phase=='cache':bind(model,'RUNOFF','SAVE',target/'history.bin')
                            if phase=='rebase':
                                for i in range(4):
                                    source=root/('external'+str(i)+'.dat');source.write_text('01/01/2020 00:00 0\n01/01/2020 00:01 0\n',encoding='ascii')
                                    model.timeseries.add(FileTimeSeries(id='External'+str(i),file=FileReference(path=str(source))))
                            before=model.to_json_document().to_bytes();event=threading.Event();active=[];checks=[];triggered=[];document_active=[];document_done=[];completed=[]
                            original_render=Model.to_document;original_check=rm._Cancellation.check
                            def render(value,*,normalize=False):
                                document_active.append(True)
                                try:
                                    document=original_render(value,normalize=normalize);document_done.append(True);return document
                                finally:document_active.pop()
                            module,name={'inventory':(rm,'inventory'),'encoding':(semantic,'_rows'),'reparse':(InpDocument,'from_bytes'),'cache':(cache_reuse,'_swmm_context'),'write':(rm,'write_record_bytes'),'rebase':(rm,'stage'),'input_verify':(rm,'input_matches')}[phase]
                            original=getattr(module,name)
                            def entered(*a,**k):
                                enabled=bool(document_active) if phase=='encoding' else bool(document_done) if phase=='reparse' else True
                                if enabled:active.append(True)
                                try:
                                    result=original(*a,**k)
                                    if enabled:completed.append(True)
                                    return result
                                finally:
                                    if enabled:active.pop()
                            def check(control):
                                if active:
                                    checks.append(1)
                                    if len(checks)==3:
                                        triggered.append(time.monotonic())
                                        if status=='cancelled':event.set()
                                        else:control.deadline=time.monotonic()-1
                                return original_check(control)
                            with patch.object(Model,'to_document',render),patch.object(module,name,entered),patch.object(rm._Cancellation,'check',check):
                                result=Runner().run(model,config(target,backend=backend,overwrite=True,keep_failed_artifacts=keep),cancel_event=event)
                            returned=time.monotonic()
                            self.assertEqual(result.status,status,result.failure);self.assertEqual(result.failure.stage,'inventory' if phase=='inventory' else 'capture' if phase=='rebase' else 'open' if phase=='input_verify' else 'preflight')
                            self.assertFalse(result.native_completed);self.assertEqual(len(checks),3);self.assertFalse(completed)
                            self.assertEqual(model.to_json_document().to_bytes(),before)
                            for name in ('model.inp','model.rpt','model.out'):self.assertEqual((target/name).read_bytes(),b'previous')
                            self.assertFalse(list(target.rglob('.easysewer-lock-*')))
                            self.assertEqual(result.retained_directory is not None,keep and phase!='inventory')
                            if phase=='cache':self.assertFalse((target/'history.bin').exists())
                            result.save(root/'saved');loaded=RunResult.load(root/'saved');self.assertEqual(loaded.status,result.status);self.assertEqual(loaded.failure,result.failure)
                            EVIDENCE.append(dict(backend=backend,phase=phase,status=status,keep=keep,checks=3,stage=result.failure.stage,model_unchanged=True,old_outputs_preserved=True,native_completed=False,retained=result.retained_directory is not None,return_seconds=returned-triggered[0]))
if __name__=='__main__':unittest.main()
