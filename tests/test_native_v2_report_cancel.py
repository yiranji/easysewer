"""Actual native completion followed by interrupted report scanning/table work."""
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from easysewer import get_native_capabilities
from easysewer.io import report_document as rd
from easysewer.io import _report_tables as rt
from easysewer.runtime import Runner, ReportReadOptions, RunResult
from easysewer.runtime import runner as runner_module
from test_options_v2 import network
from test_runner_v2 import config
from test_report_cancel_v2 import large_depth


EVIDENCE=[]


@unittest.skipUnless(get_native_capabilities()['swmm_solver'],'Native solver unavailable')
class NativeReportCancellationTests(unittest.TestCase):
    def test_cancel_and_deadline_during_report_work_keep_previous_outputs(self):
        raw=large_depth(3000)
        for backend in ('swmm:standard','easysewer:flexible-ponding'):
            for stage in ('scan','rows'):
                for status in ('cancelled','timed_out'):
                    for keep in (False,True):
                        with self.subTest(backend=backend,stage=stage,status=status,keep=keep),tempfile.TemporaryDirectory() as folder:
                            root=Path(folder);target=root/'out';target.mkdir();event=threading.Event()
                            for name in ('model.inp','model.rpt','model.out'):(target/name).write_bytes(b'previous')
                            control=[];armed=[];seen=[];trigger=[]
                            init=runner_module._Cancellation.__init__;read=rd.ReportDocument.read
                            lines=rd._lines;row=rt.RowReader.__init__
                            def capture(value,*a,**k):init(value,*a,**k);control.append(value)
                            def mark():
                                seen.append(1)
                                if len(seen)==20:
                                    trigger.append(time.monotonic())
                                    if status=='cancelled':event.set()
                                    else:control[-1].deadline=time.monotonic()-1
                            def scanning(value):
                                for part in lines(value):
                                    if armed and stage=='scan':mark()
                                    yield part
                            def reading(path,**options):
                                Path(path).write_bytes(raw);armed.append(True)
                                try:return read(path,**options)
                                finally:armed.clear()
                            def rows(value,*a,**k):
                                if stage=='rows':mark()
                                row(value,*a,**k)
                            model=network()
                            if backend=='easysewer:flexible-ponding':model.update_options(flow_routing='DYNWAVE',allow_ponding=True)
                            with patch.object(runner_module._Cancellation,'__init__',capture),patch.object(rd.ReportDocument,'read',reading),\
                                 patch.object(rd,'_lines',scanning),patch.object(rt.RowReader,'__init__',rows):
                                result=Runner().run(model,config(target,backend=backend,overwrite=True,
                                    keep_failed_artifacts=keep,report_read=ReportReadOptions(tables=('swmm:node_depth',))),cancel_event=event)
                            returned=time.monotonic()
                            self.assertEqual(result.status,status,result.failure)
                            self.assertEqual(result.failure.stage,'report_read');self.assertTrue(result.native_completed)
                            self.assertTrue(trigger);self.assertLess(len(seen),600)
                            for name in ('model.inp','model.rpt','model.out'):self.assertEqual((target/name).read_bytes(),b'previous')
                            self.assertFalse(list(target.glob('.easysewer-lock-*')))
                            self.assertEqual(result.retained_directory is not None,keep)
                            self.assertTrue(result.failure_report.truncated)
                            self.assertEqual(result.failure_report.document.raw,raw[:65536])
                            if keep:
                                artifact,=(v for v in result.artifacts if v.role=='run:report')
                                self.assertEqual(artifact.read_bytes(),raw)
                            else:self.assertFalse(result.artifacts)
                            result.save(root/'saved');loaded=RunResult.load(root/'saved')
                            self.assertEqual(loaded.failure_report,result.failure_report)
                            self.assertEqual(loaded.failure,result.failure)
                            EVIDENCE.append(dict(backend=backend,phase=stage,status=status,retained=keep,
                                rows_or_lines_before_return=len(seen),return_seconds=returned-trigger[0],
                                report_bytes=len(raw),failure_prefix_bytes=65536,old_outputs_preserved=True))


if __name__=='__main__':unittest.main()
