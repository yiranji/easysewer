"""H physical result plugin against both engines and data-only fresh readers."""
from dataclasses import replace
from decimal import Decimal
import hashlib,json,os,shutil,subprocess,sys,tempfile,unittest
from pathlib import Path
from easysewer import get_native_capabilities
from easysewer.io.json import JsonDocument
from easysewer.io.report import ReportLayoutError,swmm_report_tables
from easysewer.io.report_details import table_series
from easysewer.model import Ref
from easysewer.results import ResultTable,ResultSeries
from easysewer.runtime import Runner,RunResult,ReportReadOptions
from e_domain_fixture import model
from h_result_extension import KEY,METRIC,registry,parse_depth_changes
from test_runner_v2 import config
from test_native_v2_report import check_tables

EVIDENCE=[]
FAMILIES=('swmm:standard','easysewer:flexible-ponding')
NODE=Ref(collection='swmm:nodes',key='J')

def altered_parser(block,context,source):
    columns,rows=parse_depth_changes(block,context,source)
    return columns,tuple(replace(row,cells=tuple(replace(cell,value=cell.value+999)
        if index==3 and cell.value is not None else cell for index,cell in enumerate(row.cells))) for row in rows)

@unittest.skipUnless(get_native_capabilities()['swmm_solver'] and get_native_capabilities()['flexible_ponding'],
                    'Standard/custom native solvers unavailable')
