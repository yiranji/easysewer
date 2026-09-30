"""Independent binary fixtures for ownership, identities and result contracts."""

from dataclasses import replace
from datetime import datetime, timedelta
import hashlib
import io
from pathlib import Path
import struct
import tempfile
import tracemalloc
import unittest
from unittest.mock import patch

from easysewer.io.output import OutputChangedError, OutputReader
from easysewer.io.output_metadata import NotRecordedError, OutputMetadata
from easysewer.io.json import JsonDocument
from easysewer.model import Ref
from easysewer.results import OutputVariable, ResultSeries, swmm_output_variables


def binary_output(*, periods=7, flow_units=0, extra=False, pump=False, names=None, encoding='utf-8'):
    names=names or (('S',),('Earlier','J'),('P',),('Count','Micro','Mass'))
    ns,nn,nl,np=map(len,names)
    data=bytearray(struct.pack('<7i',516114522,52004,flow_units,ns,nn,nl,np))
    for group in names:
        for name in group:
            raw=name.encode(encoding);data+=struct.pack('<i',len(raw))+raw
    data+=struct.pack('<'+'i'*np,*[2,1,0][:np]);input_pos=len(data)
    for count,codes in ((ns,(1,)),(nn,(0,2,3)),(nl,(0,4,4,3,5))):
        data+=struct.pack('<i',len(codes))+struct.pack('<'+'i'*len(codes),*codes)
        for _ in range(count):
            for code in codes:data+=struct.pack('<i' if code==0 else '<f',int(pump) if code==0 else 1.)
    variables=(tuple(range(8+np)),tuple(range(6+np)),tuple(range(5+np)),tuple(range(15))+((99,) if extra else ()))
    for codes in variables:data+=struct.pack('<i',len(codes))+struct.pack('<'+'i'*len(codes),*codes)
    data+=struct.pack('<di',43831.,60);output_pos=len(data)
    for period in range(periods):
        data+=struct.pack('<d',43831.+(period+1)/1440)
        for category,(count,codes) in enumerate(zip((ns,nn,nl,1),variables)):
            for obj in range(count):
                for code in codes:data+=struct.pack('<f',(period+1)*1000+category*100+obj*10+code)
    data+=struct.pack('<6i',28,input_pos,output_pos,periods,0,516114522)
    return bytes(data)


