"""Full native OUT-library comparisons and captured result ownership."""

from ctypes import byref
from datetime import date, timedelta
import json
from pathlib import Path
import tempfile
import unittest

from easysewer import get_native_capabilities
from easysewer.io.inp import InpDocument
from easysewer.io.output import NotRecordedError, OutputChangedError, OutputReader
from easysewer.model import Model, Ref
from easysewer.model.quality import Pollutant
from easysewer.model.report import ReportSelection
from easysewer.results import ResultSeries
from easysewer.runtime import Runner
from test_runner_v2 import config
from test_native_v2_quality import literal_quality
from test_flexible_v2 import ponding_model, configuration


def compare_native(test, result):
    from easysewer.runtime._output_api import SWMMOutputAPI
    native=SWMMOutputAPI()
    queries=0
    try:
        native.open(result.output.path);count=native.get_times(1)
        with result.open_output() as reader, OutputReader(result.output.path) as raw_reader:
            test.assertEqual(count,reader.metadata.periods)
            for namespace,method in (('subcatchments',native.get_subcatch_series),('nodes',native.get_node_series),
                                     ('links',native.get_link_series),('system',None)):
                collection='swmm:'+namespace
                for index,name in enumerate((None,) if namespace=='system' else reader.metadata.names(collection)):
                    target=None if name is None else Ref(collection=collection,key=name)
                    for definition in reader.available_variables(collection):
                        for p,pollutant in enumerate(reader.metadata.names('swmm:pollutants') if definition.pollutant else (None,)):
                            quality=None if pollutant is None else Ref(collection='swmm:pollutants',key=pollutant)
                            series=reader.series(target,definition.key,pollutant=quality)
                            code=definition.code+p
                            expected=native.get_system_series(code,0,count) if method is None else method(index,code,0,count)
                            raw_series=raw_reader.series(target,definition.key,pollutant=quality)
                            test.assertEqual(raw_series.values,tuple(expected),(namespace,name,definition.key,pollutant))
                            if series.applicability.unavailable:
                                # This fixture has no groundwater bindings;
                                # native placeholder zeros are not elevations
                                # or moisture observations for this catchment.
                                if series.applicability.reasons==('swmm:groundwater-binding-absent',):
                                    test.assertEqual(namespace,'subcatchments')
                                    test.assertIn(definition.key,('swmm:groundwater_flow','swmm:groundwater_elevation','swmm:soil_moisture'))
                                else:
                                    test.assertEqual(series.applicability.reasons,('swmm:process-inactive',))
                                    test.assertEqual(result.engine_objects.names('swmm:subcatchments'),())
                                    test.assertEqual(namespace,'system')
                                    test.assertIn(definition.key,('swmm:rainfall','swmm:snow_depth','swmm:infiltration','swmm:runoff'))
                                test.assertTrue(all(value==0 for value in expected))
                                test.assertEqual(series.values,(None,)*count)
                            else:test.assertEqual(series.values,tuple(expected),(namespace,name,definition.key,pollutant))
                            test.assertEqual(series.source.sha256,result.output.sha256)
                            test.assertEqual(series.source.run_id,result.run_id)
                            test.assertEqual(series.times[0],reader.metadata.first_time)
                            test.assertEqual(series.times[-1],reader.metadata.last_time)
                            test.assertEqual(ResultSeries.from_json_document(series.to_json_document()),series)
                            queries+=1
    finally:
        test.assertEqual(native.lib.SMO_close(byref(native.handle)),0)
    return queries


