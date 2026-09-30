"""Actual archive rain caches and hydraulics, including native condition quirks."""

from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
import struct
import tempfile
import unittest

from easysewer import get_native_capabilities
from easysewer.io.historical_rainfall import HistoricalRainfallData, FORMATS
from easysewer.model import FileReference
from easysewer.model.hydrology import FileRainfall
from easysewer.runtime import check_files
from test_hydrology_v2 import hydrology_model
from test_historical_rainfall_v2 import fixture, nws_fixture, canadian_fixture
import test_native_v2_files as native_files


@unittest.skipUnless(get_native_capabilities()['swmm_solver'] and get_native_capabilities()['swmm_output'],'Native solver/output unavailable')
class NativeHistoricalRainfallTests(unittest.TestCase):
    solve=native_files.NativeFilesTests.solve

    def model(self,path,*,units='CFS',start_date=None):
        model=native_files.selected(hydrology_model())
        model.reinterpret_units(units);model.update_options(routing_step=timedelta(seconds=60))
        # Archive readers must override this intentionally unrelated declaration.
        model.raingages.update('R',form='CUMULATIVE',interval=timedelta(minutes=30),
            source=FileRainfall(file=FileReference(path=str(path)),station='Archive',units='MM',start_date=start_date))
        return model

    def cache(self,path):
        data=Path(path).read_bytes()
        self.assertEqual(data[:10],b'SWMM5-RAIN')
        self.assertEqual(struct.unpack_from('<i',data,10)[0],1)
        interval,start,end=struct.unpack_from('<iii',data,14+1025)
        result=[]
        for offset in range(start,end,12):
            stamp,value=struct.unpack_from('<df',data,offset)
            when=datetime(1899,12,30)+timedelta(seconds=round(stamp*86400))
            result.append((when,value))
        return interval,result

    def assert_interpretation(self,cache,document,*,start_date=None):
        interval,actual=self.cache(cache)
        predicted=document.interpret(start_date=start_date,arithmetic='reference')
        self.assertEqual(interval,predicted.interval.total_seconds())
        expected=[(p.time,p.depth_inches) for p in predicted.periods if p.saved]
        self.assertEqual(actual,expected)

    def with_cache(self,model,cache):
        return model.to_document().text+f'[FILES]\nSAVE RAINFALL "{cache}"\n'

    def test_all_eight_us_si_cache_roundtrip_and_independent_user_oracle(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);path=root/'rain.dat';cache=root/'cache.bin';user=root/'user.dat'
            for kind in FORMATS:
                for units in ('CFS','CMS'):
                    with self.subTest(format=kind,units=units):
                        raw=fixture(kind);path.write_bytes(raw)
                        model=self.model(path,units=units)
                        source=self.with_cache(model,cache)
                        expected=self.solve(root,'original',source)
                        document=HistoricalRainfallData.read(path)
                        self.assert_interpretation(cache,document)
                        self.assertTrue(check_files(model).complete)
                        self.assertGreater(max(expected['series'][0][0][4]),0)
                        document.write(path,normalize=True)
                        self.assertEqual(expected,self.solve(root,'rewritten',source))
                        self.assert_interpretation(cache,document)
                        # Independent literal interval starts and depth units.
                        if kind in ('AES_HLY','CMC_HLY','CMC_FIF'):
                            minutes=15 if kind=='CMC_FIF' else 60
                            moments=[datetime(2020,1,30)+timedelta(minutes=(i-1)*minutes) for i in (3,4)]
                            depths=[struct.unpack('<f',struct.pack('<f',v/10/25.4))[0] for v in (150,50)]
                        else:
                            minutes=15 if kind=='NWS_ONLINE_15' else 60
                            moments=[datetime(2020,1,30,h)-timedelta(minutes=minutes) for h in (3,4)]
                            # The pinned standard build disables fast-math and
                            # follows the reference float division by 100.
                            depths=[struct.unpack('<f',struct.pack('<f',v/100))[0] for v in (60,20)]
                        user.write_text(''.join(f'Archive {t:%Y %m %d %H %M} {v!r}\n' for t,v in zip(moments,depths)),encoding='ascii')
                        oracle=model.copy();oracle.raingages.update('R',form='VOLUME',interval=timedelta(minutes=minutes),source=FileRainfall(file=FileReference(path=str(user)),station='Archive',units='IN'))
                        actual=self.solve(root,'oracle',oracle.to_document().text)
                        self.assertEqual(actual['series'],expected['series'])
                        self.assertEqual(actual['system'],expected['system'])

    def test_all_formats_edits_change_runoff_without_mutating_original_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);path=root/'rain.dat';cache=root/'cache.bin'
            for kind in FORMATS:
                with self.subTest(format=kind):
                    raw=fixture(kind);path.write_bytes(raw)
                    model=self.model(path);source=self.with_cache(model,cache)
                    before=self.solve(root,'before',source)
                    document=HistoricalRainfallData.read(path)
                    edited=replace(document,records=tuple(replace(r,observations=tuple(replace(v,value=v.value*2) for v in r.observations)) for r in document.records))
                    edited.write(path)
                    after=self.solve(root,'after',source)
                    self.assertNotEqual(before['series'],after['series'])
                    self.assert_interpretation(cache,edited)
                    # Native cache itself is an independent observation of the
                    # changed source, in addition to the actual runoff change.
                    self.assertEqual(document.to_bytes(),raw)

    def test_nws_conditions_accumulation_reset_decimal_rounding_and_space_promotion(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);path=root/'rain.dat';cache=root/'cache.bin'
            conditions=((1,0,0,'a',' '),(3,0,60,'A',' '),(4,0,10,'{',' '),(5,0,10,' ',' '),(6,0,10,'}',' '),
                        (7,0,10,'[',' '),(8,0,10,']',' '),(9,0,10,'M',' '),(10,0,9999,' ',' '),(11,0,20,' ',' '))
            for kind in ('NWS_TAPE','NWS_SPACE_DELIMITED','NWS_COMMA_DELIMITED','NWS_ONLINE_60','NWS_ONLINE_15'):
                for special in ('conditions','missing_accumulation'):
                    readings=conditions if special=='conditions' else ((1,0,0,'a',' '),(2,0,99999,'A',' '),(3,0,60,'A',' '))
                    with self.subTest(format=kind,case=special):
                        path.write_bytes(nws_fixture(kind,readings=readings))
                        model=self.model(path);source=self.with_cache(model,cache)
                        original=self.solve(root,'flags',source)
                        data=HistoricalRainfallData.read(path);self.assert_interpretation(cache,data)
                        data.write(path,normalize=True)
                        self.assertEqual(original,self.solve(root,'flags_rewrite',source))
            for kind,readings in (('NWS_ONLINE_60',((3,0,.126,' ',' '),(4,0,.245,' ',' '))),
                                  ('NWS_SPACE_DELIMITED',((3,0,60,' ','M'),(4,0,20,' ',' ')))):
                path.write_bytes(nws_fixture(kind,readings=readings,named=kind=='NWS_SPACE_DELIMITED'))
                self.solve(root,'special',self.with_cache(self.model(path),cache))
                self.assert_interpretation(cache,HistoricalRainfallData.read(path))
            for kind in ('NWS_TAPE','NWS_SPACE_DELIMITED','NWS_COMMA_DELIMITED'):
                raw=nws_fixture(kind,readings=((3,0,60,' ',' '),))
                raw=raw[:-(3 if kind=='NWS_TAPE' else 5)]+b'\n'
                path.write_bytes(raw)
                model=self.model(path);source=self.with_cache(model,cache)
                expected=self.solve(root,'no_codes',source)
                data=HistoricalRainfallData.read(path);self.assert_interpretation(cache,data)
                data.write(path,normalize=True)
                self.assertEqual(expected,self.solve(root,'no_codes_rewrite',source))
                path.write_bytes(nws_fixture(kind,readings=((3,0,60,' ',' '),(25,0,500,' ',' '),(4,0,20,' ',' '))))
                self.solve(root,'terminator',source)
                self.assert_interpretation(cache,HistoricalRainfallData.read(path))

    def test_canadian_missing_flags_years_station_merge_and_record_date_filter(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);path=root/'rain.dat';cache=root/'cache.bin'
            for kind in ('AES_HLY','CMC_HLY','CMC_FIF'):
                with self.subTest(format=kind):
                    raw=canadian_fixture(kind,day=date(1999,12,31),values={0:254,2:-99999,3:127},flag='M')
                    raw+=canadian_fixture(kind,day=date(2000,1,1),values={0:254,3:127},flag='M',station='7654321')
                    path.write_bytes(raw)
                    model=self.model(path,start_date=date(2000,1,1))
                    model.update_options(start_date=date(2000,1,1),report_start_date=date(2000,1,1),end_date=date(2000,1,3))
                    self.solve(root,'canadian',self.with_cache(model,cache))
                    data=HistoricalRainfallData.read(path)
                    self.assert_interpretation(cache,data,start_date=date(2000,1,1))
                    self.assertLess(self.cache(cache)[1][0][0],datetime(2000,1,1))
                    data.write(path,normalize=True)
                    self.solve(root,'canadian_rewrite',self.with_cache(model,cache))
                    self.assert_interpretation(cache,data,start_date=date(2000,1,1))

    def test_older_fifteen_minute_elements_and_explicit_user_materialization(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);path=root/'rain.dat';cache=root/'cache.bin';user=root/'user.dat'
            for kind in ('NWS_TAPE','NWS_SPACE_DELIMITED','NWS_COMMA_DELIMITED'):
                for element in ('QPCP','QGAG'):
                    with self.subTest(format=kind,element=element):
                        raw=nws_fixture(kind,element=element,named=kind=='NWS_SPACE_DELIMITED',readings=((0,15,25,' ',' '),(0,30,50,' ',' ')))
                        path.write_bytes(raw);model=self.model(path)
                        before=self.solve(root,'quarter',self.with_cache(model,cache))
                        data=HistoricalRainfallData.read(path)
                        self.assertEqual(data.interval,timedelta(minutes=15))
                        self.assert_interpretation(cache,data)
                        data.as_user(station='Archive',arithmetic='reference').write(user)
                        model.raingages.update('R',form='VOLUME',interval=timedelta(minutes=15),source=FileRainfall(file=FileReference(path=str(user)),station='Archive',units='IN'))
                        after=self.solve(root,'materialized',model.to_document().text)
                        self.assertEqual(before['series'],after['series'])
                        self.assertEqual(before['system'],after['system'])


if __name__=='__main__':unittest.main()
