"""Result finalization cancellation: stream ownership, exception identity and complete retry."""
import hashlib,io,json,struct,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from easysewer.io.output_metadata import OutputMetadata
from easysewer.io.hotstart import HotstartLayout,HotstartData,StateObject
from easysewer.io.hotstart_manifest import HotstartManifest
from easysewer.io.runoff_cache import RunoffLayout,RunoffData,RdiiData
from easysewer.io.cache_manifest import CacheManifest
from easysewer.io import _record_work as work
from easysewer.validation._cooperative import checkpoint_scope
from test_output_v2 import binary_output

class ResultFinalizationCancellationTests(unittest.TestCase):
    def interrupt(self,operation,*,at=4,error_type=OSError):
        error=error_type('stop finalization');cause=RuntimeError('original cause');error.__cause__=cause;calls=[]
        def check():
            calls.append(1)
            if len(calls)==at:raise error
        with self.assertRaises(error_type) as caught:operation(check)
        self.assertIs(caught.exception,error);self.assertIs(caught.exception.__cause__,cause);self.assertEqual(len(calls),at)

    def test_metadata_complete_bytes_encoding_and_noop(self):
        for encoding in ('utf-8','cp1252'):
            for periods in (0,1,7):
                raw=binary_output(periods=periods,encoding=encoding,names=(('S',),('Café','J'),('P',),('Q',)))
                plain=OutputMetadata.from_stream(io.BytesIO(raw),encoding=encoding)
                stream=io.BytesIO(raw);active=OutputMetadata.from_stream(stream,encoding=encoding,checkpoint=lambda:None)
                self.assertEqual(active,plain);self.assertFalse(stream.closed)
                with tempfile.TemporaryDirectory() as folder:
                    path=Path(folder)/'out';path.write_bytes(raw)
                    self.assertEqual(OutputMetadata.read(path,encoding=encoding,checkpoint=lambda:None),plain)
                    self.assertEqual(path.read_bytes(),raw)

    def test_metadata_interruptions_leave_caller_stream_open_and_reusable(self):
        raw=binary_output(periods=0,names=((),tuple('N'+str(i) for i in range(3000)),(),()));expected=OutputMetadata.from_stream(io.BytesIO(raw))
        for kind in (ValueError,OSError,KeyboardInterrupt):
            stream=io.BytesIO(raw)
            def during_names(check):
                def entered():
                    if stream.tell()>500:check()
                return OutputMetadata.from_stream(stream,checkpoint=entered)
            self.interrupt(during_names,at=3,error_type=kind)
            self.assertFalse(stream.closed);self.assertGreater(stream.tell(),28);self.assertEqual(stream.getvalue(),raw)
            self.assertEqual(OutputMetadata.from_stream(stream,checkpoint=lambda:None),expected)
            with tempfile.TemporaryDirectory() as folder:
                path=Path(folder)/'out';path.write_bytes(raw)
                self.interrupt(lambda check:OutputMetadata.read(path,checkpoint=check),at=7,error_type=kind)
                self.assertEqual(OutputMetadata.read(path),expected);self.assertEqual(path.read_bytes(),raw)

    def test_legacy_stream_override_and_callback_validation(self):
        calls=[]
        class Legacy(OutputMetadata):
            @classmethod
            def from_stream(cls,stream,*,encoding='utf-8',max_metadata_bytes=64*1024*1024):
                calls.append(True);return super().from_stream(stream,encoding=encoding,max_metadata_bytes=max_metadata_bytes)
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'out';path.write_bytes(binary_output())
            self.assertEqual(Legacy.read(path,checkpoint=lambda:None).groups,OutputMetadata.read(path).groups)
            self.assertEqual(calls,[True])
            with self.assertRaises(TypeError):OutputMetadata.read(path,checkpoint=1)
        with self.assertRaises(TypeError):OutputMetadata.from_stream(io.BytesIO(binary_output()),checkpoint=1)

    def test_metadata_short_read_error_remains_strict_with_callback(self):
        raw=binary_output();meta=OutputMetadata.from_stream(io.BytesIO(raw))
        class Short(io.BytesIO):
            def read(self,count=-1):return super().read(count-1 if self.tell()==self.bad else count)
        for offset in (len(raw)-24,0,meta.output_offset,meta.output_offset+(meta.periods-1)*meta.period_bytes):
            stream=Short(raw);stream.bad=offset
            with self.assertRaisesRegex(ValueError,'Truncated OUT'):OutputMetadata.from_stream(stream,checkpoint=lambda:None)
            self.assertFalse(stream.closed)

    def test_private_chunked_read_hash_identity_and_retry(self):
        raw=b'abcdef0123456789'*65536
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'data';path.write_bytes(raw)
            for operation in (lambda:work.read_record_bytes(path),lambda:work.record_digest(raw)):
                def checked(check):
                    with checkpoint_scope(check):return operation()
                self.interrupt(checked,at=4,error_type=KeyboardInterrupt)
            with checkpoint_scope(lambda:None):
                self.assertEqual(work.read_record_bytes(path),raw);self.assertEqual(work.record_digest(raw),hashlib.sha256(raw).hexdigest())
            self.assertEqual(path.read_bytes(),raw)

    def test_one_large_cache_frame_and_hotstart_rows_interrupt_inside_decoder(self):
        count=3000;runoff=RunoffLayout(flow_units='CFS',subcatchments=tuple('S'+str(i) for i in range(count)),pollutants=())
        runoff_raw=b'SWMM5-RUNOFF'+struct.pack('<4i',count,0,0,1)+struct.pack('<f',60.)+b'\x00'*(count*32)
        hotstart=HotstartLayout(flow_units='CFS',nodes=tuple(StateObject(id='N'+str(i),kind='JUNCTION') for i in range(count)),links=(),subcatchments=(),pollutants=(),landuses=())
        hotstart_raw=b'SWMM5-HOTSTART4'+struct.pack('<6i',0,0,count,0,0,0)+b'\x00'*(count*8)
        rdii_raw=b'SWMM5-RDII'+struct.pack('<2i',60,count)+struct.pack('<'+'i'*count,*range(count))+struct.pack('<d',43831.)+b'\x00'*(count*4)
        for kind,raw,layout in ((RunoffData,runoff_raw,runoff),(HotstartData,hotstart_raw,hotstart),(RdiiData,rdii_raw,None)):
            options={'layout':layout} if layout is not None else {};expected=kind.from_bytes(raw,**options)
            def operation(check):
                with checkpoint_scope(check):return kind.from_bytes(raw,**options)
            self.interrupt(operation,at=5,error_type=ValueError)
            with checkpoint_scope(lambda:None):self.assertEqual(kind.from_bytes(raw,**options),expected)
            self.assertEqual(expected.to_bytes(),raw)

    def test_manifest_serialization_and_verification_keep_complete_wire_bytes(self):
        count=3000;layout=RunoffLayout(flow_units='CFS',subcatchments=tuple('S'+str(i) for i in range(count)),pollutants=())
        raw=b'SWMM5-RUNOFF'+struct.pack('<4i',count,0,0,1)+struct.pack('<f',60.)+b'\x00'*(count*32)
        manifest=CacheManifest.asserted(raw,layout=layout);expected=manifest.to_bytes();decoded=manifest.verify(raw,layout=layout)
        for operation in (lambda:manifest.to_bytes(),lambda:manifest.verify(raw,layout=layout)):
            def checked(check):
                with checkpoint_scope(check):return operation()
            self.interrupt(checked,at=5,error_type=OSError)
        with checkpoint_scope(lambda:None):
            self.assertEqual(manifest.to_bytes(),expected);self.assertEqual(manifest.verify(raw,layout=layout),decoded)
        self.assertEqual(CacheManifest.from_bytes(expected),manifest)
        with checkpoint_scope(lambda:None),self.assertRaises(ValueError):manifest.verify(raw+b'changed',layout=layout)

if __name__=='__main__':unittest.main()