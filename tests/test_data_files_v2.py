"""External time-series/user-rainfall documents and model-aware file checks."""

from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from easysewer.io.rainfall import RainfallData, RainfallReading
from easysewer.io.timeseries import TimeSeriesData
from easysewer.model import FileReference
from easysewer.model.resources import FileTimeSeries, SeriesPoint
from easysewer.model.hydrology import FileRainfall
from easysewer.runtime import check_files
from easysewer.validation import ValidationError
from test_hydrology_v2 import hydrology_model
from test_options_v2 import network


class TimeSeriesDataTests(unittest.TestCase):
    def test_elapsed_calendar_continuation_comments_and_byte_preservation(self):
        for raw in (b'; description\r\n0 1\r\n52:20 2\n',
                    b'Jan-01-2020 00:00 1\n  12:00 2\n01/02/2020 00:00:01 3\n'):
            data=TimeSeriesData.from_bytes(raw)
            self.assertEqual(data.to_bytes(),raw)
            self.assertEqual(TimeSeriesData.from_bytes(data.to_bytes(normalize=True)),data)
        calendar=TimeSeriesData.from_bytes(b'01/01/2020 25:00 1\n26:00 2\n')
        self.assertEqual(calendar.points[-1].time,datetime(2020,1,2,2))
        self.assertFalse(calendar.report.diagnostics)

    def test_mixed_anchor_explicit_resolution_and_inline_materialization(self):
        raw=b'0 1\n01/01/2020 12:05 2\n'
        data=TimeSeriesData.from_bytes(raw)
        self.assertFalse(data.report.diagnostics)
        fixed=data.resolved(datetime(2020,1,1,12))
        self.assertFalse(fixed.report.diagnostics)
        self.assertEqual(fixed.points[0].time,datetime(2020,1,1,12))
        self.assertEqual(data.to_bytes(),raw)
        self.assertEqual(fixed.as_inline('Rain').id,'Rain')
        with self.assertRaises(ValidationError):data.resolved(datetime(2020,1,2))

    def test_source_diagnostics_and_editing_numeric_values(self):
        raw=b'01/01/2020 00:00:01.9 1 ignored\n00:01 2\n'
        data=TimeSeriesData.from_bytes(raw,source='input.dat')
        self.assertEqual(data.points[0].time.second,1)
        self.assertEqual({d.code for d in data.report.diagnostics},{'timeseries.native_time_coercion','timeseries.ignored_columns'})
        self.assertTrue(all(d.span.source=='input.dat' for d in data.report.diagnostics))
        changed=replace(data,points=tuple(replace(p,value=p.value*2) for p in data.points))
        self.assertEqual(TimeSeriesData.from_bytes(changed.to_bytes()).points[0].value,2)

    def test_invalid_data_encoding_and_native_row_boundaries(self):
        for raw in (b'',b';only comment\n',b'0 nan\n',b'0 1\n0 2\n',b'02/30/2020 0 1\n',
                    b'0 1 ;inline comments alter the native field count\n',b'0 1\r1 2',
                    b'0\xc2\xa01\n',b'\xef\xbb\xbf0 1\n',b';'+b'x'*1022+b'\n0 1\n',b'0 1\x1a\n',
                    b'01/01/20_20 0 1', '01/01/２０２０ 0 1'.encode(),
                    b'2147483648:00 1', b'1:60 1', b'1:00:60 1'):
            with self.subTest(raw=raw[:30]):
                with self.assertRaises((ValidationError,ValueError)):TimeSeriesData.from_bytes(raw)
        with self.assertRaises(TypeError):TimeSeriesData.from_bytes(bytearray(b'0 1\n'))
        with self.assertRaises(ValueError):TimeSeriesData.from_bytes(b'0\x00 1\x00',encoding='utf-16-le')
        with self.assertRaises(ValueError):TimeSeriesData(points=(SeriesPoint(time=timedelta(),value=float('inf')),))

    def test_long_comments_fields_and_complete_eof_preserve_and_normalize(self):
        for raw in (b';'+b'x'*1021+b'\n0 1', b'0 '+b'0'*900+b'.1',
                    b'0'*900+b':00 1', b'01/01/'+b'0'*900+b'2020 0 1'):
            for ending in (b'',b'\n',b'\r\n'):
                with self.subTest(prefix=raw[:20],ending=ending):
                    data=TimeSeriesData.from_bytes(raw+ending)
                    self.assertEqual(data.to_bytes(),raw+ending)
                    self.assertEqual(TimeSeriesData.from_bytes(data.to_bytes(normalize=True)),data)
                    self.assertFalse(data.report.diagnostics)

    def test_execution_capability_is_explicit_without_loading_native_code(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'data.dat';path.write_bytes(b'0 0\n01/01/2020 1 2')
            model=network();model.timeseries.add(FileTimeSeries(id='Data',file=FileReference(path=str(path))))
            for capabilities,status in ((None,'pending'),((),'unsupported'),(('easysewer:timeseries-io:1',),'validated')):
                checked=check_files(model,backend_capabilities=capabilities)
                self.assertEqual(checked.checks[0].inspection.status,status)
                self.assertEqual(checked.checks[0].inspection.required_capabilities,('easysewer:timeseries-io:1',))
                self.assertEqual(checked.complete,status=='validated')

    def test_explicit_preflight_reads_data_and_applies_rainfall_consumer_rules(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'data.dat';path.write_bytes(b'0 1\n.5 2\n')
            m=hydrology_model();m.timeseries.replace('Rain',FileTimeSeries(id='Rain',file=FileReference(path=str(path))))
            checked=check_files(m)
            self.assertIn('rainfall.interval_exceeds_series',{d.code for d in checked.report.errors})
            path.write_bytes(b'0 -1\n1 2\n')
            self.assertIn('rainfall.negative_series',{d.code for d in check_files(m).report.errors})
            unused=m.copy();unused.subcatchments.remove('S')
            self.assertTrue(check_files(unused,backend_capabilities=('easysewer:timeseries-io:1',)).complete)
            path.write_bytes(b'0 0\n1 2\n')
            self.assertTrue(check_files(m,backend_capabilities=('easysewer:timeseries-io:1',)).complete)
            from easysewer.model import Model
            from easysewer.io.inp.resources import ResourcesCodec
            from easysewer.schema.structured import ModelSchema
            schema=ModelSchema();codec=ResourcesCodec();schema.register(codec.descriptor,codec)
            resources_only=Model(schema=schema)
            resources_only.timeseries.add(FileTimeSeries(id='Data',file=FileReference(path=str(path))))
            self.assertTrue(check_files(resources_only,backend_capabilities=('easysewer:timeseries-io:1',)).complete)
            data=TimeSeriesData.read(path)
            m.timeseries.replace('Rain',data.as_inline('Rain'))
            m.convert_units('CMS')
            self.assertAlmostEqual(m.timeseries['Rain'].points[1].value,50.8)
            self.assertEqual(path.read_bytes(),b'0 0\n1 2\n')

    def test_atomic_output_and_strict_charset_failure_leave_destination_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'series.dat';path.write_bytes(b'old')
            data=TimeSeriesData.from_bytes(b'0 1\n')
            with patch('easysewer.io._atomic.os.replace',side_effect=OSError('injected')):
                with self.assertRaises(OSError):data.write(path)
            self.assertEqual(path.read_bytes(),b'old')
            with self.assertRaises(ValueError):data.write(path,encoding='utf-8-sig')
            self.assertEqual(list(Path(directory).iterdir()),[path])


class RainfallDataTests(unittest.TestCase):
    def test_multiple_stations_keep_interleaving_and_export_selected_series(self):
        raw=b'; Rain\r\nA 2020 1 1 0 0 .1\r\nB 2020 1 1 0 0 .2\nA 2020 1 1 1 0 0\n'
        data=RainfallData.from_bytes(raw)
        self.assertEqual(data.to_bytes(),raw)
        self.assertEqual(len(data.station_series('a').points),2)
        self.assertEqual(RainfallData.from_bytes(data.to_bytes(normalize=True)),data)
        changed=replace(data,readings=tuple(replace(r,value=r.value*10) for r in data.readings))
        self.assertEqual(RainfallData.from_bytes(changed.to_bytes()).readings[1].value,2)

    def test_invalid_station_dates_sequence_and_format_detection_window(self):
        for raw in (b'',b'A 2020 2 30 0 0 1\n',b'A 2020 1 1 -1 0 1\n',
                    b'A 2020 1 1 0 0 nan\n',b';header\n'*5+b'A 2020 1 1 0 0 1\n',
                    b'A 2020 1 1 0 0 1\na 2020 1 1 0 0 2\n'):
            with self.assertRaises((ValueError,ValidationError)):RainfallData.from_bytes(raw)
        with self.assertRaises(ValueError):RainfallReading(station='two words',time=datetime(2020,1,1),value=1)

    def test_extended_hours_preserve_native_record_date_filter(self):
        raw=b'A 2020 1 1 24 0 1\nA 2020 1 2 1 0 2\n'
        data=RainfallData.from_bytes(raw)
        self.assertEqual(data.readings[0].time,datetime(2020,1,2))
        self.assertEqual(data.readings[0].file_date,date(2020,1,1))
        self.assertEqual(len(data.station_series('A',start_date=date(2020,1,2)).points),1)
        self.assertEqual(RainfallData.from_bytes(data.to_bytes(normalize=True)),data)

    def test_preflight_station_filter_nonfinite_path_and_start_date(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'rain.dat'
            path.write_bytes(b'A 2020 1 30 0 0 1\nA 2020 1 31 0 0 2\n')
            m=hydrology_model();m.raingages.update('R',source=FileRainfall(file=FileReference(path=str(path)),station='A',units='IN',start_date=date(2020,1,31)))
            self.assertTrue(check_files(m).complete)
            self.assertEqual(dict(check_files(m).checks[0].inspection.facts)['readings'],1)
            m.raingages.update('R',source=replace(m.raingages['R'].source,station='Missing'))
            self.assertFalse(check_files(m).report.is_valid)
            m.raingages.update('R',source=replace(m.raingages['R'].source,station='A'))
            for value in ('-1','1e100'):
                path.write_text('A 2020 1 31 0 0 '+value+'\n',encoding='ascii')
                self.assertFalse(check_files(m).report.is_valid)

    def test_unrecognized_future_rainfall_format_remains_visible(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'rain.dat';path.write_bytes(b'FUTURE-RAIN FORMAT 3\n')
            m=hydrology_model();m.raingages.update('R',source=FileRainfall(file=FileReference(path=str(path)),station='A',units='IN'))
            checked=check_files(m)
            self.assertFalse(checked.complete)
            self.assertEqual(checked.checks[0].inspection.status,'unsupported')


if __name__=='__main__':unittest.main()