class NativeHDomainTests(unittest.TestCase):
    def test_physical_extension_keeps_old_queries_native_values_and_portable_archives(self):
        import easysewer
        from test_native_v2_result_archive import NativeResultArchiveTests
        package=str(Path(easysewer.__file__).resolve().parent.parent)
        child='''import sys,json,hashlib
from pathlib import Path
package,archive,expected,resaved=sys.argv[1:]
sys.path.insert(0,package)
import easysewer
assert Path(easysewer.__file__).resolve().is_relative_to(Path(package).resolve())
from unittest.mock import patch
from easysewer.runtime import RunResult
from easysewer.io.report_details import table_series
from easysewer.results import ResultSeries
from easysewer.io.json import JsonDocument
with patch('ctypes.CDLL',side_effect=AssertionError('archive must not load native code')),patch('subprocess.Popen',side_effect=AssertionError('archive must not start worker')):
    result=RunResult.load(archive)
    data=json.loads(Path(expected).read_bytes())
    table=result.report_table('acceptance:node_depth_changes')
    assert table.to_json_document().to_bytes().decode()==data['table']
    series=table_series(table,'acceptance:depth_change')
    assert series.to_json_document().to_bytes().decode()==data['series']
    assert ResultSeries.from_json_document(JsonDocument.from_bytes(data['series'].encode()))==series
    assert hashlib.sha256(result.output.read_bytes()).hexdigest()==data['output']
    with result.open_output() as output:
        assert list(output.series(table.rows[0].target,'swmm:depth').values)==data['depth']
    result.save(resaved)
    assert RunResult.load(resaved).report_table(table.key)==table
assert 'h_result_extension' not in sys.modules
print(json.dumps(dict(no_plugin=True,no_native=True,rows=len(table.rows))))
'''
        for family in FAMILIES:
            for units in ('CFS','CMS'):
                for averages in (False,True):
                    with self.subTest(family=family,units=units,averages=averages),tempfile.TemporaryDirectory() as directory:
                        root=Path(directory).resolve();value=model();value.convert_units(units,basis='physical')
                        value.update_options(allow_ponding=True);value.update_report(averages=averages)
                        extended=registry();settings=ReportReadOptions(tables=extended.keys,on_table_error='raise')
                        result=Runner(report_tables=extended).run(value,config(root/'run',backend=family,report_read=settings))
                        check_tables(self,result);table=result.report_table(KEY);series=table_series(table,METRIC)
                        self.assertEqual(table.status,'present');self.assertEqual(series.unit,'ft' if units=='CFS' else 'm')
                        self.assertEqual(len(series.values),120);self.assertIsNone(series.values[0])
                        self.assertTrue(any(v>0 for v in series.values if v is not None))
                        self.assertTrue(any(v<0 for v in series.values if v is not None))
                        self.assertEqual(series.applicability.status,'computed')
                        self.assertIn('averages='+str(averages),series.semantics)
                        old=result.report_reader();new=result.report_reader(registry=extended)
                        for key in swmm_report_tables().keys:
                            self.assertEqual(old.table(key).to_json_document().to_bytes(),new.table(key).to_json_document().to_bytes())
                        self.assertEqual(old.detail(NODE),new.detail(NODE))
                        self.assertEqual(old.series(NODE,'swmm:depth'),new.series(NODE,'swmm:depth'))
                        with result.open_output() as output:depth=output.series(NODE,'swmm:depth')
                        # Compare actual printed operands and an independent OUT depth query.
                        for i,row in enumerate(table.rows):
                            self.assertAlmostEqual(row.cells[1].value,depth.values[i],delta=.000501)
                            self.assertLessEqual(abs((row.cells[0].value-depth.times[i]).total_seconds()),.501)
                            if i:
                                self.assertEqual(row.cells[2],table.rows[i-1].cells[1])
                                self.assertEqual(row.cells[3].value,float(Decimal(row.cells[1].raw)-Decimal(row.cells[2].raw)))
                                self.assertAlmostEqual(row.cells[3].value,depth.values[i]-depth.values[i-1],delta=.001002)
                        for version in ('1.1','1.2'):
                            self.assertEqual(ResultTable.from_json_document(table.to_json_document(version=version)),table)
                        self.assertEqual(ResultSeries.from_json_document(series.to_json_document()),series)
                        output_sha=result.output.sha256
                        if units=='CFS' and not averages:
                            baseline=Runner().run(value,config(root/'baseline',backend=family,
                                report_read=ReportReadOptions(tables=swmm_report_tables().keys,on_table_error='raise')))
                            check_tables(self,baseline);self.assertEqual(result.output.read_bytes(),baseline.output.read_bytes())
                        changed=result.report_reader(registry=registry(altered_parser)).table(KEY)
                        self.assertNotEqual(changed.rows[1].cells[3].value,table.rows[1].cells[3].value)
                        self.assertEqual(result.report_table(KEY),table)
                        result.save(root/'archive');NativeResultArchiveTests().assert_same(result,RunResult.load(root/'archive'))
                        expected=root/'expected.json'
                        expected.write_text(json.dumps(dict(table=table.to_json_document().to_bytes().decode(),
                            series=series.to_json_document().to_bytes().decode(),output=output_sha,depth=depth.values)),encoding='utf-8')
                        self.assertTrue((root/'run').resolve().is_relative_to(root));shutil.rmtree(root/'run')
                        (root/'archive').rename(root/'moved')
                        process=subprocess.run([sys.executable,'-I','-B','-c',child,package,str(root/'moved'),str(expected),str(root/'resaved')],
                            capture_output=True,text=True,timeout=90,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                        self.assertEqual(process.returncode,0,process.stdout+process.stderr)
                        self.assertEqual(json.loads(process.stdout),dict(no_plugin=True,no_native=True,rows=120))
                        EVIDENCE.append(dict(family=family,units=units,averages=averages,out_sha256=output_sha,
                            rows=120,builtin_tables_unchanged=43,no_plugin_fresh_reader=True,original_run_removed=True,
                            saved_metric_preserved_after_parser_change=True))

    def test_extension_layout_failure_is_explicit_and_raise_preserves_previous_publication(self):
        def unknown(*args):raise ReportLayoutError('future extension header')
        for family in FAMILIES:
            with self.subTest(family=family),tempfile.TemporaryDirectory() as directory:
                root=Path(directory);value=model();value.update_options(allow_ponding=True)
                preserve=Runner(report_tables=registry(unknown)).run(value,config(root/'preserve',backend=family,
                    report_read=ReportReadOptions(tables=('swmm:node_depth',KEY))))
                self.assertTrue(preserve.succeeded,preserve.failure)
                self.assertEqual(preserve.report_table(KEY).status,'unsupported_layout')
                self.assertTrue(preserve.report_table(KEY).raw_text)
                self.assertEqual(preserve.report_table('swmm:node_depth').status,'present')
                destination=root/'published';destination.mkdir();old=destination/'model.rpt';old.write_bytes(b'previous-success')
                failed=Runner(report_tables=registry(unknown)).run(value,config(destination,backend=family,overwrite=True,
                    report_read=ReportReadOptions(tables=('swmm:node_depth',KEY),on_table_error='raise')))
                self.assertEqual(failed.status,'failed');self.assertEqual(failed.failure.stage,'report_read')
                self.assertEqual(old.read_bytes(),b'previous-success');self.assertEqual(failed.report_tables,())
                failed.save(root/'failed');self.assertEqual(RunResult.load(root/'failed').failure,failed.failure)
                EVIDENCE.append(dict(family=family,kind='failure-policy',preserve_retains_raw=True,
                    raise_keeps_previous_publication=True,failure_archived=True))

if __name__=='__main__':unittest.main()
