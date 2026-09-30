"""Independent physical climate fixtures and document/preflight contracts."""

from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from easysewer.io.climate_data import ClimateData, ClimateObservation, ClimateRecord
from easysewer.model import climate as c, FileReference
from easysewer.runtime import check_files
from easysewer.validation import ValidationError
from test_climate_v2 import climate_model


def td_month(year,month,parameter,value,*,flag='0',station='12345600',sign='+'):
    return f'DLY{station}{parameter}  {year:04d}{month:02d}9999031'+''.join(
        f'{day:02d}24{sign}{value:05d} {flag}' for day in range(1,32))


def dly_month(year,month,parameter,value,*,flag=' ',station='1234567',sign='+'):
    return f'{station}{year:04d}{month:02d}{parameter:03d}'+''.join(
        f'{sign}{value:05d}{flag}' for day in range(1,32))


def monthly_fixture(format,*,year=2020,months=(1,2)):
    rows=[]
    for month in months:
        if format=='TD3200':
            rows.extend(td_month(year,month,p,v) for p,v in (('TMAX',60),('TMIN',40),('EVAP',20),('WDMV',192)))
        else:
            rows.extend(dly_month(year,month,p,v) for p,v in ((1,200),(2,50),(151,20)))
    return ('\r\n'.join(rows)+'\r\n').encode('ascii')


def ghcnd_fixture(*,wind_movement=True):
    names=['STATION','DATE','TMAX','TMIN','EVAP','AWND']
    if wind_movement: names+=['WDMV']
    names+=['NOTE']
    widths=[18]+[14]*(len(names)-1)
    rows=[''.join(n.ljust(w) for n,w in zip(names,widths))]
    rows.append(''.join(('-'*len(n)).ljust(w) for n,w in zip(names,widths)))
    for offset in range(34):
        when=date(2020,1,1)+timedelta(days=offset)
        cells=['Station',when.strftime('%Y%m%d'),'200','50','20','100',*(['24'] if wind_movement else []),'kept']
        rows.append(''.join(n.ljust(w) for n,w in zip(cells,widths)))
    return ('\n'.join(rows)+'\n').encode('ascii')


