"""Model capture/preflight interruptions preserve caller data and owned outputs."""
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from easysewer import get_native_capabilities
from easysewer.model import Model
from easysewer.runtime import Runner, RunResult
from easysewer.runtime import runner as rm
from test_options_v2 import network
from test_runner_v2 import config

EVIDENCE=[]

@unittest.skipUnless(get_native_capabilities()['swmm_solver'],'Native solver unavailable')
class NativeModelCancellationTests(unittest.TestCase):
    def test_capture_initial_validation_and_preflight_interruptions(self):
        for backend in ('swmm:standard','easysewer:flexible-ponding'):
            for phase in ('copy','validation','preflight'):
                for status in ('cancelled','timed_out'):
                    for keep in (False,True):
                        with self.subTest(backend=backend,phase=phase,status=status,keep=keep),tempfile.TemporaryDirectory() as folder:
                            root=Path(folder);target=root/'out';target.mkdir()
                            for filename in ('model.inp','model.rpt','model.out'):(target/filename).write_bytes(b'previous')
                            model=network()
                            if backend=='easysewer:flexible-ponding':model.update_options(flow_routing='DYNWAVE',allow_ponding=True)
                            before=model.to_json_document().to_bytes()
                            event=threading.Event();active=[];checks=[];triggered=[]
                            original_check=rm._Cancellation.check;copy=Model.copy;validate=Model.validate
                            def copied(value):
                                if phase=='copy':active.append(True)
                                try:return copy(value)
                                finally:
                                    if phase=='copy':active.pop()
                            def validated(value,*,for_run=False,normalize=False):
                                enabled=(phase=='preflight' and for_run) or (phase=='validation' and not for_run)
                                if enabled:active.append(True)
                                try:return validate(value,for_run=for_run,normalize=normalize)
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
                            with patch.object(Model,'copy',copied),patch.object(Model,'validate',validated),patch.object(rm._Cancellation,'check',check):
                                result=Runner().run(model,config(target,backend=backend,overwrite=True,keep_failed_artifacts=keep),cancel_event=event)
                            returned=time.monotonic()
                            self.assertEqual(result.status,status,result.failure)
                            self.assertEqual(result.failure.stage,'preflight' if phase=='preflight' else 'validation')
                            self.assertFalse(result.native_completed);self.assertEqual(len(checks),3)
                            self.assertEqual(model.to_json_document().to_bytes(),before)
                            for filename in ('model.inp','model.rpt','model.out'):self.assertEqual((target/filename).read_bytes(),b'previous')
                            self.assertFalse(list(target.glob('.easysewer-lock-*')))
                            self.assertEqual(result.retained_directory is not None,keep and phase=='preflight')
                            result.save(root/'saved');loaded=RunResult.load(root/'saved')
                            self.assertEqual(loaded.failure,result.failure);self.assertEqual(loaded.status,result.status)
                            EVIDENCE.append(dict(backend=backend,phase=phase,status=status,keep=keep,retained=result.retained_directory is not None,old_outputs_preserved=True,model_unchanged=True,checks=3,return_seconds=returned-triggered[0]))

if __name__=='__main__':unittest.main()
