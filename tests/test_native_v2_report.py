"""RPT contracts exercised against bundled engines, printed bytes and native stats."""

from datetime import date, datetime, time, timedelta
import json
from pathlib import Path
import tempfile
import unittest

from easysewer import get_native_capabilities
from easysewer.io.inp import InpDocument
from easysewer.io.json import JsonDocument
from easysewer.io.report import ReportLayoutError, ReportTableCodec, read_report_tables, swmm_report_tables
from easysewer.io.report_document import ReportDocument
from easysewer.model import Model, Ref
from easysewer.model.quality import Pollutant
from easysewer.results import ResultCell, ResultColumn, ResultRow, ResultTable
from easysewer.runtime import FlexiblePondingPolicy, ReportReadOptions, Runner
from test_runner_v2 import config
from test_native_v2_quality import literal_quality
from test_flexible_v2 import configuration, ponding_model, SOURCE
from test_native_v2_ponding_accounting import sealed_model
from test_regulators_v2 import regulator_model, KINDS


OPTIONS=ReportReadOptions(tables=swmm_report_tables().keys,on_table_error='raise')


def check_tables(test, result):
    test.assertTrue(result.succeeded,repr(result.failure)+' '+repr(result.diagnostics.errors))
    test.assertIsNotNone(result.report_document.text)
    test.assertEqual(result.report_document.raw,result.report.read_bytes())
    record=json.loads(result.artifact('run:execution-record').read_bytes())
    test.assertEqual(record['report']['sha256'],result.report.sha256)
    count=0
    lines=result.report_document.text.splitlines()
    for table in result.report_tables:
        test.assertIn(table.status,('present','absent','empty'))
        test.assertEqual(table.source.sha256,result.report.sha256)
        test.assertEqual(table.source.path,result.report.path)
        test.assertEqual(table.source.run_id,result.run_id)
        test.assertEqual(table.source.input_sha256,result.snapshot.input_sha256)
        test.assertEqual(table.source.backend_sha256,result.backend.sha256)
        test.assertEqual(table.time_origin,result.snapshot.options.report_start)
        artifact=result.artifact('run:report-table',field=(table.key,))
        test.assertEqual(ResultTable.from_json_document(JsonDocument.from_bytes(artifact.read_bytes())),table)
        for row in table.rows:
            for cell in row.cells:
                if cell.span is not None:
                    line=lines[cell.span.line-1]
                    test.assertEqual(line[cell.span.column-1:cell.span.end_column-1],cell.raw)
                    test.assertEqual(cell.span.source,result.report.path)
                count+=1
    return count


