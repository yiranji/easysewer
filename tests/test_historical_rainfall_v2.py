"""All historical rainfall layouts and native interval semantics."""

from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from easysewer.io.historical_rainfall import HistoricalRainfallData, FORMATS
from easysewer.model import FileReference
from easysewer.model.hydrology import FileRainfall
from easysewer.runtime import check_files
from easysewer.validation import ValidationError
from test_hydrology_v2 import hydrology_model


def nws_fixture(kind,*,day=date(2020,1,30),element='HPCP',readings=((3,0,60,' ',' '),(4,0,20,' ',' ')),station='123456',named=False):
    y,m,d=day.year,day.month,day.day
    if kind=='NWS_TAPE':
        return (f'HPD{station}00{element}HI{y:04d}{m:02d}{d:04d}{len(readings):03d}'+''.join(f'{h:02d}{mi:02d}{v:06d}{a}{b}' for h,mi,v,a,b in readings)+'\n').encode()
    if kind in ('NWS_SPACE_DELIMITED','NWS_COMMA_DELIMITED'):
        sep=',' if kind=='NWS_COMMA_DELIMITED' else ' '
        header=station+sep+('Test station'.ljust(30)+' ' if named else '')+sep.join(('00',element,'HI',f'{y:04d}',f'{m:02d}',f'{d:02d}'))
        return (header+''.join(sep+f'{h:02d}{mi:02d}'+sep+f'{v:6d}'+sep+a+sep+b for h,mi,v,a,b in readings)+'\n').encode()
    element='HPCP' if kind=='NWS_ONLINE_60' else 'QPCP'
    header='STATION'.ljust(17)+'DATE'.ljust(19)+element+'\n'
    return (header+''.join(('COOP:'+station).ljust(17)+f'{y:04d}{m:02d}{d:02d} {h:02d}:{mi:02d}'.ljust(19)+f'{v} {a}'.ljust(12)+'\n' for h,mi,v,a,b in readings)).encode()


def canadian_fixture(kind,*,day=date(2020,1,30),values=None,flag=' ',element=None,station='1234567'):
    count=96 if kind=='CMC_FIF' else 24
    if values is None:values={3:150,4:50}
    year=f'{day.year%1000:03d}' if kind=='AES_HLY' else f'{day.year:04d}'
    element=element or ('159' if count==96 else '123')
    return (station+year+f'{day.month:02d}{day.day:02d}'+element+''.join(f'{values.get(i,0):06d}{flag}' for i in range(count))+'\n').encode()


def fixture(kind):
    return canadian_fixture(kind) if kind in ('AES_HLY','CMC_HLY','CMC_FIF') else nws_fixture(kind)


