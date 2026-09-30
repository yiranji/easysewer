"""H independent physical result extension and old data contracts."""
from dataclasses import replace
import unittest
from easysewer.io.json import JsonDocument
from easysewer.io.report import ReportContext,ReportLayoutError,ReportReader,read_report_tables,swmm_report_tables
from easysewer.io.report_document import ReportDocument
from easysewer.io.report_details import table_series
from easysewer.model import Ref
from easysewer.results import ResultTable,ResultSeries
from easysewer.results.applicability import ResultContext
from h_result_extension import KEY,METRIC,LITERAL,registry,codec,parse_depth_changes
from test_report_v2 import DEPTH

def parsed(text=LITERAL,*,on_error='raise',tables=None,context=None):
    document=ReportDocument.from_bytes(text.encode(),source='independent.rpt')
    return read_report_tables(document,(KEY,),registry=tables or registry(),context=context,on_error=on_error)[0]

class HDomainTests(unittest.TestCase):
    def test_registered_metric_units_missing_first_sample_raw_origins_and_old_queries(self):
        base=swmm_report_tables();extended=registry()
        self.assertEqual(extended.keys[:-1],base.keys);self.assertEqual(len(base.keys),43)
        for source,unit in ((LITERAL,'ft'),(LITERAL.replace('CFS CFS feet feet','CMS CMS meters meters'),'m')):
            table=parsed(source);series=table_series(table,METRIC)
            self.assertEqual(series.values,(None,.25,-.75));self.assertEqual(series.unit,unit)
            self.assertEqual(series.target,Ref(collection='swmm:nodes',key='J'))
            self.assertEqual(series.missing,('acceptance:no_previous_observation',None,None))
            self.assertEqual(series.maximum().value,.25);self.assertEqual(series.maximum().missing_count,1)
            self.assertIn('not an instantaneous rate',series.semantics)
            self.assertEqual(series.applicability.status,'unknown')
            for row in table.rows:
                for cell in row.cells:
                    if cell.span:
                        self.assertEqual(source.splitlines()[cell.span.line-1][cell.span.column-1:cell.span.end_column-1],cell.raw)
                    else:self.assertEqual(cell.raw,'')
            self.assertEqual(table.rows[1].cells[2],table.rows[0].cells[1])
        doc=ReportDocument.from_bytes((DEPTH+LITERAL).encode(),source='with-detail.rpt')
        old=read_report_tables(doc,base.keys,registry=base)
        new=read_report_tables(doc,base.keys+(KEY,),registry=extended)
        self.assertEqual([v.to_json_document().to_bytes() for v in old],[v.to_json_document().to_bytes() for v in new[:-1]])
        self.assertEqual(ReportReader(doc).detail(Ref(collection='swmm:nodes',key='J')),
            ReportReader(doc,registry=extended).detail(Ref(collection='swmm:nodes',key='J')))
        self.assertNotIn(KEY,base.keys)
        with self.assertRaises(ValueError):extended.with_codecs(codec())
        with self.assertRaises(ValueError):extended.with_codecs(replace(codec(),key='acceptance:other'))

    def test_extension_tables_and_series_use_current_and_supported_legacy_json(self):
        table=parsed();series=table_series(table,METRIC)
        for version in ('1.1','1.2'):
            self.assertEqual(ResultTable.from_json_document(table.to_json_document(version=version)),table)
        legacy=table.to_json_document(version='1.1').data
        legacy['schema_version']='1.0';legacy.pop('applicability')
        for column in legacy['columns']:column.pop('applicability')
        self.assertEqual(ResultTable.from_json_document(JsonDocument.from_data(legacy)),table)
        self.assertEqual(ResultSeries.from_json_document(series.to_json_document()),series)
        old=series.to_json_document().data;old['schema_version']='1.0';old.pop('applicability')
        self.assertEqual(ResultSeries.from_json_document(JsonDocument.from_data(old)),series)

    def test_unfamiliar_layout_keeps_bytes_and_never_shifts_or_invents_values(self):
        for source in (LITERAL.replace('Depth Head','Depth Extra Head'),
            LITERAL.replace('1.500','NaN'),LITERAL.replace('1.500','bad'),
            LITERAL.replace('feet feet','furlongs furlongs'),LITERAL+LITERAL,
            LITERAL.replace('00:01:00','00:00:00').replace('02/01/2020 00:00:00 0.000','01/01/2020 00:00:00 0.000')):
            with self.subTest(source=source):
                document=ReportDocument.from_bytes(source.encode())
                table=read_report_tables(document,(KEY,),registry=registry())[0]
                self.assertEqual(table.status,'unsupported_layout');self.assertFalse(table.rows)
                self.assertEqual(document.raw,source.encode());self.assertEqual(table.raw_text,source)
                with self.assertRaises(ReportLayoutError):parsed(source)
        empty=parsed(LITERAL[:LITERAL.index('  01/31')]);self.assertEqual(empty.status,'empty')
        absent=parsed('no report tables\n');self.assertEqual(absent.status,'absent')
        duplicate=parsed(LITERAL.replace('02/01/2020 00:00:00','01/31/2020 23:59:00'))
        self.assertEqual(len(duplicate.rows),3)
        with self.assertRaises(ReportLayoutError):table_series(duplicate,METRIC)
        marker=RuntimeError('plugin bug')
        def broken(*args):raise marker
        with self.assertRaises(RuntimeError) as caught:parsed(tables=registry(broken),on_error='preserve')
        self.assertIs(caught.exception,marker)

    def test_metric_inherits_hydraulic_scope_without_claiming_an_unknown_process(self):
        for process,status in (('computed','computed'),('inactive','not_applicable')):
            context=ReportContext(result_context=ResultContext(policy='swmm:result-context:1',
                facts=(('swmm:routing',process),),evidence=('fixture:captured-input',)))
            table=parsed(context=context);series=table_series(table,METRIC)
            self.assertEqual(series.applicability.status,status)
            self.assertEqual(ResultSeries.from_json_document(series.to_json_document()),series)
            self.assertEqual(ResultTable.from_json_document(table.to_json_document()),table)
            if process=='inactive':
                self.assertEqual(series.values,(None,None,None))
                self.assertEqual(table.rows[1].cells[1].raw,'1.500')
            else:self.assertEqual(series.values,(None,.25,-.75))

if __name__=='__main__':unittest.main()