class ClimateDataTests(unittest.TestCase):
    def test_four_formats_preserve_source_and_rewrite_daily_values(self):
        fixtures=[b'A 2020 1 1 50 30 .2 8\r\nA 2020 1 2 * * * *\n',ghcnd_fixture(),monthly_fixture('TD3200'),monthly_fixture('DLY0204')]
        for raw in fixtures:
            data=ClimateData.from_bytes(raw)
            with self.subTest(format=data.format):
                self.assertEqual(data.to_bytes(),raw)
                other=ClimateData.from_bytes(data.to_bytes(normalize=True))
                self.assertEqual(other.daily(unit_system='US',ghcnd_units='C10'),data.daily(unit_system='US',ghcnd_units='C10'))
                self.assertEqual(other.records,data.records)

    def test_monthly_flags_missing_unused_parameters_and_sign_rules_remain_visible(self):
        td=ClimateData.from_bytes((td_month(2020,1,'TMAX',50,flag='2')+'\n'+td_month(2020,1,'TMIN',99999)+'\n'+td_month(2020,1,'AWND',300)+'\n').encode())
        self.assertTrue(all(d.maximum is None and d.minimum is None and d.wind is None for d in td.daily(unit_system='US')))
        self.assertIn('climate.ignored_awnd',{d.code for d in td.report.diagnostics})
        dly=ClimateData.from_bytes((dly_month(2020,1,1,100,sign='-',flag='M')+'\n'+dly_month(2020,1,151,20,sign='-')+'\n'+dly_month(2020,1,3,42)+'\n').encode())
        self.assertEqual(dly.daily(unit_system='SI')[0].maximum,-10)
        self.assertEqual(dly.daily(unit_system='SI')[0].evaporation,2)
        self.assertIn('climate.ignored_evaporation_sign',{d.code for d in dly.report.diagnostics})
        self.assertEqual(ClimateData.from_bytes(dly.to_bytes(normalize=True)),dly)

    def test_contextual_units_ghcnd_wind_precedence_and_explicit_user_conversion(self):
        data=ClimateData.from_bytes(ghcnd_fixture())
        with self.assertRaises(ValueError): data.daily(unit_system='SI')
        for units in ('C10','C','F'):
            for system in ('US','SI'):
                days=data.daily(unit_system=system,ghcnd_units=units)
                self.assertAlmostEqual(days[0].wind,.62137 if units!='F' else 1)
                user=data.as_user(unit_system=system,ghcnd_units=units)
                self.assertEqual(user.daily(unit_system=system),days)
                self.assertEqual(ClimateData.from_bytes(user.to_bytes()).daily(unit_system=system),days)
        self.assertEqual(data.records[0].extra_fields,(('NOTE','kept'),))
        only_speed=ClimateData.from_bytes(ghcnd_fixture(wind_movement=False))
        for label,factor in (('C10',.62137*3.6/10),('C',.62137*3.6),('F',1)):
            self.assertAlmostEqual(only_speed.daily(unit_system='US',ghcnd_units=label)[0].wind,100*factor)
        missing_movement=replace(data,records=tuple(replace(r,observations=tuple(replace(v,value=None) if v.parameter=='WDMV' else v for v in r.observations)) for r in data.records))
        self.assertIsNone(missing_movement.daily(unit_system='US',ghcnd_units='C10')[0].wind)

    def test_empty_month_and_unterminated_eof_are_not_confused_with_missing_month(self):
        raw=b'DLY12345600TMAX  2020019999000\n'
        data=ClimateData.from_bytes(raw)
        self.assertEqual(data.records[0].observations,())
        self.assertEqual(data.to_bytes(normalize=True),raw)
        self.assertEqual(next(data.trajectory(start=date(2020,1,30),days=1,unit_system='US')).maximum,70)
        self.assertEqual(next(data.as_user(unit_system='US').trajectory(start=date(2020,1,30),days=1,unit_system='US')).maximum,70)
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'weather.dat';path.write_bytes(raw.rstrip(b'\n'))
            model=climate_model();model.update_climate(file=c.ClimateFile(file=FileReference(path=str(path))))
            checked=check_files(model)
            self.assertTrue(checked.report.is_valid)
            self.assertEqual(checked.checks[0].inspection.status,'pending')
            self.assertEqual(checked.checks[0].inspection.required_capabilities,('easysewer:climate-io:1',))
            self.assertFalse(check_files(model,backend_capabilities=()).report.is_valid)
            self.assertEqual(check_files(model,backend_capabilities=('easysewer:climate-io:1',)).checks[0].inspection.status,'validated')
            path.write_bytes(raw)
            self.assertTrue(check_files(model).report.is_valid)

    def test_trajectory_initial_default_hold_missing_days_months_and_years(self):
        data=ClimateData.from_bytes(b'A 2019 12 29 0 0 1 2\nA 2019 12 31 40 20 3 4\nA 2020 1 2 60 * * 8\nA 2020 3 1 70 50 1 1\n')
        days=list(data.trajectory(start=date(2019,12,30),days=64,unit_system='US'))
        self.assertEqual((days[0].maximum,days[0].evaporation),(70,0))
        self.assertEqual((days[1].maximum,days[1].minimum),(40,20))
        self.assertEqual((days[3].maximum,days[3].minimum),(60,20))
        self.assertEqual((days[-1].maximum,days[-1].minimum),(70,50))
        with self.assertRaises(ValueError):list(data.trajectory(start=date(2020,2,1),days=1,unit_system='US'))

    def test_station_overwrites_only_nonmissing_values_and_record_edit_is_independent(self):
        raw=b'A 2020 1 1 40 20 1 2\nB 2020 1 1 * 10 * 5\n'
        data=ClimateData.from_bytes(raw)
        day=data.daily(unit_system='US')[0]
        self.assertEqual((day.maximum,day.minimum,day.evaporation,day.wind),(40,10,1,5))
        self.assertIn('climate.multiple_stations',{d.code for d in data.report.diagnostics})
        record=data.records[0]
        changed=replace(data,records=(replace(record,observations=tuple(replace(v,value=80) if v.parameter=='TMAX' else v for v in record.observations)),*data.records[1:]))
        self.assertEqual(ClimateData.from_bytes(changed.to_bytes()).daily(unit_system='US')[0].maximum,80)
        self.assertEqual(data.to_bytes(),raw)

    def test_reject_bad_native_boundaries_dates_numbers_and_lossy_precision(self):
        invalid=[b'',b'\nA 2020 1 1 50 30\n',b'; comment\nA 2020 1 1 50 30\n',b'A 2020 2 30 1 0\n',
            b'A 2020 1 1 nope 0\n',b'A 2020 1 1 nan 0\n',b'A'*1023+b' 2020 1 1 1 0\n',
            b'A 2020 2 1 1 0\nA 2020 1 1 1 0\n',b'\xef\xbb\xbfA 2020 1 1 1 0\n',monthly_fixture('TD3200')[:40]+b'\n',
            b'A'*80+b' DATE TMAX\n']
        for raw in invalid:
            with self.subTest(raw=raw[:50]):
                with self.assertRaises((ValueError,ValidationError)):ClimateData.from_bytes(raw)
        with self.assertRaises(TypeError):ClimateData.from_bytes(bytearray(b'A 2020 1 1 1 0\n'))
        data=ClimateData.from_bytes(ghcnd_fixture())
        row=data.records[0]
        changed=replace(data,records=(replace(row,observations=tuple(replace(v,value=1.23456789) if v.parameter=='TMAX' else v for v in row.observations)),))
        with self.assertRaises(ValueError):changed.to_bytes()

    def test_long_station_and_complete_final_row_roundtrip_with_explicit_backend_gate(self):
        raw=b'A'*160+b' 2020 1 30 50 30 .2 5'
        data=ClimateData.from_bytes(raw)
        self.assertEqual(data.to_bytes(),raw)
        self.assertEqual(ClimateData.from_bytes(data.to_bytes(normalize=True)).records,data.records)
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'weather.dat';path.write_bytes(raw)
            model=climate_model();model.update_climate(file=c.ClimateFile(file=FileReference(path=str(path))))
            with patch('ctypes.CDLL',side_effect=AssertionError('Pure preflight must not load native code')):
                for capabilities,status in ((None,'pending'),((),'unsupported'),(('easysewer:climate-io:1',),'validated')):
                    checked=check_files(model,backend_capabilities=capabilities)
                    self.assertEqual(checked.checks[0].inspection.status,status)
                    self.assertEqual(checked.report.is_valid,status!='unsupported')

    def test_preflight_uses_model_start_month_consumers_and_si_wind_rules(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'weather.dat'
            model=climate_model()
            model.update_climate(file=c.ClimateFile(file=FileReference(path=str(path))),wind=c.FileWind(),
                evaporation=c.Evaporation(source=c.FileEvaporation()))
            for kind in ('TD3200','DLY0204'):
                path.write_bytes(monthly_fixture(kind))
                checked=check_files(model)
                self.assertTrue(checked.report.is_valid,checked.report)
                self.assertEqual(dict(checked.checks[0].inspection.facts)['format'],kind)
            path.write_bytes(b'A 2020 1 30 50 30 -1 -2\n')
            codes={d.code for d in check_files(model).report.errors}
            self.assertTrue({'climate.negative_file_evaporation','climate.negative_file_wind'}<=codes)
            model.update_climate(file=replace(model.climate.file,start_date=date(2021,1,1)))
            self.assertFalse(check_files(model).report.is_valid)
            model.update_climate(file=replace(model.climate.file,start_date=date(2020,1,30)))
            model.reinterpret_units('CMS');path.write_bytes(b'A 2020 1 30 20 10 1 2\n')
            self.assertIn('climate.user_wind_mph',{d.code for d in check_files(model).report.diagnostics})
            path.write_bytes(b'A 2020 1 30 20 10 1 2\nA 2020 1 31 5 * * *\n')
            self.assertIn('climate.inverted_temperature_range',{d.code for d in check_files(model).report.errors})

    def test_atomic_failure_encoding_and_immutable_collections(self):
        data=ClimateData.from_bytes(b'A 2020 1 1 50 30 .2 8\n')
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'weather.dat';path.write_bytes(b'keep')
            with patch('easysewer.io._atomic.os.replace',side_effect=OSError('injected')):
                with self.assertRaises(OSError):data.write(path)
            self.assertEqual(path.read_bytes(),b'keep')
            with self.assertRaises(ValueError):data.write(path,encoding='utf-16-le')
            self.assertEqual(path.read_bytes(),b'keep')
        with self.assertRaises(TypeError):replace(data,records=list(data.records))
        with self.assertRaises(TypeError):replace(data.records[0],observations=list(data.records[0].observations))


if __name__=='__main__':unittest.main()
