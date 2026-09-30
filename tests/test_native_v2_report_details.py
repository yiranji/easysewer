"""Printed observations against the independent native OUT API and LID files."""

from ctypes import byref
from datetime import time
from pathlib import Path
import tempfile
import unittest

from easysewer import get_native_capabilities
from easysewer.io.inp import InpDocument
from easysewer.io.report_details import table_series
from easysewer.model import Model, Ref
from easysewer.model import climate as c
from easysewer.model.quality import Pollutant
from easysewer.model.report import ReportSelection
from easysewer.model.surface import SlottedInlet
from easysewer.results import ResultTable, ResultSeries
from easysewer.runtime import Runner
from test_runner_v2 import config
from test_native_v2_report import OPTIONS, check_tables
from test_native_v2_quality import literal_quality
from test_native_v2_groundwater import literal_groundwater
from test_native_v2_lid import literal_source, TYPE_LAYERS
from test_native_v2_surface import surface_network
from test_hydrology_v2 import hydrology_model, snowpack
from test_climate_v2 import add_series
from test_flexible_v2 import ponding_model, configuration


def compare_details(test,result):
    from easysewer.runtime._output_api import SWMMOutputAPI
    native=SWMMOutputAPI();queries=0;report=result.report_reader(on_error='raise')
    try:
        native.open(result.output.path);periods=native.get_times(1)
        with result.open_output() as output:
            for target in report.targets:
                table=report.detail(target)
                test.assertEqual(table.status,'present');test.assertEqual(len(table.rows),periods)
                test.assertEqual(table.source.sha256,result.report.sha256)
                test.assertEqual(table.source.run_id,result.run_id)
                test.assertEqual(ResultTable.from_json_document(table.to_json_document()),table)
                definitions={v.key:v for v in output.available_variables(target.collection)}
                index=output.metadata.names(target.collection).index(target.key)
                method={'swmm:subcatchments':native.get_subcatch_series,'swmm:nodes':native.get_node_series,
                        'swmm:links':native.get_link_series}[target.collection]
                for position,column in enumerate(table.columns[1:],1):
                    if column.key=='swmm:losses':
                        expected=tuple(a/24+b for a,b in zip(method(index,2,0,periods),method(index,3,0,periods)))
                        times=output.series(target,'swmm:infiltration').times
                    else:
                        code=definitions[column.key].code
                        if column.pollutant is not None:code+=output.metadata.names('swmm:pollutants').index(column.pollutant.key)
                        expected=tuple(method(index,code,0,periods))
                        times=output.series(target,column.key,pollutant=column.pollutant).times
                    series=report.series(target,column.key,pollutant=column.pollutant)
                    test.assertEqual(ResultSeries.from_json_document(series.to_json_document()),series)
                    for row,actual,value,when in zip(table.rows,series.values,expected,times):
                        cell=row.cells[position];digits=int(cell.precision.partition(':')[2])
                        test.assertAlmostEqual(actual,value,delta=.501*10**-digits,msg=str((target,column.key,column.pollutant,cell.raw,value)))
                        test.assertLessEqual(abs((row.cells[0].value-when).total_seconds()),.501)
                        line=result.report_document.text.splitlines()[cell.span.line-1]
                        test.assertEqual(line[cell.span.column-1:cell.span.end_column-1],cell.raw)
                    queries+=1
    finally:
        test.assertEqual(native.lib.SMO_close(byref(native.handle)),0)
    return queries