class HistoricalRainfallTests(unittest.TestCase):
    def test_all_eight_source_records_and_canonical_roundtrip(self):
        for kind in FORMATS:
            with self.subTest(format=kind):
                raw=fixture(kind).replace(b'\n',b'\r\n')
                data=HistoricalRainfallData.from_bytes(raw)
                self.assertEqual(data.format,kind)
                self.assertEqual(data.to_bytes(),raw)
                self.assertEqual(HistoricalRainfallData.from_bytes(data.to_bytes(normalize=True)),data)
                interpreted=data.interpret()
                self.assertTrue(interpreted.report.is_valid)
                self.assertEqual(len([p for p in interpreted.periods if p.saved]),2)

    def test_interval_end_filter_and_canadian_first_slot_previous_day(self):
        data=HistoricalRainfallData.from_bytes(canadian_fixture('CMC_HLY',values={0:254,1:127}))
        active=[p for p in data.interpret(start_date=date(2020,1,30)).periods if p.saved]
        self.assertEqual(active[0].time,datetime(2020,1,29,23))
        self.assertEqual(active[0].record_date,date(2020,1,30))
        self.assertEqual(active[0].depth_inches,1)
        self.assertFalse(data.interpret(start_date=date(2020,1,31)).periods)
        online=HistoricalRainfallData.from_bytes(nws_fixture('NWS_ONLINE_15',readings=((0,15,25,' ',' '),)))
        self.assertEqual(online.interpret().periods[0].time,datetime(2020,1,30))

    def test_accumulation_missing_deleted_and_native_flag_reset(self):
        readings=((1,0,0,'a',' '),(3,0,60,'A',' '),(4,0,10,'{',' '),(5,0,10,' ',' '),(6,0,10,'}',' '),
                  (7,0,10,'[',' '),(8,0,10,']',' '),(9,0,10,'M',' '),(10,0,9999,' ',' '))
        data=HistoricalRainfallData.from_bytes(nws_fixture('NWS_TAPE',readings=readings))
        result=data.interpret();periods=result.periods
        self.assertEqual([p.time.hour for p in periods if p.saved],[0,1,2,4])
        self.assertTrue(all(abs(p.depth_inches-.2)<1e-7 for p in periods[:3]))
        self.assertEqual([p.status for p in periods[3:]],['deleted','observed','deleted','missing','missing','missing','missing'])
        with self.assertRaises(ValueError):data.as_user()
        user=data.as_user(missing='omit')
        self.assertEqual(len(user.readings),4)
        self.assertEqual(user.readings[0].time,datetime(2020,1,30))

    def test_missing_accumulation_keeps_anchor_and_expansion_budget(self):
        data=HistoricalRainfallData.from_bytes(nws_fixture('NWS_TAPE',readings=((1,0,0,'a',' '),(2,0,99999,'A',' '),(3,0,60,'A',' '))))
        result=data.interpret()
        self.assertEqual(len(result.periods),5)
        self.assertEqual(len([p for p in result.periods if p.saved]),3)
        self.assertIn('rainfall.missing_accumulation_keeps_start',{d.code for d in result.report.diagnostics})
        with self.assertRaises(ValueError):data.interpret(max_periods=4)
        filtered=data.interpret(start_date=date(2020,1,31))
        self.assertFalse(filtered.periods)

    def test_online_decimal_integer_and_rounding_and_space_quality_promotion(self):
        raw=nws_fixture('NWS_ONLINE_60',readings=((3,0,.126,' ',' '),(4,0,20,' ',' ')))
        data=HistoricalRainfallData.from_bytes(raw)
        self.assertTrue(data.records[0].observations[0].decimal_inches)
        self.assertFalse(data.records[1].observations[0].decimal_inches)
        self.assertAlmostEqual(data.interpret().periods[0].depth_inches,.13,places=7)
        raw=nws_fixture('NWS_SPACE_DELIMITED',named=True,readings=((3,0,60,' ','M'),(4,0,20,' ',' ')))
        data=HistoricalRainfallData.from_bytes(raw)
        self.assertEqual(data.records[0].station_name,'Test station')
        self.assertEqual(data.interpret().periods[0].status,'missing')
        self.assertEqual(HistoricalRainfallData.from_bytes(data.to_bytes(normalize=True)),data)
        reference=data.interpret(arithmetic='reference').periods[-1].depth_inches
        reciprocal=data.interpret(arithmetic='float32_reciprocal_100').periods[-1].depth_inches
        self.assertNotEqual(reference,reciprocal)
        self.assertAlmostEqual(reference,reciprocal,delta=2e-8)
        with self.assertRaises(ValueError):data.interpret(arithmetic='unknown')
        large=HistoricalRainfallData.from_bytes(nws_fixture('NWS_ONLINE_60',readings=((3,0,2147483647,' ',' '),)))
        self.assertEqual(large.interpret().periods[0].status,'missing')
        self.assertEqual(HistoricalRainfallData.from_bytes(large.to_bytes(normalize=True)),large)
        with self.assertRaises((ValueError,ValidationError)):
            HistoricalRainfallData.from_bytes(nws_fixture('NWS_ONLINE_60',readings=((3,0,2147483648,' ',' '),)))

    def test_ignored_canadian_flags_elements_and_aes_year_mapping(self):
        data=HistoricalRainfallData.from_bytes(canadian_fixture('AES_HLY',day=date(1999,1,1),flag='M')+canadian_fixture('AES_HLY',day=date(2000,1,1),element='001'))
        self.assertEqual(tuple(r.date.year for r in data.records),(1999,2000))
        self.assertEqual(len([p for p in data.interpret().periods if p.saved]),2)
        self.assertIn('rainfall.archive_ignored_quality',{d.code for d in data.report.diagnostics})

    def test_edits_multi_station_and_duplicate_native_cache_times(self):
        raw=nws_fixture('NWS_TAPE')+nws_fixture('NWS_TAPE',day=date(2020,1,31),station='654321')
        data=HistoricalRainfallData.from_bytes(raw)
        changed=replace(data,records=tuple(replace(r,observations=tuple(replace(v,value=v.value*2) for v in r.observations)) for r in data.records))
        self.assertEqual(HistoricalRainfallData.from_bytes(changed.to_bytes()),changed)
        self.assertEqual(data.to_bytes(),raw)
        self.assertIn('rainfall.archive_multiple_stations',{d.code for d in data.report.diagnostics})
        with self.assertRaises(ValueError):HistoricalRainfallData.from_bytes(nws_fixture('NWS_TAPE')*2).interpret()

    def test_bad_headers_counts_columns_and_native_scanner_ambiguity(self):
        for raw in (b'',b'; header\n'*5+fixture('NWS_TAPE'),fixture('CMC_FIF').rstrip(b'\n'),
                    fixture('CMC_HLY')[:-10],fixture('NWS_TAPE').replace(b'2020010030',b'2020020030'),
                    fixture('CMC_HLY').replace(b'000150 ',b'   1500'),b'\xef\xbb\xbf'+fixture('NWS_TAPE'),
                    fixture('NWS_ONLINE_60').replace(b'STATION',b'STATI\xc3\x93N')):
            with self.subTest(raw=raw[:40]):
                with self.assertRaises((ValueError,ValidationError)):HistoricalRainfallData.from_bytes(raw)
        with self.assertRaises(TypeError):HistoricalRainfallData.from_bytes(bytearray(fixture('NWS_TAPE')))
        data=HistoricalRainfallData.from_bytes(fixture('NWS_TAPE'))
        with self.assertRaises(TypeError):replace(data,records=list(data.records))

    def test_preflight_detects_all_formats_uses_archive_interval_and_ignores_gage_station(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'rain.dat'
            model=hydrology_model()
            model.raingages.update('R',source=FileRainfall(file=FileReference(path=str(path)),station='AnyStation',units='MM'))
            for kind in FORMATS:
                path.write_bytes(fixture(kind))
                checked=check_files(model)
                self.assertTrue(checked.complete,checked.report)
                facts=dict(checked.checks[0].inspection.facts)
                self.assertEqual(facts['format'],kind)
                self.assertEqual(facts['native_units'],'IN')
            path.write_bytes(canadian_fixture('CMC_HLY',values={0:-99999,1:254}))
            self.assertFalse(check_files(model).complete)
            path.write_bytes(canadian_fixture('CMC_HLY',values={}))
            self.assertFalse(check_files(model).report.is_valid)

    def test_atomic_export_errors_leave_original_target(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'rain.dat';path.write_bytes(b'keep')
            data=HistoricalRainfallData.from_bytes(fixture('NWS_TAPE'))
            with patch('easysewer.io._atomic.os.replace',side_effect=OSError('injected')):
                with self.assertRaises(OSError):data.write(path)
            with self.assertRaises(ValueError):data.write(path,encoding='utf-16-le')
            self.assertEqual(path.read_bytes(),b'keep')

    def test_optional_final_codes_and_native_hour_25_terminator(self):
        for kind in ('NWS_TAPE','NWS_SPACE_DELIMITED','NWS_COMMA_DELIMITED'):
            full=nws_fixture(kind,readings=((3,0,60,' ',' '),))
            suffix=2 if kind=='NWS_TAPE' else 4
            shortened=full[:-1-suffix]+b'\n'
            document=HistoricalRainfallData.from_bytes(shortened)
            self.assertEqual(document.interpret().periods[0].depth_inches,HistoricalRainfallData.from_bytes(full).interpret().periods[0].depth_inches)
            self.assertEqual(HistoricalRainfallData.from_bytes(document.to_bytes(normalize=True)),document)
            terminated=HistoricalRainfallData.from_bytes(nws_fixture(kind,readings=((3,0,60,' ',' '),(25,0,500,' ',' '),(4,0,20,' ',' '))))
            self.assertEqual(len(terminated.interpret().periods),1)
            self.assertEqual(HistoricalRainfallData.from_bytes(terminated.to_bytes(normalize=True)),terminated)


if __name__=='__main__':unittest.main()
