"""Broader report layouts, composite identities and printed observation semantics."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from easysewer.io.report import ReportReader, ReportContext, ReportLayoutError, read_report_tables
from easysewer.io.report_document import ReportDocument
from easysewer.io.report_details import read_lid_report, table_series
from easysewer.io.json import JsonDocument
from easysewer.model import Ref
from easysewer.results import ResultTable
from test_report_v2 import block, table


NODE='''  <<< Node J >>>
  ----------------------------------------------------------------
  Inflow Flooding Depth Head
  Date Time CFS CFS feet feet
  ----------------------------------------------------------------
  01/01/2020 23:59:59 123456789.123-2.345     1.250     2.500
  01/02/2020 00:00:01 0.000 0.000 0.500 1.750
'''
LID='''SWMM5 LID Report File

Project:  fixture
LID Unit: L in Subcatchment S

Elapsed Total Total Surface Pavement Soil Storage Surface Drain Surface Pavement Soil Storage
Time Inflow Evap Infil Perc Perc Exfil Runoff OutFlow Level Level Moisture Level
Date Time Hours in/hr in/hr in/hr in/hr in/hr in/hr in/hr in/hr inches inches Content inches
----------- -------- --------- ---------
01/01/2020 00:00:01 0.000 1.234 0.0123 2.000 3.000 4.000 5.000 6.000 7.000 8.000 9.000 0.300 11.000
01/01/2020 01:00:00 1.000 1.234 0.0123 2.000 3.000 4.000 5.000 6.000 7.000 8.000 9.000 0.300 11.000
'''


def quality_block(names,values):
    fmt=lambda name:' '*max(0,14-len(name.encode()))+name
    return ('  '+'*'*26+''.join(map(fmt,names))+'\n  Quality Routing Continuity'+''.join(fmt('kg') for _ in names)+
            '\n  '+'*'*26+''.join('    ----------' for _ in names)+'\n  External Inflow ..........'+
            ''.join(f'{value:14.3f}' for value in values)+'\n  Continuity Error (%) .....'+''.join(f'{0:14.3f}' for _ in names)+'\n\n')


class ReportDetailsTests(unittest.TestCase):
    def test_empty_table_retains_owner_and_older_json_without_table_target_restores(self):
        text=NODE[:NODE.index('  01/01/2020')]
        reader=ReportReader(ReportDocument.from_bytes(text.encode()),on_error='raise')
        ref=Ref(collection='swmm:nodes',key='j')
        result=reader.detail(ref)
        self.assertEqual(result.status,'empty');self.assertEqual(result.target.key,'J')
        self.assertEqual(reader.series(ref,'swmm:depth').target,result.target)
        self.assertEqual(ResultTable.from_json_document(result.to_json_document()),result)
        legacy=result.to_json_document().data;legacy.pop('target')
        restored=ResultTable.from_json_document(JsonDocument.from_data(legacy))
        self.assertIsNone(restored.target);self.assertEqual(restored.status,'empty')
        with self.assertRaises(ValueError):table_series(result,'swmm:depth',target=Ref(collection='swmm:nodes',key='Other'))

    def test_touching_values_calendar_rollover_and_printed_precision(self):
        reader=ReportReader(ReportDocument.from_bytes(NODE.encode()),on_error='raise')
        target=Ref(collection='swmm:nodes',key='j');detail=reader.detail(target)
        self.assertEqual(detail.rows[0].cells[1].value,123456789.123)
        self.assertEqual(detail.rows[0].cells[2].value,-2.345)
        self.assertEqual(detail.rows[1].cells[0].value,datetime(2020,1,2,0,0,1))
        self.assertEqual(detail.rows[0].cells[1].precision,'decimal:3')
        series=reader.series(target,'swmm:depth')
        self.assertEqual(series.values,(1.25,.5));self.assertEqual(series.target.key,'J')
        self.assertEqual(series.times[1]-series.times[0],timedelta(seconds=2))
        for row in detail.rows:
            for cell in row.cells:
                self.assertEqual(NODE.splitlines()[cell.span.line-1][cell.span.column-1:cell.span.end_column-1],cell.raw)
        self.assertEqual(ResultTable.from_json_document(detail.to_json_document()),detail)
        with self.assertRaises(ValueError):replace(detail.rows[0].cells[0],value=datetime.now(timezone.utc))

    def test_duplicate_rounded_timestamps_are_rows_and_never_invented_fractions(self):
        source=NODE.replace('01/02/2020 00:00:01','01/01/2020 23:59:59')
        reader=ReportReader(ReportDocument.from_bytes(source.encode()),on_error='raise')
        target=Ref(collection='swmm:nodes',key='J')
        detail=reader.detail(target)
        self.assertEqual(len(detail.rows),2);self.assertNotEqual(detail.rows[0].key,detail.rows[1].key)
        with self.assertRaises(ReportLayoutError):reader.series(target,'swmm:depth')
        self.assertEqual(ResultTable.from_json_document(detail.to_json_document()),detail)

    def test_missing_and_unrecognized_detail_are_explicit(self):
        reader=ReportReader(ReportDocument.from_bytes(NODE.encode()))
        self.assertEqual(reader.detail(Ref(collection='swmm:nodes',key='Missing')).status,'absent')
        for text in (NODE.replace('Depth Head','New Depth Head'),NODE.replace('1.250','NaN'),
                     NODE.replace('01/02/2020','12/31/2019'),NODE+NODE):
            result=ReportReader(ReportDocument.from_bytes(text.encode())).detail(Ref(collection='swmm:nodes',key='J'))
            self.assertEqual(result.status,'unsupported_layout');self.assertEqual(result.rows,())
            self.assertIn('<<< Node J >>>',result.raw_text)

    def test_quality_blocks_join_by_identity_and_wide_names_need_producer_context(self):
        names=('abcdefghijklmno','pqrstuvwxyzABCDE','Q2','Q3','Q4','Q5','Q6')
        source=quality_block(names[:5],range(1,6))+quality_block(names[5:],range(6,8))
        doc=ReportDocument.from_bytes(source.encode())
        key='swmm:quality_routing_continuity'
        self.assertEqual(read_report_tables(doc,(key,))[0].status,'unsupported_layout')
        result=read_report_tables(doc,(key,),context=ReportContext(pollutants=names),on_error='raise')[0]
        self.assertEqual(len(result.columns),7)
        self.assertEqual(tuple(c.value for c in result.row('swmm:external_inflow').cells),tuple(range(1,8)))
        self.assertEqual(ResultTable.from_json_document(result.to_json_document()),result)
        duplicate=ReportDocument.from_bytes((quality_block(('A',),(1,))*2).encode())
        self.assertEqual(read_report_tables(duplicate,(key,))[0].status,'unsupported_layout')
        mismatch=source.replace('External Inflow','External Outflow',1)
        self.assertEqual(read_report_tables(ReportDocument.from_bytes(mismatch.encode()),(key,),context=ReportContext(pollutants=names))[0].status,'unsupported_layout')

    def test_wide_summary_numbers_interface_notice_and_duplicate_detail_columns(self):
        source=quality_block(('A','B'),(1234567890123,1234567890124))
        result=table(source,'swmm:quality_routing_continuity',on_error='raise')
        self.assertEqual(tuple(c.value for c in result.row('swmm:external_inflow').cells),(1234567890123,1234567890124))
        text='  '+'*'*26+'\n  Runoff Quantity Continuity\n  '+'*'*26+'\n  Runoff supplied by interface file data folder/cache.runoff\n'
        result=table(text,'swmm:runoff_quantity_continuity',on_error='raise')
        self.assertEqual(result.cell('swmm:source','swmm:runoff_interface').value,'data folder/cache.runoff')
        self.assertEqual(result.columns[0].kind,'text')
        duplicate=NODE.replace('Depth Head','Depth Head Q q').replace('feet feet','feet feet MG/L MG/L')
        result=ReportReader(ReportDocument.from_bytes(duplicate.encode())).detail(Ref(collection='swmm:nodes',key='J'))
        self.assertEqual(result.status,'unsupported_layout')

    def test_system_aggregate_cannot_shadow_an_object_called_system(self):
        source=block('Subcatchment Washoff Summary','              Q\n Subcatchment kg',
            ' System 1.000\n swmm:system 2.000\n '+'-'*90+'\n System 3.000')
        result=table(source,'swmm:subcatchment_washoff',on_error='raise')
        pollutant=Ref(collection='swmm:pollutants',key='Q')
        self.assertEqual(result.cell('System','swmm:load',pollutant=pollutant).value,1)
        self.assertEqual(result.cell('swmm:system','swmm:load',pollutant=pollutant).value,2)
        self.assertEqual(result.cell(('swmm:system',),'swmm:load',pollutant=pollutant).value,3)
        self.assertIsNone(result.row(('swmm:system',)).target)
        self.assertEqual(ResultTable.from_json_document(result.to_json_document()),result)

    def test_repeated_lid_deployments_are_not_collapsed_or_bound_to_invented_model_ids(self):
        header='Total Evap Infil Surface Drain Initial Final Continuity\nInflow Loss Loss Outflow Outflow Storage Storage Error\nSubcatchment LID Control in in in in in in in %'
        result=table(block('LID Performance Summary',header,' S L 1 2 3 4 5 6 7 8\n S L 11 12 13 14 15 16 17 18'),
                     'swmm:lid_performance',on_error='raise')
        self.assertEqual(len(result.rows),2)
        self.assertEqual(result.rows[0].key,('swmm:lid_occurrence','S','L','1'))
        self.assertEqual(result.rows[1].key,('swmm:lid_occurrence','S','L','2'))
        self.assertTrue(all(r.target is None for r in result.rows))
        self.assertEqual(ResultTable.from_json_document(result.to_json_document()),result)

    def test_routing_frequencies_and_rankings_keep_units_and_object_categories(self):
        source='''  *************************
  Routing Time Step Summary
  *************************
  Minimum Time Step : 0.50 sec
  Average Time Step : 1.50 sec
  Maximum Time Step : 3.00 sec
  % of Time in Steady State : 1.00
  Average Iterations per Step : 2.00
  % of Steps Not Converging : 0.00
  Time Step Frequencies :
    0.500 - 1.000 sec : 20.00 %
    1.000 - 3.000 sec : 80.00 %
'''
        result=table(source,'swmm:routing_time_step',on_error='raise')
        self.assertEqual(result.cell('swmm:minimum_time_step','swmm:value').unit,'s')
        self.assertEqual(result.rows[-1].cells[0].unit,'%')
        self.assertEqual(result.rows[-1].cells[1].value,1)
        text='  ***************************\n  Time-Step Critical Elements\n  ***************************\n  Node Same (10.00%)\n  Link Same (20.00%)\n'
        ranked=table(text,'swmm:time_step_critical_elements',on_error='raise')
        self.assertEqual(ranked.cell(Ref(collection='swmm:links',key='Same'),'swmm:value').value,20)
        self.assertEqual(ResultTable.from_json_document(ranked.to_json_document()),ranked)

    def test_lid_sparse_file_units_hash_budget_empty_and_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'lid.txt';path.write_bytes(LID.encode())
            result=read_lid_report(path,on_error='raise',expected_subcatchment='S',expected_control='L')
            self.assertEqual(len(result.rows),2)
            self.assertEqual(result.rows[0].cells[3].precision,'decimal:4')
            self.assertEqual(result.columns[3].unit,'in/hr')
            series=table_series(result,'swmm:surface_level')
            self.assertEqual(len(series.values),2);self.assertEqual(series.times[1]-series.times[0],timedelta(seconds=3599))
            self.assertIn('dry-compressed',series.sampling)
            self.assertEqual(ResultTable.from_json_document(result.to_json_document()),result)
            with self.assertRaises(ValueError):read_lid_report(path,expected_sha256='a'*64)
            with self.assertRaises(ValueError):read_lid_report(path,max_bytes=20)
            with self.assertRaises(ReportLayoutError):read_lid_report(path,expected_control='Wrong',on_error='raise')
            path.write_bytes(LID[:LID.index('01/01/2020')].encode())
            owner=Ref(collection='swmm:lid_usage',key='placement')
            empty=read_lid_report(path,on_error='raise',owner=owner)
            self.assertEqual(empty.status,'empty');self.assertEqual(table_series(empty,'swmm:inflow').target,owner)
            path.write_bytes(LID.replace('Content','Unknown').encode())
            self.assertEqual(read_lid_report(path).status,'unsupported_layout')


if __name__=='__main__':unittest.main()
