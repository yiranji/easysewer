"""Native climate files compared with independent daily-file oracles."""

from dataclasses import replace
from datetime import date, time, timedelta
from pathlib import Path
import tempfile
import unittest
import subprocess
import sys
import json
import easysewer

from easysewer import get_native_capabilities
from easysewer.io.climate_data import ClimateData
from easysewer.model import climate as c, FileReference
from test_climate_v2 import climate_model
from test_climate_data_v2 import monthly_fixture, td_month, dly_month, ghcnd_fixture
import test_native_v2_climate as native_climate

SNOW_CATCHMENT=native_climate.SNOW_CATCHMENT


@unittest.skipUnless(get_native_capabilities()['swmm_solver'] and get_native_capabilities()['swmm_output'],'Native solver/output unavailable')
class NativeClimateDataTests(unittest.TestCase):
    solve=native_climate.NativeClimateTests.solve

    def compare_results(self, a, b):
        self.assertEqual(a.keys(),b.keys())
        for key in a:
            if key in ('history','balance'):
                self.assertEqual(len(a[key]),len(b[key]))
                for x,y in zip(a[key],b[key]):
                    if isinstance(x,(list,tuple)):
                        for xx,yy in zip(x,y):self.assertAlmostEqual(xx,yy,delta=2e-6,msg=key)
                    else:self.assertAlmostEqual(x,y,delta=2e-6,msg=key)
            else:
                self.assertEqual(len(a[key]),len(b[key]))
                for x,y in zip(a[key],b[key]):self.assertAlmostEqual(x,y,delta=1e-5,msg=key)

    def configured(self, path, *, units='CFS', file_units='C10', file_start=None):
        model=climate_model();model.reinterpret_units(units)
        model.update_climate(file=c.ClimateFile(file=FileReference(path=str(path)),units=file_units,start_date=file_start),
            wind=c.FileWind(),evaporation=c.Evaporation(source=c.FileEvaporation()))
        return model

    def test_monthly_formats_roundtrip_edit_and_independent_us_si_daily_oracles(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'weather.dat'
            oracle=Path(directory)/'daily.dat'
            for format in ('TD3200','DLY0204'):
                for units in ('CFS','CMS'):
                    with self.subTest(format=format,units=units):
                        raw=monthly_fixture(format);path.write_bytes(raw)
                        model=self.configured(path,units=units)
                        expected=self.solve(directory,'original',model.to_document().text+SNOW_CATCHMENT,snow=True)
                        self.assertGreater(max(expected['evaporation']),0)
                        self.assertGreater(max(expected['snow']),0)
                        data=ClimateData.read(path);data.write(path,normalize=True)
                        actual=self.solve(directory,'canonical',model.to_document().text+SNOW_CATCHMENT,snow=True)
                        self.assertEqual(expected,actual)
                        if format=='TD3200':
                            high,low,evap,wind=60,40,.2,8
                            if units=='CMS':high,low,evap=(high-32)*5/9,(low-32)*5/9,evap*25.4
                        else:
                            high,low,evap,wind=20,5,2,0
                            if units=='CFS':high,low,evap=high*9/5+32,low*9/5+32,evap/25.4
                        rows=[]
                        for offset in range(34):
                            when=date(2020,1,1)+timedelta(days=offset)
                            rows.append(f'Manual {when.year} {when.month} {when.day} {high!r} {low!r} {evap!r} {wind!r}')
                        oracle.write_text('\n'.join(rows)+'\n',encoding='ascii')
                        independent=self.configured(oracle,units=units)
                        self.compare_results(expected,self.solve(directory,'manual',independent.to_document().text+SNOW_CATCHMENT,snow=True))
                        edited=replace(data,records=tuple(replace(r,observations=tuple(replace(v,value=v.value*2) if v.parameter=='EVAP' else v for v in r.observations)) for r in data.records))
                        edited.write(path)
                        changed=self.solve(directory,'edited',model.to_document().text+SNOW_CATCHMENT,snow=True)
                        self.assertNotEqual(changed['evaporation'],expected['evaporation'])
                        oracle.write_text('\n'.join(row.rsplit(' ',2)[0]+f' {evap*2!r} {wind!r}' for row in rows)+'\n',encoding='ascii')
                        self.compare_results(changed,self.solve(directory,'manual_edit',independent.to_document().text+SNOW_CATCHMENT,snow=True))
                        self.assertEqual(data.to_bytes(),raw)

    def test_ghcnd_all_unit_labels_wdmv_precedence_and_flags_survive_rewrite(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'weather.dat';user=Path(directory)/'user.dat'
            for label in ('C10','C','F'):
                for units in ('CFS','CMS'):
                    with self.subTest(label=label,units=units):
                        raw=ghcnd_fixture().replace(b'200           ',b'200     ,E    ')
                        path.write_bytes(raw)
                        model=self.configured(path,units=units,file_units=label)
                        expected=self.solve(directory,'ghcnd',model.to_document().text+SNOW_CATCHMENT,snow=True)
                        document=ClimateData.read(path)
                        self.assertIn('climate.ignored_quality_flags',{d.code for d in document.report.diagnostics})
                        document.write(path,normalize=True)
                        self.assertEqual(self.solve(directory,'rewritten',model.to_document().text+SNOW_CATCHMENT,snow=True),expected)
                        # Independent literal daily data. AWND=100 must lose to WDMV=24.
                        high,low,evap=200,50,20
                        if label=='C10':high,low,evap=20,5,2
                        if label!='F' and units=='CFS':high,low,evap=high*9/5+32,low*9/5+32,evap/25.4
                        if label=='F' and units=='CMS':high,low,evap=(high-32)*5/9,(low-32)*5/9,evap*25.4
                        wind=.62137 if label!='F' else 1
                        rows=[]
                        for offset in range(34):
                            when=date(2020,1,1)+timedelta(days=offset)
                            rows.append(f'Manual {when.year} {when.month} {when.day} {high!r} {low!r} {evap!r} {wind!r}')
                        user.write_text('\n'.join(rows)+'\n',encoding='ascii')
                        self.compare_results(expected,self.solve(directory,'user',self.configured(user,units=units).to_document().text+SNOW_CATCHMENT,snow=True))

    def test_native_flag_suppression_and_dly_ignored_flags_and_evaporation_sign(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'weather.dat';oracle=Path(directory)/'oracle.dat'
            for format in ('TD3200','DLY0204'):
                if format=='TD3200':
                    raw='\n'.join(td_month(2020,m,p,v,flag=f) for m in (1,2) for p,v,f in (('TMAX',50,'0'),('TMIN',30,'1'),('EVAP',40,'2'),('WDMV',99999,'0'),('AWND',500,'0')))+'\n'
                    high,low,evap=50,30,0
                else:
                    raw='\n'.join(dly_month(2020,m,p,v,sign='-',flag='M') for m in (1,2) for p,v in ((1,100),(2,200),(151,20)))+'\n'
                    high,low,evap=14,-4,2/25.4
                path.write_text(raw,encoding='ascii');model=self.configured(path)
                expected=self.solve(directory,'flags',model.to_document().text+SNOW_CATCHMENT,snow=True)
                rows=[]
                for offset in range(34):
                    when=date(2020,1,1)+timedelta(days=offset)
                    rows.append(f'Manual {when.year} {when.month} {when.day} {high} {low} {evap!r} 0')
                oracle.write_text('\n'.join(rows)+'\n',encoding='ascii')
                self.compare_results(expected,self.solve(directory,'flags_oracle',self.configured(oracle).to_document().text+SNOW_CATCHMENT,snow=True))
                data=ClimateData.read(path);data.write(path,normalize=True)
                self.assertEqual(self.solve(directory,'flags_rewrite',model.to_document().text+SNOW_CATCHMENT,snow=True),expected)

    def test_monthly_cross_year_and_leap_day_boundaries(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'weather.dat';oracle=Path(directory)/'daily.dat'
            for kind in ('TD3200','DLY0204'):
                for start in (date(2019,12,30),date(2020,2,28)):
                    with self.subTest(format=kind,start=start):
                        end=start+timedelta(days=4)
                        months=((start.year,start.month),(end.year,end.month))
                        rows=[]
                        for year,month in months:
                            if kind=='TD3200': rows.extend(td_month(year,month,p,v) for p,v in (('TMAX',60+month),('TMIN',40),('EVAP',20+month),('WDMV',192)))
                            else: rows.extend(dly_month(year,month,p,v) for p,v in ((1,200+month),(2,50),(151,20+month)))
                        path.write_text('\n'.join(rows)+'\n',encoding='ascii')
                        model=self.configured(path,file_start=start)
                        model.update_options(start_date=start,end_date=end,report_start_date=start)
                        expected=self.solve(directory,'boundary',model.to_document().text+SNOW_CATCHMENT,snow=True)
                        rows=[]
                        for offset in range(5):
                            when=start+timedelta(days=offset);month=when.month
                            if kind=='TD3200':high,low,evap,wind=60+month,40,(20+month)/100,8
                            else:high,low,evap,wind=(200+month)/10*1.8+32,41,(20+month)/10/25.4,0
                            rows.append(f'Manual {when.year} {when.month} {when.day} {high!r} {low!r} {evap!r} {wind!r}')
                        oracle.write_text('\n'.join(rows)+'\n',encoding='ascii')
                        other=model.copy();other.update_climate(file=replace(model.climate.file,file=FileReference(path=str(oracle))))
                        self.compare_results(expected,self.solve(directory,'boundary_oracle',other.to_document().text+SNOW_CATCHMENT,snow=True))
                        document=ClimateData.read(path);document.write(path,normalize=True)
                        self.assertEqual(expected,self.solve(directory,'boundary_rewrite',model.to_document().text+SNOW_CATCHMENT,snow=True))

    def test_initial_missing_day_no_lookback_year_transition_station_overwrite_and_explicit_start(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'weather.dat';oracle=Path(directory)/'oracle.dat'
            path.write_bytes(b'A 2019 12 29 0 0 1 2\nA 2019 12 31 40 20 .1 3\nB 2019 12 31 * 10 * 5\nA 2020 1 2 60 * .2 8\n')
            model=self.configured(path,file_start=date(2019,12,30))
            model.update_options(start_date=date(2020,1,30),end_date=date(2020,2,3))
            expected=self.solve(directory,'sparse',model.to_document().text+SNOW_CATCHMENT,snow=True)
            # File dates advance with simulation days, not simulation month/day.
            oracle.write_bytes(b'M 2019 12 30 70 70 0 0\nM 2019 12 31 40 10 .1 5\nM 2020 1 1 40 10 .1 5\nM 2020 1 2 60 10 .2 8\nM 2020 1 3 60 10 .2 8\n')
            other=model.copy();other.update_climate(file=replace(model.climate.file,file=FileReference(path=str(oracle))))
            self.assertEqual(expected,self.solve(directory,'filled',other.to_document().text+SNOW_CATCHMENT,snow=True))
            data=ClimateData.read(path);data.write(path,normalize=True)
            self.assertEqual(expected,self.solve(directory,'sparse_rewrite',model.to_document().text+SNOW_CATCHMENT,snow=True))
            model.update_climate(file=replace(model.climate.file,start_date=date(2019,11,1)))
            # A failed native climate start can leave the climate FILE* open
            # despite swmm_close; isolate this failure path and release it at
            # process exit before the parent removes the fixture directory.
            source=Path(directory)/'failure-source.inp'
            source.write_text(model.to_document().text,encoding='utf-8')
            # Bind the child to the same package as the parent. PYTHONPATH is
            # insufficient for embedded Python runtimes, and may otherwise
            # silently load a source checkout during installed-wheel testing.
            package = str(Path(easysewer.__file__).resolve().parent.parent)
            paths = [package, str(Path(__file__).resolve().parent)]
            code = ('import json,sys; from pathlib import Path; sys.path[:0]=' + repr(paths) + '; '
                'import easysewer; assert Path(easysewer.__file__).resolve().is_relative_to(Path(' + repr(package) + ')); '
                'from test_native_v2_climate_data import NativeClimateDataTests; '
                'print(json.dumps(NativeClimateDataTests().solve(sys.argv[1], "missing_month", '
                'Path(sys.argv[2]).read_text(encoding="utf-8"), allow_error=True)))')
            result=subprocess.run([sys.executable,'-I','-B','-c',code,directory,str(source)],capture_output=True,text=True,timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            error,report=json.loads(result.stdout)
            self.assertNotEqual(error,0)
            self.assertIn('climate',report.lower())
            # A zero-count month is valid. Climate I/O revision 1 also accepts
            # its complete final record without a newline; older engines do not.
            path.write_bytes(b'DLY12345600TMAX  2020019999000\n')
            model.update_climate(file=replace(model.climate.file,start_date=date(2020,1,30)))
            zero=self.solve(directory,'zero_count',model.to_document().text)
            self.assertTrue(all(v==70 for v in zero['temperature']))
            path.write_bytes(b'DLY12345600TMAX  2020019999000')
            source.write_text(model.to_document().text,encoding='utf-8')
            result=subprocess.run([sys.executable,'-I','-B','-c',code,directory,str(source)],capture_output=True,text=True,timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            from easysewer.runtime._solver_api import SWMMSolverAPI
            lib=SWMMSolverAPI().swmm
            if hasattr(lib,'swmm_getEasySewerClimateIO') and lib.swmm_getEasySewerClimateIO()==1:
                self.assertEqual(json.loads(result.stdout),json.loads(json.dumps(zero)))
            else:
                error,report=json.loads(result.stdout)
                self.assertEqual(error,339)


if __name__=='__main__':unittest.main()
