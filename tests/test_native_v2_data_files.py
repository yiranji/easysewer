"""Native comparisons for actual external data reads, canonical writes and edits."""

from dataclasses import replace
from datetime import date, datetime, time, timedelta
from pathlib import Path
import tempfile
import unittest

from easysewer import get_native_capabilities
from easysewer.io.rainfall import RainfallData
from easysewer.io.timeseries import TimeSeriesData
from easysewer.model import FileReference, Ref
from easysewer.model.hydrology import FileRainfall
from easysewer.model.inflows import FlowInflow
from easysewer.model.resources import FileTimeSeries
from easysewer.runtime import check_files, StandardBackend
from test_hydrology_v2 import hydrology_model
import test_native_v2_files as native_files
from test_options_v2 import network


@unittest.skipUnless(get_native_capabilities()['swmm_solver'] and get_native_capabilities()['swmm_output'],'Native solver/output unavailable')
class NativeDataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.backend_capabilities = StandardBackend().probe().capabilities

    def solve(self,directory,name,source):
        return native_files.NativeFilesTests.solve(self,directory,name,source)

    def inflow(self,path):
        m=native_files.selected(network())
        m.update_options(start_time=time(12),end_time=time(12,10))
        m.timeseries.add(FileTimeSeries(id='Input',file=FileReference(path=str(path))))
        m.inflows.add(FlowInflow(node=Ref(collection='swmm:nodes',key='J'),series=Ref(collection='swmm:timeseries',key='Input')))
        return m

    def test_elapsed_calendar_and_continuation_match_inline_hydraulics(self):
        cases=(b'0 .1\n0.08333333333333333 .9\n0.16666666666666666 .1\n',
               b'-1 .1\n0 .2\n0:05 .9\n0:10 .1\n',
               b'; values\n0:00 .1\n0:05 .9\n0:10 .1\n',
               b'01/01/2020 12:00 .1\n12:05 .9\n12:10 .1\n')
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'input.dat'
            for data in cases:
                with self.subTest(data=data[:25]):
                    path.write_bytes(data);m=self.inflow(path)
                    self.assertTrue(check_files(m,backend_capabilities=self.backend_capabilities).complete)
                    expected=self.solve(directory,'original',m.to_document().text)
                    document=TimeSeriesData.read(path);document.write(path,normalize=True)
                    self.assertEqual(expected,self.solve(directory,'canonical',m.to_document().text))
                    m.timeseries.replace('Input',document.as_inline('Input'))
                    self.assertEqual(expected,self.solve(directory,'inline',m.to_document().text))
                    self.assertGreater(max(expected['series'][1][0][4]),.5)

    def test_mixed_date_rewind_matches_explicit_calendar_resolution(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'input.dat';path.write_bytes(b'0 0\n01/01/2020 12:05 1\n12:10 0\n')
            m=self.inflow(path)
            self.assertTrue(check_files(m,backend_capabilities=self.backend_capabilities).complete)
            original=self.solve(directory,'mixed',m.to_document().text)
            document=TimeSeriesData.read(path).resolved(m.effective_options.start)
            document.write(path)
            fixed=self.solve(directory,'dated',m.to_document().text)
            self.assertEqual(original,fixed)
            self.assertTrue(check_files(m,backend_capabilities=self.backend_capabilities).complete)
            m.timeseries.replace('Input',document.as_inline('Input'))
            self.assertEqual(fixed,self.solve(directory,'inline',m.to_document().text))

    def test_edited_series_changes_actual_inflow_and_preserves_original_model(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);path=root/'input.dat';path.write_bytes(b'0 .1\n0:05 .8\n0:10 .1\n')
            m=self.inflow(path);original=m.to_json_document().to_bytes()
            before=self.solve(root,'before',m.to_document().text)
            data=TimeSeriesData.read(path)
            edited=replace(data,points=tuple(replace(p,value=p.value*2) for p in data.points))
            edited.write(root/'edited.dat')
            changed=m.copy();changed.timeseries.update('Input',file=FileReference(path=str(root/'edited.dat')))
            after=self.solve(root,'after',changed.to_document().text)
            oracle=m.copy();oracle.timeseries.replace('Input',edited.as_inline('Input'))
            self.assertEqual(after,self.solve(root,'oracle',oracle.to_document().text))
            self.assertNotEqual(before['series'],after['series'])
            self.assertEqual(m.to_json_document().to_bytes(),original)
            self.assertEqual(path.read_bytes(),b'0 .1\n0:05 .8\n0:10 .1\n')

    def test_user_rainfall_six_unit_form_cases_native_read_rewrite_and_edit(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);path=root/'rain.dat'
            for units in ('CFS','CMS'):
                for form in ('INTENSITY','VOLUME','CUMULATIVE'):
                    with self.subTest(units=units,form=form):
                        lines=[];accumulated=0
                        for hour in range(73):
                            stamp=datetime(2020,1,30)+timedelta(hours=hour)
                            value=.6 if 2<=hour<8 or 48<=hour<52 else 0
                            if form=='CUMULATIVE':accumulated+=value;value=accumulated
                            if units=='CMS':value*=25.4
                            lines.extend((f'Unused {stamp:%Y %m %d %H %M} 0',f'Station {stamp:%Y %m %d %H %M} {value!r}'))
                        path.write_text('\n'.join(lines)+'\n',encoding='ascii')
                        model=native_files.selected(hydrology_model(form=form));model.reinterpret_units(units)
                        model.update_options(routing_step=timedelta(seconds=60))
                        model.raingages.update('R',source=FileRainfall(file=FileReference(path=str(path)),station='Station',units='MM' if units=='CMS' else 'IN'))
                        self.assertTrue(check_files(model).complete)
                        expected=self.solve(root,'original',model.to_document().text)
                        data=RainfallData.read(path);data.write(path,normalize=True)
                        self.assertEqual(expected,self.solve(root,'canonical',model.to_document().text))
                        self.assertGreater(max(expected['series'][0][0][4]),0)
                        # An independent literal-file oracle for a deliberate edit.
                        edited=replace(data,readings=tuple(replace(r,value=r.value/2) for r in data.readings))
                        edited.write(root/'edited.dat')
                        model.raingages.update('R',source=replace(model.raingages['R'].source,file=FileReference(path=str(root/'edited.dat'))))
                        actual=self.solve(root,'edited',model.to_document().text)
                        oracle=[' '.join(row.split()[:-1]+[repr(float(row.split()[-1])/2)]) for row in lines]
                        (root/'edited.dat').write_text('\n'.join(oracle)+'\n',encoding='ascii')
                        self.assertEqual(actual,self.solve(root,'oracle',model.to_document().text))
                        self.assertNotEqual(actual['series'],expected['series'])

    def test_extended_rainfall_clock_keeps_the_native_file_start_date_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);path=root/'rain.dat'
            path.write_bytes(b'Station 2020 1 30 24 0 1\nStation 2020 1 31 1 0 .2\n')
            model=native_files.selected(hydrology_model())
            model.update_options(routing_step=timedelta(seconds=60))
            model.raingages.update('R',source=FileRainfall(file=FileReference(path=str(path)),station='Station',units='IN',start_date=date(2020,1,31)))
            original=self.solve(root,'original',model.to_document().text)
            data=RainfallData.read(path);data.write(path,normalize=True)
            self.assertEqual(original,self.solve(root,'canonical',model.to_document().text))
            self.assertEqual(dict(check_files(model).checks[0].inspection.facts)['readings'],1)
            changed=replace(data,readings=tuple(replace(r,record_date=None) for r in data.readings))
            changed.write(path)
            self.assertNotEqual(original['series'],self.solve(root,'changed',model.to_document().text)['series'])


if __name__=='__main__':unittest.main()