@unittest.skipUnless(get_native_capabilities()['swmm_solver'] and get_native_capabilities()['swmm_output'],'Native solver/output unavailable')
class NativeOutputTests(unittest.TestCase):
    def success(self,result):
        self.assertTrue(result.succeeded,repr(result.failure)+' '+repr(result.diagnostics.errors))

    def test_all_native_variables_six_flow_units_and_three_pollutant_units(self):
        with tempfile.TemporaryDirectory() as directory:
            for units in ('CFS','GPM','MGD','CMS','LPS','MLD'):
                with self.subTest(units=units):
                    model=Model.from_document(InpDocument.from_text(literal_quality(units=units)),strict=True)
                    model.pollutants.add(Pollutant(id='Count',units='#/L',rainfall_concentration=7,
                        groundwater_concentration=0,rdii_concentration=0,decay_rate=0,initial_concentration=3))
                    result=Runner().run(model,config(Path(directory)/units));self.success(result)
                    self.assertEqual(compare_native(self,result),52)

    def test_report_subset_and_model_changes_cannot_reassign_saved_results(self):
        with tempfile.TemporaryDirectory() as directory:
            model=ponding_model()
            model.update_report(nodes=ReportSelection(mode='SELECTED',members=(Ref(collection='swmm:nodes',key='J'),)),
                links=ReportSelection(mode='NONE'))
            result=Runner().run(model,config(Path(directory)/'run'));self.success(result)
            model.nodes.rename('J','Renamed');model.convert_units('CMS')
            with result.open_output() as reader:
                series=reader.series(Ref(collection='swmm:nodes',key='j'),'swmm:depth')
                self.assertEqual(series.target.key,'J');self.assertEqual(series.unit,'ft')
                for collection,name,variable in (('swmm:nodes','O','swmm:depth'),('swmm:nodes','Renamed','swmm:depth'),('swmm:links','P','swmm:flow')):
                    with self.assertRaises(NotRecordedError):reader.series(Ref(collection=collection,key=name),variable)
            with Path(result.output.path).open('ab') as stream:stream.write(b'modified')
            with self.assertRaises(OutputChangedError):result.open_output()

    def test_averaging_context_and_unknown_standalone_context_are_persisted(self):
        with tempfile.TemporaryDirectory() as directory:
            model=ponding_model();series=[]
            for averages in (False,True):
                model.update_report(averages=averages)
                result=Runner().run(model,config(Path(directory)/str(averages)));self.success(result)
                compare_native(self,result)
                with result.open_output() as reader:
                    row=reader.series(Ref(collection='swmm:nodes',key='J'),'swmm:depth');series.append(row.values)
                    self.assertEqual(row.sampling,'routing-step-arithmetic-mean' if averages else 'report-time-interpolation')
                with OutputReader(result.output.path) as reader:
                    self.assertEqual(reader.series(Ref(collection='swmm:nodes',key='J'),'swmm:depth').sampling,'producer-unspecified')
                record=json.loads(result.artifact('run:execution-record').read_bytes())
                self.assertEqual(record['output']['averages'],averages)
                self.assertEqual(record['output']['producer'],'swmm:standard')
                self.assertTrue(record['output']['input_properties'])
            self.assertNotEqual(series[0],series[1])

    @unittest.skipUnless(get_native_capabilities()['flexible_ponding'],'Custom native solver unavailable')
    def test_custom_semantics_and_sampled_peak_remain_distinct_from_native_statistics(self):
        with tempfile.TemporaryDirectory() as directory:
            result=Runner().run(ponding_model(),configuration(Path(directory)/'run'));self.success(result)
            compare_native(self,result)
            with result.open_output() as reader:
                row=reader.series(Ref(collection='swmm:nodes',key='J'),'swmm:overflow')
                self.assertEqual(row.semantics,'easysewer:system-flooding')
                self.assertEqual(reader.series(None,'swmm:flooding').semantics,row.semantics)
                peak=reader.series(Ref(collection='swmm:nodes',key='J'),'swmm:depth').maximum()
                stats=next(row for row in result.backend_results.data['native_statistics'] if row['id']=='J')
                self.assertEqual(peak.statistic,'easysewer:saved-observation-maximum')
                self.assertNotAlmostEqual(peak.value,stats['maximum_depth'],places=6)

    def test_cross_day_report_window_uses_actual_saved_dates_and_chunks(self):
        with tempfile.TemporaryDirectory() as directory:
            model=ponding_model()
            model.update_options(end_date=date(2020,1,2),routing_step=timedelta(seconds=7),
                report_step=timedelta(hours=1),report_start_date=date(2020,1,1))
            result=Runner().run(model,config(Path(directory)/'run'));self.success(result)
            with result.open_output(max_buffer_bytes=64) as reader:
                chunks=list(reader.iter_chunks(Ref(collection='swmm:nodes',key='J'),'swmm:depth',chunk_size=3))
                combined=tuple(value for chunk in chunks for value in chunk.values)
                self.assertEqual(combined,reader.series(Ref(collection='swmm:nodes',key='J'),'swmm:depth').values)
                self.assertEqual(chunks[-1].times[-1].date(),date(2020,1,2))
                self.assertEqual(chunks[0].times[0].hour,1)


if __name__=='__main__':unittest.main()