@unittest.skipUnless(get_native_capabilities()['swmm_solver'],'Native solver unavailable')
class NativeReportTests(unittest.TestCase):
    def test_six_flow_units_mixed_utf8_headers_quality_and_persisted_cells(self):
        with tempfile.TemporaryDirectory() as directory:
            for units in ('CFS','GPM','MGD','CMS','LPS','MLD'):
                with self.subTest(units=units):
                    model=Model.from_document(InpDocument.from_text(literal_quality(units=units)),strict=True)
                    model.pollutants.add(Pollutant(id='Count',units='#/L',rainfall_concentration=7,
                        groundwater_concentration=0,rdii_concentration=0,decay_rate=0,initial_concentration=3))
                    name='池γ😀';model.nodes.rename('J',name)
                    result=Runner().run(model,config(Path(directory)/units,report_read=OPTIONS))
                    self.assertGreater(check_tables(self,result),90)
                    self.assertEqual(result.report_document.decoding_strategy,'swmm-volume-header-normalization')
                    self.assertEqual(len(result.report_document.repaired_byte_offsets),2)
                    self.assertEqual(result.report_table('swmm:node_depth').row(name).target.key,name)
                    self.assertEqual(result.report_table('swmm:node_inflow').cell(name,'swmm:maximum_total_inflow').unit,units)
                    self.assertEqual(result.report_table('swmm:node_flooding').status,'empty')
                    self.assertEqual(result.report_table('swmm:storage_volume').cell(name,'swmm:maximum_volume').unit,
                                     '1000 ft3' if units in ('CFS','GPM','MGD') else '1000 m3')
                    quality=result.report_table('swmm:quality_routing_continuity')
                    self.assertEqual(quality.cell('swmm:external_inflow','swmm:load',pollutant=Ref(collection='swmm:pollutants',key='Count')).unit,'LogN')
                    errors=[cell.value for cell in quality.row('swmm:continuity_error').cells]
                    self.assertAlmostEqual(max(errors,key=abs),result.mass_balance.quality_percent,delta=.0006)
                    self.assertTrue(any(b.kind=='detail' and b.target.key==name for b in result.report_document.blocks))

    @unittest.skipUnless(get_native_capabilities()['flexible_ponding'],'Custom solver unavailable')
    def test_custom_native_stats_match_printed_peaks_volumes_and_report_start(self):
        with tempfile.TemporaryDirectory() as directory:
            for units in ('CFS','CMS'):
                for averages in (False,True):
                    model=ponding_model(units)
                    model.update_options(report_start_date=date(2020,1,1),report_start_time=time(0,0,20))
                    model.update_report(averages=averages)
                    result=Runner().run(model,configuration(Path(directory)/(units+str(averages)),report_read=OPTIONS))
                    check_tables(self,result)
                    stats=next(row for row in result.backend_results.data['native_statistics'] if row['id']=='J')
                    depths=result.report_table('swmm:node_depth');flood=result.report_table('swmm:node_flooding')
                    self.assertAlmostEqual(depths.cell('J','swmm:maximum_depth').value,stats['maximum_depth'],delta=.0051)
                    self.assertAlmostEqual(depths.cell('J','swmm:mean_depth').value,stats['routing_step_mean_depth'],delta=.0051)
                    self.assertAlmostEqual(flood.cell('J','swmm:maximum_flooding_flow').value,stats['maximum_flooding_flow'],delta=.0051 if units=='CFS' else .00051)
                    scaled=stats['flooding_volume']*(7.48e-6 if units=='CFS' else .001)
                    self.assertAlmostEqual(flood.cell('J','swmm:flooding_volume').value,scaled,delta=.00051)
                    self.assertIn('discrete external removal',flood.columns[1].semantics)
                    for native,table in (('maximum_depth_date',depths),('maximum_flooding_date',flood)):
                        seconds=max(0,(datetime(1899,12,30)+timedelta(days=stats[native])-result.snapshot.options.report_start).total_seconds())
                        printed=table.cell('J','swmm:peak_time').value.total_seconds()
                        self.assertLessEqual(abs(printed-seconds),60.5)

    @unittest.skipUnless(get_native_capabilities()['flexible_ponding'],'Custom solver unavailable')
    def test_multi_day_peak_is_relative_to_report_start_and_not_simulation_start(self):
        with tempfile.TemporaryDirectory() as directory:
            model=sealed_model(pollutants=False,with_pump=True)
            model.update_options(end_date=date(2020,1,3),end_time=time(2),report_start_date=date(2020,1,1),report_start_time=time(1),
                                 routing_step=timedelta(seconds=60),report_step=timedelta(hours=1))
            policy=FlexiblePondingPolicy(external_flooding_ratio=0,record_steps=False)
            result=Runner().run(model,configuration(Path(directory)/'days',policy,report_read=OPTIONS))
            check_tables(self,result)
            stats=next(row for row in result.backend_results.data['native_statistics'] if row['id']=='J')
            peak=result.report_table('swmm:node_depth').cell('J','swmm:peak_time')
            actual=datetime(1899,12,30)+timedelta(days=stats['maximum_depth_date'])
            self.assertGreaterEqual(peak.value,timedelta(days=2))
            self.assertLess(abs((actual-result.snapshot.options.report_start-peak.value).total_seconds()),60.5)
            self.assertGreater(abs((actual-result.snapshot.options.start-peak.value).total_seconds()),3500)

    def test_every_regulator_optional_slot_and_large_identifier(self):
        with tempfile.TemporaryDirectory() as directory:
            for kind in KINDS:
                with self.subTest(kind=kind):
                    model=regulator_model(kind);model.links.rename('P','A_very_long_link_name_γ')
                    result=Runner().run(model,config(Path(directory)/kind.replace('/','-'),report_read=OPTIONS))
                    check_tables(self,result)
                    row=result.report_table('swmm:link_flow').row('A_very_long_link_name_γ')
                    self.assertEqual(len(row.cells),6)
                    if kind.startswith('PUMP'):
                        self.assertIsNone(row.cells[3].value);self.assertIsNotNone(row.cells[4].value)
                    elif kind in ('BOTTOM','FUNCTIONAL/HEAD','FUNCTIONAL/DEPTH','TABULAR/HEAD','TABULAR/DEPTH'):
                        self.assertTrue(all(cell.value is None for cell in row.cells[3:]))

    def test_non_dynamic_flood_volume_header_and_disabled_report_missingness(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            model=Model.from_document(InpDocument.from_text(SOURCE.replace('DYNWAVE','KINWAVE')),strict=True)
            result=Runner().run(model,config(root/'kinematic',report_read=OPTIONS));check_tables(self,result)
            flood=result.report_table('swmm:node_flooding')
            self.assertEqual(flood.status,'present')
            self.assertEqual(flood.cell('J','swmm:maximum_ponded_volume').unit,'1000 ft3')
            self.assertIn('swmm-volume-header-normalization',result.report_document.decoding_strategy)
            model=ponding_model();model.update_report(disabled=True)
            result=Runner().run(model,config(root/'disabled',report_read=OPTIONS));check_tables(self,result)
            self.assertTrue(all(t.status=='absent' for t in result.report_tables if t.key!='swmm:analysis_timing'))
            self.assertEqual(result.report_table('swmm:analysis_timing').status,'present')

    def test_layout_failure_and_byte_budget_preserve_previous_publication(self):
        def unsupported(*args):raise ReportLayoutError('Future header fixture')
        registry=type(swmm_report_tables())((ReportTableCodec(key='swmm:node_depth',title='Node Depth Summary',parser=unsupported),))
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            preserved=Runner(report_tables=registry).run(ponding_model(),config(root/'preserved',report_read=ReportReadOptions(tables=('swmm:node_depth',))))
            self.assertTrue(preserved.succeeded,repr(preserved.failure))
            self.assertEqual(preserved.report_table('swmm:node_depth').status,'unsupported_layout')
            for name,options in (('strict',ReportReadOptions(tables=('swmm:node_depth',),on_table_error='raise')),
                                 ('budget',ReportReadOptions(max_bytes=1))):
                target=root/name;target.mkdir();old=target/'model.rpt';old.write_bytes(b'previous')
                result=Runner(report_tables=registry).run(ponding_model(),config(target,overwrite=True,report_read=options,keep_failed_artifacts=False))
                self.assertFalse(result.succeeded);self.assertEqual(result.failure.stage,'report_read')
                self.assertEqual(old.read_bytes(),b'previous');self.assertEqual(result.report_tables,())
                self.assertIsNone(result.report_document)

    def test_registered_new_table_works_in_runner_without_changing_old_queries(self):
        def parser(block,context,source):
            raw=next(line.strip() for line in block.text.splitlines() if 'VERSION' in line)
            return ((ResultColumn(key='example:banner',unit=None,kind='text',semantics='printed engine banner'),),
                    (ResultRow(key='system',label='System',target=None,cells=(ResultCell(value=raw,raw=raw,unit=None,precision='text',span=None),)),))
        codec=ReportTableCodec(key='example:engine_banner',title='Report preamble',block_kind='preamble',parser=parser)
        registry=swmm_report_tables().with_codecs(codec)
        with tempfile.TemporaryDirectory() as directory:
            result=Runner(report_tables=registry).run(ponding_model(),config(Path(directory)/'extended',
                report_read=ReportReadOptions(tables=('swmm:node_depth','example:engine_banner'),on_table_error='raise')))
            check_tables(self,result)
            self.assertIn('VERSION',result.report_table(codec.key).cell('system','example:banner').value)
            self.assertEqual(result.report_table('swmm:node_depth').columns[2].key,'swmm:maximum_depth')


if __name__=='__main__':unittest.main()
