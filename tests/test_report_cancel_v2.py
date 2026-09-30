"""Report work interruption preserves exception identity and captured semantics."""
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from easysewer.io import report_document as rd
from easysewer.io import _report_tables as rt
from easysewer.io.report import ReportReader, ReportLayoutError, read_report_tables
from easysewer.io.report_details import read_lid_report
from easysewer.model import Ref
from easysewer.validation._cooperative import checkpoint_scope
from test_report_v2 import DEPTH, STORAGE, DEPTH_HEADER, block
from test_report_details_v2 import NODE, LID


def large_depth(count=2000):
    return block('Node Depth Summary',DEPTH_HEADER,'\n'.join(
        f' N{i} JUNCTION 0.50 1.25 11.25 2 03:04 1.00' for i in range(count))).encode()


class ReportCancellationTests(unittest.TestCase):
    def test_normal_noop_paths_preserve_documents_tables_details_and_lid(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'report.rpt';path.write_bytes(DEPTH.encode())
            lid=Path(folder)/'lid.rpt';lid.write_text(LID,encoding='utf-8')
            document=rd.ReportDocument.from_bytes(DEPTH.encode())
            reader=ReportReader(rd.ReportDocument.from_bytes(NODE.encode()))
            target=Ref(collection='swmm:nodes',key='J')
            operations=[lambda **k:rd.ReportDocument.read(path,**k),
                lambda **k:rd.ReportDocument.from_bytes(DEPTH.encode(),**k),
                lambda **k:rd.ReportCapture.read(path,**k),
                lambda **k:read_report_tables(document,('swmm:node_depth',),**k),
                lambda **k:reader.detail(target,**k),lambda **k:reader.series(target,'swmm:depth',**k),
                lambda **k:read_lid_report(lid,**k)]
            for operation in operations:
                expected=operation();calls=[]
                self.assertEqual(operation(checkpoint=lambda:calls.append(1)),expected)
                self.assertTrue(calls)
                with self.assertRaises(TypeError):operation(checkpoint=1)
            self.assertEqual(ReportReader(document,checkpoint=lambda:None).table('swmm:node_depth'),
                             ReportReader(document).table('swmm:node_depth',checkpoint=lambda:None))

    def test_file_read_checks_chunks_and_closes_on_interrupt(self):
        raw=large_depth(10000);streams=[];calls=[];failure=OSError('stop file read')
        def opened(*a,**k):
            stream=io.BytesIO(raw);streams.append(stream);return stream
        def check():
            calls.append(1)
            if streams and streams[-1].tell()>=65536:raise failure
        with patch.object(Path,'open',opened),self.assertRaises(OSError) as caught:
            rd.ReportDocument.read('virtual.rpt',checkpoint=check)
        self.assertIs(caught.exception,failure);self.assertTrue(streams[0].closed)
        self.assertLess(len(calls),10)

    def test_scanning_and_header_repair_interrupt_with_original_exception(self):
        ordinary=large_depth();repaired=STORAGE.encode().replace(b'\xc2\xb3',b'\xb3')+b' blank\n'*2000
        for stage,raw in (('_messages',ordinary),('_catalog',ordinary),('_volume_headers',repaired)):
            for kind in (ValueError,OSError,ReportLayoutError):
                with self.subTest(stage=stage,kind=kind):
                    active=[];calls=[];failure=kind('stop '+stage);original=getattr(rd,stage)
                    def entered(*a,**k):
                        active.append(True)
                        try:return original(*a,**k)
                        finally:active.pop()
                    def check():
                        if active:
                            calls.append(1)
                            if len(calls)==2:raise failure
                    with patch.object(rd,stage,entered),self.assertRaises(kind) as caught:
                        rd.ReportDocument.from_bytes(raw,profile=rd.SWMM_UTF8_REPORT,checkpoint=check)
                    self.assertIs(caught.exception,failure);self.assertEqual(len(calls),2)
        self.assertEqual(rd.ReportDocument.from_bytes(DEPTH.encode()).text,DEPTH)

    def test_row_interrupt_is_never_translated_to_unsupported_layout(self):
        document=rd.ReportDocument.from_bytes(large_depth());original=rt.RowReader.__init__
        for policy in ('preserve','raise'):
            seen=[];failure=ReportLayoutError('caller interrupted rows')
            def row(value,*a,**k):seen.append(1);original(value,*a,**k)
            def check():
                if len(seen)>=20:raise failure
            with patch.object(rt.RowReader,'__init__',row),self.assertRaises(ReportLayoutError) as caught:
                read_report_tables(document,('swmm:node_depth',),on_error=policy,checkpoint=check)
            self.assertIs(caught.exception,failure);self.assertGreaterEqual(len(seen),20)
            self.assertLessEqual(len(seen),256)

    def test_nested_report_scope_preserves_exception_chain_and_does_not_leak(self):
        cause=RuntimeError('cause')
        try:
            try:raise cause
            except RuntimeError:raise ReportLayoutError('interrupt') from cause
        except ReportLayoutError as error:failure=error
        document=rd.ReportDocument.from_bytes(DEPTH.encode());calls=[]
        def check():
            calls.append(1)
            if len(calls)==3:raise failure
        with self.assertRaises(ReportLayoutError) as caught:
            with checkpoint_scope(check):read_report_tables(document,('swmm:node_depth',))
        self.assertIs(caught.exception,failure);self.assertIs(failure.__cause__,cause)
        self.assertEqual(ReportReader(document).table('swmm:node_depth').status,'present')

    def test_cancelled_parse_is_not_published_as_verified_source(self):
        before=set(rd._VERIFIED_REPORTS);failure=ValueError('stop before source registration')
        original=rd.copy.deepcopy;copied=[]
        def copying(value,*a,**k):
            result=original(value,*a,**k);copied.append(1);return result
        def check():
            if copied:raise failure
        with patch.object(rd.copy,'deepcopy',copying),self.assertRaises(ValueError) as caught:
            rd.ReportDocument.from_bytes(DEPTH.encode(),checkpoint=check)
        self.assertIs(caught.exception,failure)
        self.assertFalse(set(rd._VERIFIED_REPORTS)-before)
        document=rd.ReportDocument.from_bytes(DEPTH.encode());self.assertTrue(rd._matches_source(document))

    def test_detail_and_lid_row_interrupts_are_not_layout_diagnostics(self):
        raw=NODE[:NODE.index('  01/01/2020')]+('  01/01/2020 23:59:59 1.000 0.000 1.000 2.000\n'*2000)
        reader=ReportReader(rd.ReportDocument.from_bytes(raw.encode()))
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'lid.rpt'
            header=LID[:LID.index('01/01/2020')];line=LID.splitlines()[-2]
            path.write_text(header+(line+'\n')*2000,encoding='utf-8')
            for operation in (lambda check:reader.detail(Ref(collection='swmm:nodes',key='J'),checkpoint=check),
                              lambda check:read_lid_report(path,checkpoint=check)):
                seen=[];original=rt.RowReader.__init__;failure=ReportLayoutError('stop observations')
                def row(value,*a,**k):seen.append(1);original(value,*a,**k)
                def check():
                    if len(seen)>=20:raise failure
                with patch.object(rt.RowReader,'__init__',row),self.assertRaises(ReportLayoutError) as caught:
                    operation(check)
                self.assertIs(caught.exception,failure);self.assertLessEqual(len(seen),256)

    def test_failure_capture_public_checkpoint_preserves_file_and_exception(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'failed.rpt';raw=large_depth(5000);path.write_bytes(raw)
            calls=[];failure=OSError('capture interrupted')
            def check():
                calls.append(1)
                if len(calls)==4:raise failure
            with self.assertRaises(OSError) as caught:rd.ReportCapture.read(path,checkpoint=check)
            self.assertIs(caught.exception,failure);self.assertEqual(path.read_bytes(),raw)
            moved=path.with_suffix('.moved');path.rename(moved)
            self.assertEqual(rd.ReportCapture.read(moved,max_bytes=65536).document.raw,raw[:65536])


if __name__=='__main__':unittest.main()