class OutputReaderTests(unittest.TestCase):
    def test_metadata_short_reads_and_nonfinite_input_properties(self):
        raw=binary_output()
        metadata=OutputMetadata.from_stream(io.BytesIO(raw))
        class ShortRead(io.BytesIO):
            def read(self,count=-1):
                if self.tell()==self.short_at:
                    return super().read(max(0,count-1))
                return super().read(count)
        for offset in (len(raw)-24,0,metadata.output_offset,
                       metadata.output_offset+(metadata.periods-1)*metadata.period_bytes):
            with self.subTest(offset=offset):
                stream=ShortRead(raw);stream.short_at=offset
                with self.assertRaisesRegex(ValueError,'Truncated OUT'):
                    OutputMetadata.from_stream(stream)
                self.assertFalse(stream.closed)
        input_pos=struct.unpack_from('<i',raw,len(raw)-20)[0]
        for value in (float('nan'),float('inf'),-float('inf')):
            damaged=bytearray(raw)
            struct.pack_into('<f',damaged,input_pos+8,value)
            with self.assertRaisesRegex(ValueError,'Nonfinite OUT input property'):
                OutputMetadata.from_stream(io.BytesIO(damaged))

    def write(self,root,**options):
        path=Path(root)/'model.out';path.write_bytes(binary_output(**options));return path

    def test_ids_every_variable_units_and_pollutant_dimension(self):
        with tempfile.TemporaryDirectory() as directory:
            path=self.write(directory)
            with OutputReader(path) as reader:
                self.assertEqual(reader.metadata.index(Ref(collection='swmm:nodes',key='j')),1)
                result=reader.series(Ref(collection='swmm:nodes',key='j'),'swmm:depth',start=1,stop=4)
                self.assertEqual(result.target.key,'J')
                self.assertEqual(result.periods,(1,2,3));self.assertEqual(result.values,(2110.,3110.,4110.))
                self.assertEqual(result.unit,'ft');self.assertEqual(result.times[0],datetime(2020,1,1,0,2))
                self.assertEqual(result.sampling,'producer-unspecified')
                for category,collection in enumerate(('subcatchments','nodes','links','system')):
                    target=None if collection=='system' else Ref(collection='swmm:'+collection,key=reader.metadata.names('swmm:'+collection)[0])
                    for variable in reader.available_variables('swmm:'+collection):
                        if variable.pollutant:
                            for p,(name,unit) in enumerate((('Count','#/L'),('Micro','UG/L'),('Mass','MG/L'))):
                                series=reader.series(target,variable.key,pollutant=Ref(collection='swmm:pollutants',key=name))
                                self.assertEqual(series.unit,unit)
                                self.assertEqual(series.values[0],1000+category*100+variable.code+p)
                        else:
                            series=reader.series(target,variable.key)
                            self.assertEqual(series.values[-1],7000+category*100+variable.code)
                            self.assertIsNotNone(series.unit)
                with self.assertRaises(NotRecordedError):reader.series(Ref(collection='swmm:nodes',key='Absent'),'swmm:depth')
                with self.assertRaises(TypeError):reader.series(Ref(collection='swmm:nodes',key='J'),'swmm:concentration')
                with self.assertRaises(ValueError):reader.series(None,'swmm:inflow',pollutant=Ref(collection='swmm:pollutants',key='Mass'))
            self.assertTrue(reader.closed);reader.close();path.unlink()

    def test_six_unit_families_and_actual_input_properties(self):
        with tempfile.TemporaryDirectory() as directory:
            for units in range(6):
                path=self.write(directory,flow_units=units)
                with OutputReader(path) as reader:
                    node=Ref(collection='swmm:nodes',key='J')
                    self.assertEqual(reader.series(node,'swmm:volume').unit,'ft3' if units<3 else 'm3')
                    self.assertEqual(reader.series(None,'swmm:temperature').unit,'F' if units<3 else 'C')
                    self.assertEqual(reader.series(None,'swmm:evaporation').unit,'in/day' if units<3 else 'mm/day')
                    self.assertEqual(reader.series(None,'swmm:inflow').unit,('CFS','GPM','MGD','CMS','LPS','MLD')[units])
                    links=next(row for row in reader.metadata.input_properties if row[0]=='swmm:links')
                    self.assertEqual(links[1],(0,4,4,3,5));self.assertEqual(links[2],((0,1.,1.,1.,1.),))

    def test_streaming_limits_sparse_reads_interleaving_and_empty_ranges(self):
        with tempfile.TemporaryDirectory() as directory:
            path=self.write(directory,periods=25)
            with OutputReader(path,max_buffer_bytes=12,max_series_values=4) as reader:
                with self.assertRaises(ValueError):reader.series(None,'swmm:inflow')
                first=reader.iter_chunks(None,'swmm:inflow',chunk_size=4)
                second=reader.iter_chunks(None,'swmm:storage',chunk_size=3)
                a=next(first);b=next(second)
                self.assertEqual(a.periods,(0,1,2,3));self.assertEqual(b.periods,(0,1,2))
                rest=list(first)
                self.assertEqual(sum(len(item.values) for item in (a,*rest)),25)
                self.assertEqual(next(second).periods,(3,4,5))
                empty=reader.series(None,'swmm:inflow',start=25,stop=25)
                self.assertEqual(empty.maximum().reason,'no_observations')
                with self.assertRaises(ValueError):reader.series(None,'swmm:inflow',start=-1)
            with self.assertRaises(ValueError):next(second)
            self.write(directory,periods=0)
            with OutputReader(path) as reader:self.assertEqual(reader.series(None,'swmm:inflow').values,())

    def test_captured_identity_digest_and_changes_close_handles(self):
        with tempfile.TemporaryDirectory() as directory:
            path=self.write(directory);raw=path.read_bytes();sha=hashlib.sha256(raw).hexdigest()
            metadata=OutputMetadata.read(path)
            with self.assertRaises(OutputChangedError):OutputReader(path,expected_sha256='0'*64)
            with self.assertRaises(OutputChangedError):OutputReader(path,metadata=replace(metadata,periods=1))
            reader=OutputReader(path,metadata=metadata,expected_sha256=sha,expected_size=len(raw),run_id='run')
            self.assertEqual(reader.series(None,'swmm:inflow').source.sha256,sha)
            with path.open('ab') as stream:stream.write(b'x')
            with self.assertRaises(OutputChangedError):reader.series(None,'swmm:inflow')
            self.assertTrue(reader.closed);path.unlink()
            path.write_bytes(b'broken')
            with self.assertRaises(ValueError):OutputReader(path)
            path.unlink()

    def test_partial_io_failure_and_context_exception_close_once(self):
        with tempfile.TemporaryDirectory() as directory:
            path=self.write(directory);reader=OutputReader(path)
            with patch.object(reader,'_read',side_effect=OSError('read failed')):
                with self.assertRaises(OSError):reader.series(None,'swmm:storage')
            self.assertTrue(reader.closed)
            with self.assertRaisesRegex(RuntimeError,'caller failed'):
                with OutputReader(path) as reader:
                    reader.series(None,'swmm:storage');raise RuntimeError('caller failed')
            self.assertTrue(reader.closed);reader.close();path.unlink()

    def test_nonfinite_values_are_explicit_and_middle_time_corruption_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            path=self.write(directory);metadata=OutputMetadata.read(path);raw=bytearray(path.read_bytes())
            # Last system variable is potential evaporation, independently at the end of each row.
            struct.pack_into('<f',raw,metadata.output_offset+metadata.period_bytes-4,float('nan'));path.write_bytes(raw)
            with OutputReader(path,invalid_values='preserve') as reader:
                series=reader.series(None,'swmm:potential_evaporation')
                self.assertIsNone(series.values[0]);self.assertEqual(series.missing[0],'nonfinite_source_value')
                self.assertEqual(series.maximum().missing_count,1)
                self.assertEqual(ResultSeries.from_json_document(series.to_json_document()),series)
            reader=OutputReader(path)
            with self.assertRaisesRegex(ValueError,'Nonfinite'):reader.series(None,'swmm:potential_evaporation')
            self.assertTrue(reader.closed)
            path=self.write(directory);raw=bytearray(path.read_bytes())
            struct.pack_into('<d',raw,metadata.output_offset+2*metadata.period_bytes,43831.)
            path.write_bytes(raw);reader=OutputReader(path)
            with self.assertRaisesRegex(ValueError,'increase'):list(reader.iter_chunks(None,'swmm:inflow',chunk_size=2))
            self.assertTrue(reader.closed)

    def test_extension_unknown_values_and_json_do_not_change_existing_queries(self):
        with tempfile.TemporaryDirectory() as directory:
            path=self.write(directory,extra=True)
            extension=OutputVariable(collection='swmm:system',key='example:energy',code=99,dimension='energy',
                description='Extension fixture',units=(('US','kWh'),('SI','kWh')),semantics='example:reported-energy')
            registry=swmm_output_variables().with_variables(extension)
            with OutputReader(path) as old,OutputReader(path,variables=registry) as new:
                self.assertEqual(old.series(None,'swmm:storage'),new.series(None,'swmm:storage'))
                opaque=old.series(None,'swmm:unknown-99');self.assertIsNone(opaque.unit)
                self.assertEqual(opaque.semantics,'unknown')
                series=new.series(None,'example:energy')
                self.assertEqual(series.values,opaque.values);self.assertEqual(series.unit,'kWh')
                self.assertEqual(ResultSeries.from_json_document(series.to_json_document()),series)
            with self.assertRaises(ValueError):registry.with_variables(extension)
            with self.assertRaises(ValueError):registry.with_variables(replace(extension,key='swmm:unknown-99',code=100))
            collision=OutputVariable(collection='swmm:nodes',key='example:collision',code=7,dimension='flow',description='Collision')
            with self.assertRaises(ValueError):OutputReader(path,variables=registry.with_variables(collision))

    def test_averaging_provenance_capacity_setting_and_sampled_maximum(self):
        with tempfile.TemporaryDirectory() as directory:
            for pump in (False,True):
                path=self.write(directory,pump=pump)
                metadata=replace(OutputMetadata.read(path),averages=True,producer='swmm:standard')
                with OutputReader(path,metadata=metadata) as reader:
                    series=reader.series(Ref(collection='swmm:links',key='P'),'swmm:capacity')
                    self.assertEqual(series.sampling,'last-routing-step-setting' if pump else 'routing-step-arithmetic-mean')
                    self.assertEqual(reader.series(None,'swmm:storage').sampling,'routing-state-at-report')
                    self.assertEqual(reader.series(Ref(collection='swmm:nodes',key='J'),'swmm:depth').sampling,'routing-step-arithmetic-mean')
                    peak=series.maximum();self.assertEqual(peak.statistic,'easysewer:saved-observation-maximum')
                    self.assertEqual(peak.value,series.values[-1]);self.assertEqual(peak.time,series.times[-1])
                    data=series.to_json_document().data;data['values'][0]=None
                    with self.assertRaises(ValueError):ResultSeries.from_json_document(JsonDocument.from_data(data))

    def test_large_result_stream_has_bounded_reads_and_no_retained_chunks(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'large.out';raw=binary_output(periods=1)
            stream=io.BytesIO(raw);metadata=OutputMetadata.from_stream(stream)
            self.assertFalse(stream.closed)
            row=raw[metadata.output_offset:metadata.output_offset+metadata.period_bytes]
            footer=list(struct.unpack('<6i',raw[-24:]));footer[3]=50000
            with path.open('wb') as output:
                output.write(raw[:metadata.output_offset])
                for period in range(footer[3]):
                    output.write(struct.pack('<d',43831.+(period+1)/1440));output.write(row[8:])
                output.write(struct.pack('<6i',*footer))
            self.assertGreater(path.stat().st_size,10*1024*1024)
            tracemalloc.start()
            try:
                with OutputReader(path,max_buffer_bytes=4096,max_series_values=256) as reader:
                    original=reader._read;largest=0
                    def bounded(offset,count):
                        nonlocal largest
                        largest=max(largest,count);return original(offset,count)
                    reader._read=bounded;count=0
                    for chunk in reader.iter_chunks(None,'swmm:storage',chunk_size=256):count+=len(chunk.values)
                    _,peak=tracemalloc.get_traced_memory()
                    self.assertEqual(count,50000);self.assertLessEqual(largest,4096)
                    self.assertLess(peak,4*1024*1024)
            finally:tracemalloc.stop()

    def test_explicit_legacy_identifier_encoding_and_unicode_filesystem_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'结果😀.out'
            path.write_bytes(binary_output(names=(('S',),('Café',),('P',),('Count','Micro','Mass')),encoding='cp1252'))
            with self.assertRaises(UnicodeDecodeError):OutputReader(path)
            with OutputReader(path,encoding='cp1252') as reader:
                row=reader.series(Ref(collection='swmm:nodes',key='café'),'swmm:depth')
                self.assertEqual(row.target.key,'Café');self.assertEqual(row.source.encoding,'cp1252')
                self.assertEqual(row.source.path,str(path.resolve()))
                self.assertEqual(ResultSeries.from_json_document(row.to_json_document()),row)
                with OutputReader(path,metadata=reader.metadata) as captured:
                    self.assertEqual(captured.series(row.target,row.variable),row)


if __name__=='__main__':unittest.main()