@unittest.skipUnless(get_native_capabilities()['swmm_solver'] and get_native_capabilities()['swmm_output'],'Native solver/output unavailable')
class NativeReportDetailsTests(unittest.TestCase):
    def test_all_units_and_averages_use_saved_observations_and_captured_names(self):
        with tempfile.TemporaryDirectory() as directory:
            for units in ('CFS','GPM','MGD','CMS','LPS','MLD'):
                for averages in (False,True):
                    with self.subTest(units=units,averages=averages):
                        model=Model.from_document(InpDocument.from_text(literal_quality(units=units)),strict=True)
                        model.nodes.rename('J','池γ😀');model.update_report(averages=averages)
                        result=Runner().run(model,config(Path(directory)/(units+str(averages)),report_read=OPTIONS))
                        check_tables(self,result)
                        model.nodes.rename('池γ😀','Changed');model.pollutants.rename('Q0','ChangedQuality')
                        self.assertEqual(compare_details(self,result),23)
                        self.assertEqual(result.report_reader().detail(Ref(collection='swmm:nodes',key='Changed')).status,'absent')

    def test_multiple_quality_blocks_touching_headers_and_actual_system_names(self):
        with tempfile.TemporaryDirectory() as directory:
            for ignore in (False,True):
                model=Model.from_document(InpDocument.from_text(literal_quality()),strict=True)
                model.nodes.rename('O','System');model.subcatchments.rename('S','swmm:system')
                model.pollutants.rename('Q0','*LongQuality_甲');model.pollutants.rename('Q1','LongQuality_乙')
                for i in range(5):
                    model.pollutants.add(Pollutant(id='ExtraPollutant_'+str(i),units='#/L' if i==4 else 'MG/L',
                        rainfall_concentration=7,groundwater_concentration=0,rdii_concentration=0,decay_rate=0,initial_concentration=3))
                model.update_options(ignore_quality=ignore)
                result=Runner().run(model,config(Path(directory)/str(ignore),report_read=OPTIONS));check_tables(self,result)
                table=result.report_table('swmm:outfall_loading')
                self.assertEqual(table.row('System').target.key,'System')
                self.assertIsNone(table.row(('swmm:system',)).target)
                self.assertEqual(len(table.columns),11)
                if ignore:
                    self.assertEqual(result.report_table('swmm:quality_routing_continuity').status,'absent')
                else:
                    self.assertEqual(len(result.report_table('swmm:quality_routing_continuity').columns),7)
                    self.assertEqual(len(result.report_table('swmm:runoff_quality_continuity').columns),7)
                    self.assertIsNotNone(result.report_table('swmm:subcatchment_washoff').row('swmm:system').target)
                compare_details(self,result)

    def test_groundwater_snow_optional_columns_and_month_rollover(self):
        with tempfile.TemporaryDirectory() as directory:
            for units in ('CFS','CMS'):
                for kind in ('groundwater','snow'):
                    with self.subTest(units=units,kind=kind):
                        if kind=='groundwater':model=Model.from_document(InpDocument.from_text(literal_groundwater(units=units)),strict=True)
                        else:
                            model=hydrology_model();model.snowpacks.add(snowpack())
                            model.subcatchments.update('S',snowpack=Ref(collection='swmm:snowpacks',key='Snow'))
                            air=add_series(model,'Air',((0,25),(24,25),(48,45),(72,45)))
                            model.update_climate(temperature=c.SeriesTemperature(series=air),wind=c.MonthlyWindSpeeds(values=(8.,)*12))
                            if units=='CMS':model.convert_units(units)
                        model.update_report(subcatchments=ReportSelection(mode='ALL'),nodes=ReportSelection(mode='ALL'),links=ReportSelection(mode='ALL'))
                        result=Runner().run(model,config(Path(directory)/(units+kind),report_read=OPTIONS));check_tables(self,result)
                        compare_details(self,result)
                        if kind=='groundwater':
                            self.assertEqual(result.report_table('swmm:groundwater').status,'present')
                            self.assertEqual(result.report_table('swmm:groundwater_continuity').status,'present')
                        else:
                            series=result.report_reader().series(Ref(collection='swmm:subcatchments',key='S'),'swmm:snow_depth')
                            self.assertGreater(max(series.values),0);self.assertEqual(series.times[-1].month,2)

    def test_all_lid_types_details_and_repeated_deployments_have_separate_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            for units in ('CFS','CMS'):
                for kind in TYPE_LAYERS:
                    with self.subTest(units=units,kind=kind):
                        source=literal_source(kind,units,detail='lid.txt')
                        source+='[LID_USAGE]\nS L 1 200 20 20 10 0 "other.txt" * 10\n'
                        model=Model.from_document(InpDocument.from_text(source),strict=True)
                        result=Runner().run(model,config(Path(directory)/(units+kind),report_read=OPTIONS));check_tables(self,result)
                        performance=result.report_table('swmm:lid_performance')
                        self.assertEqual(len(performance.rows),2)
                        self.assertNotEqual(performance.rows[0].key,performance.rows[1].key)
                        self.assertTrue(all(row.target is None for row in performance.rows))
                        model.lid_controls.rename('L','Changed')
                        for artifact in result.artifacts:
                            if artifact.role!='swmm:lid-detail':continue
                            table=result.read_lid_report(artifact.owner,on_error='raise')
                            self.assertEqual(table.status,'present');self.assertGreater(len(table.rows),10)
                            self.assertEqual(table.source.sha256,artifact.sha256)
                            self.assertEqual(table.source.input_sha256,result.snapshot.input_sha256)
                            self.assertEqual(ResultTable.from_json_document(table.to_json_document()),table)
                            self.assertEqual(table.columns[2].unit,'in/hr' if units=='CFS' else 'mm/hr')
                            self.assertTrue(all(row.target==artifact.owner for row in table.rows))
                            self.assertTrue(all(row.cells[3].precision=='decimal:4' for row in table.rows))
                            self.assertGreater(max(table_series(table,'swmm:inflow').values),0)
                            lines=artifact.read_bytes().decode('utf-8').splitlines()
                            for row in table.rows:
                                for cell in row.cells:
                                    self.assertEqual(lines[cell.span.line-1][cell.span.column-1:cell.span.end_column-1],cell.raw)
                        self.assertEqual(sum(a.role=='swmm:lid-detail' for a in result.artifacts),2)

    def test_archived_lid_hash_and_empty_dry_output(self):
        with tempfile.TemporaryDirectory() as directory:
            model=Model.from_document(InpDocument.from_text(literal_source('RB',saturation=0,detail='dry.txt')),strict=True)
            model.update_options(end_time=time(1))
            result=Runner().run(model,config(Path(directory)/'dry',report_read=OPTIONS));check_tables(self,result)
            artifact=next(a for a in result.artifacts if a.role=='swmm:lid-detail')
            table=result.read_lid_report(artifact.owner,on_error='raise')
            self.assertEqual(table.status,'empty');self.assertEqual(table_series(table,'swmm:inflow').values,())
            self.assertEqual(table.target,artifact.owner);self.assertEqual(table_series(table,'swmm:inflow').target,artifact.owner)
            with Path(artifact.path).open('ab') as stream:stream.write(b'changed')
            with self.assertRaisesRegex(ValueError,'changed'):result.read_lid_report(artifact.owner)

    def test_streets_with_flow_dry_and_without_inlet_keep_optional_cells(self):
        with tempfile.TemporaryDirectory() as directory:
            for units in ('CFS','CMS'):
                for kind in ('flow','dry','no-inlet'):
                    with self.subTest(units=units,kind=kind):
                        model=surface_network(SlottedInlet(length=2,width=.2))
                        if kind=='no-inlet':model.inlet_usage.remove('P')
                        if kind=='dry':model.nodes.update('J',initial_depth=0)
                        if units=='CMS':model.convert_units(units)
                        result=Runner().run(model,config(Path(directory)/(units+kind),report_read=OPTIONS));check_tables(self,result)
                        street=result.report_table('swmm:street_flow');row=street.row('P')
                        self.assertEqual(len(row.cells),12)
                        if kind=='no-inlet':self.assertTrue(all(cell.value is None for cell in row.cells[3:]))
                        elif kind=='dry':self.assertTrue(all(cell.value is None for cell in row.cells[6:]))
                        else:self.assertTrue(all(cell.value is not None for cell in row.cells))

    @unittest.skipUnless(get_native_capabilities()['flexible_ponding'],'Custom native solver unavailable')
    def test_custom_backend_detail_provenance_matches_adjusted_saved_values(self):
        with tempfile.TemporaryDirectory() as directory:
            result=Runner().run(ponding_model(),configuration(Path(directory)/'custom',report_read=OPTIONS))
            check_tables(self,result);compare_details(self,result)
            series=result.report_reader().series(Ref(collection='swmm:nodes',key='J'),'swmm:overflow')
            self.assertIn('adjusted external/system flooding',series.semantics)


if __name__=='__main__':unittest.main()
