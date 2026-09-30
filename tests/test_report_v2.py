"""Independent RPT grammar fixtures, byte provenance and extension contracts."""

from dataclasses import replace
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
import tempfile
import tracemalloc
import unittest

from easysewer.io.json import JsonDocument
from easysewer.io.report import (ReportContext, ReportLayoutError, ReportTableCodec,
    read_report_tables, swmm_report_tables)
from easysewer.io.report_document import ReportDocument, SWMM_UTF8_REPORT
from easysewer.model import Ref
from easysewer.results import ResultCell, ResultColumn, ResultRow, ResultTable
from easysewer.runtime import ReportReadOptions, RunConfig, Runner
from test_runner_v2 import config
from test_options_v2 import network


def block(title, header, rows):
    return '\n  '+('*'*len(title))+'\n  '+title+'\n  '+('*'*len(title))+'\n\n  '+'-'*90+'\n'+header+'\n  '+'-'*90+'\n'+rows+'\n\n'


DEPTH_HEADER='''  Average Maximum Maximum Time of Max Reported
  Depth Depth HGL Occurrence Max Depth
  Node Type Feet Feet Feet days hr:min Feet'''
DEPTH=block('Node Depth Summary',DEPTH_HEADER,'  A JUNCTION 0.50 1.25 11.25 2 03:04 1.00\n  B OUTFALL 0.00 0.00 10.00 0 00:00 0.00')
STORAGE_HEADER='''  Average Avg Evap Exfil Maximum Max Time of Max Maximum
  Volume Pcnt Pcnt Pcnt Volume Pcnt Occurrence Outflow
  Storage Unit 1000 ft³ Full Loss Loss 1000 ft³ Full days hr:min CFS'''
STORAGE=block('Storage Volume Summary',STORAGE_HEADER,'  池γ 0.221 0.9 0.0 0.0 0.454 1.8 0 08:00 0.91')
LINK_HEADER=''' Maximum Time of Max Maximum Max/ Max/
 |Flow| Occurrence |Veloc| Full Full
 Link Type CFS days hr:min ft/sec Flow Depth'''


def table(text, key='swmm:node_depth', **kwargs):
    return read_report_tables(ReportDocument.from_bytes(text.encode()),(key,),**kwargs)[0]


class ReportTests(unittest.TestCase):
    def test_profile_repairs_only_source_defined_headers_and_preserves_utf8_identifiers(self):
        raw=STORAGE.encode().replace(b'\xc2\xb3',b'\xb3')
        self.assertIsNone(ReportDocument.from_bytes(raw).text)
        document=ReportDocument.from_bytes(raw,profile=SWMM_UTF8_REPORT)
        self.assertEqual(document.raw,raw);self.assertEqual(document.text,STORAGE)
        self.assertEqual(len(document.repaired_byte_offsets),2)
        self.assertTrue(all(raw[i]==0xb3 for i in document.repaired_byte_offsets))
        result=read_report_tables(document,('swmm:storage_volume',))[0]
        self.assertEqual(result.cell('池γ','swmm:maximum_volume').value,.454)
        self.assertEqual(result.cell('池γ','swmm:maximum_volume').unit,'1000 ft3')
        self.assertEqual(result.source.sha256,hashlib.sha256(raw).hexdigest())
        for damaged in (raw+b'\n\xb3 arbitrary text',raw.replace(b'Storage Unit',b'Future Unit')):
            self.assertIsNone(ReportDocument.from_bytes(damaged,profile=SWMM_UTF8_REPORT).text)
        explicit=ReportDocument.from_bytes(raw,encoding='ascii',profile=SWMM_UTF8_REPORT)
        self.assertIsNone(explicit.text)
        with self.assertRaises(UnicodeDecodeError):
            ReportDocument.from_bytes(raw,encoding='ascii',profile=SWMM_UTF8_REPORT,on_decode_error='raise')
        old=ReportDocument.from_bytes(b'Caf\xe9',encoding='cp1252')
        self.assertEqual(old.text,'Café');self.assertEqual(old.decoding_strategy,'strict')
        utf8=ReportDocument.from_bytes(STORAGE.encode(),profile=SWMM_UTF8_REPORT)
        self.assertEqual(utf8.repaired_byte_offsets,());self.assertEqual(utf8.decoding_strategy,'strict')

    def test_catalog_retains_unknown_sections_detail_blocks_and_native_messages(self):
        text=DEPTH+'  ************\n  Future Table\n  ************\n  new columns 99\n\n'
        text+='  <<< Node A >>>\n Date Time Inflow\n 01/01/2020 00:00:00 1\n'
        text+='  WARNING 02: maximum depth increased\n  ERROR 101: insufficient memory\n'
        doc=ReportDocument.from_bytes(text.encode(),source='report.rpt')
        self.assertEqual(''.join(b.text for b in doc.blocks),text)
        self.assertTrue(any(b.title=='Future Table' for b in doc.blocks))
        detail=next(b for b in doc.blocks if b.kind=='detail')
        self.assertEqual(detail.target,Ref(collection='swmm:nodes',key='A'))
        self.assertEqual([d.code for d in doc.messages],['swmm.warning.02','swmm.error.101'])
        for d in doc.messages:
            self.assertIn(d.message,text.splitlines()[d.span.line-1])

    def test_byte_budget_closes_files_and_invalid_budgets_do_not_disable_limits(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'report.rpt';path.write_bytes(b'x'*21)
            with self.assertRaises(ValueError):ReportDocument.read(path,max_bytes=20)
            path.rename(path.with_suffix('.renamed'))
        for limit in (0,-1,True,1.5):
            with self.assertRaises(ValueError):ReportDocument.from_bytes(b'',max_bytes=limit)
        with self.assertRaises(ValueError):ReportDocument.from_bytes(b'x'*21,max_bytes=20)

    def test_small_report_does_not_preallocate_the_default_byte_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'small.rpt';path.write_bytes(DEPTH.encode())
            tracemalloc.start()
            try:
                document=ReportDocument.read(path)
                _,peak=tracemalloc.get_traced_memory()
            finally:tracemalloc.stop()
            self.assertEqual(document.text,DEPTH)
            self.assertLess(peak,1024*1024)

    def test_depth_times_units_positions_and_portable_json_keep_native_meaning(self):
        origin=datetime(2020,1,3,12)
        result=table(DEPTH,context=ReportContext(report_start=origin))
        self.assertEqual(result.status,'present')
        self.assertEqual(result.cell(Ref(collection='swmm:nodes',key='a'),'swmm:maximum_depth').value,1.25)
        peak=result.cell('A','swmm:peak_time')
        self.assertEqual(peak.value,timedelta(days=2,hours=3,minutes=4))
        self.assertEqual(peak.precision,'minute:1');self.assertEqual(result.time_origin,origin)
        for row in result.rows:
            for cell in row.cells:
                self.assertEqual(DEPTH.splitlines()[cell.span.line-1][cell.span.column-1:cell.span.end_column-1],cell.raw)
        self.assertEqual(ResultTable.from_json_document(result.to_json_document()),result)
        self.assertIn('routing-step',result.columns[1].semantics)
        self.assertEqual(result.cell('B','swmm:maximum_depth').value,0)
        with self.assertRaises(KeyError):result.cell('C','swmm:maximum_depth')

    def test_empty_absent_undecoded_and_bad_layout_never_become_zero(self):
        empty='  *********************\n  Node Flooding Summary\n  *********************\n\n  No nodes were flooded.\n'
        self.assertEqual(table(empty,'swmm:node_flooding').status,'empty')
        self.assertEqual(table(DEPTH,'swmm:node_flooding').status,'absent')
        value=read_report_tables(ReportDocument.from_bytes(b'\xff'),('swmm:node_depth',))[0]
        self.assertEqual(value.status,'undecoded');self.assertEqual(value.rows,())
        for changed in (DEPTH.replace('Max Depth','New Column'),DEPTH.replace('1.25','NaN'),
                        DEPTH.replace('03:04','99:04'),DEPTH.replace('11.25 2','11.25 7 2'),
                        DEPTH.replace('B OUTFALL','a OUTFALL'),DEPTH+DEPTH):
            result=table(changed)
            self.assertEqual(result.status,'unsupported_layout');self.assertEqual(result.rows,())
            self.assertIn('Node Depth Summary',result.raw_text)
            with self.assertRaises(ReportLayoutError):table(changed,on_error='raise')

    def test_link_optional_slots_censoring_and_long_ids_cannot_shift_values(self):
        identifier='long_object_identifier_γ'
        rows=(f' {identifier} CONDUIT 3.00 1 02:03'+f'{">50.00":>10}{".30":>8}{".40":>8}'+'\n'
              ' Pump PUMP 2.00 0 00:01'+' '*10+f'{".50":>8}'+'\n'
              ' Weir WEIR 1.00 0 00:02'+' '*18+f'{".75":>8}'+'\n'
              ' Outlet DUMMY 0.00 0 00:00')
        result=table(block('Link Flow Summary',LINK_HEADER,rows),'swmm:link_flow',on_error='raise')
        velocity=result.cell(identifier,'swmm:maximum_velocity')
        self.assertEqual((velocity.value,velocity.qualifier),(50.,'greater_than'))
        self.assertEqual(result.cell('Pump','swmm:maximum_full_flow').value,.5)
        self.assertIsNone(result.cell('Pump','swmm:maximum_velocity').value)
        self.assertEqual(result.cell('Weir','swmm:maximum_full_depth').value,.75)
        self.assertIsNotNone(result.cell('Outlet','swmm:maximum_full_depth').missing_reason)
        broken=block('Link Flow Summary',LINK_HEADER,rows+' extra')
        self.assertEqual(table(broken,'swmm:link_flow').status,'unsupported_layout')

    def test_inflow_absolute_error_unit_and_significant_precision_are_cell_specific(self):
        header='''Maximum Maximum Lateral Total Flow
Lateral Total Time of Max Inflow Inflow Balance
Inflow Inflow Occurrence Volume Volume Error
Node Type CFS CFS days hr:min 10^6 gal 10^6 gal Percent'''
        result=table(block('Node Inflow Summary',header,' A JUNCTION 1.00 2.00 0 00:00 1.23e+05 0.123 -0.100 gal'),
                     'swmm:node_inflow',on_error='raise')
        self.assertEqual(result.cell('A','swmm:flow_balance_error').unit,'gal')
        self.assertEqual(result.cell('A','swmm:total_inflow_volume').precision,'significant:3')
        self.assertEqual(result.columns[-1].unit,'%')

    def test_flooding_dynamic_wave_depth_and_other_route_volume_are_distinct(self):
        for end,unit,key in (('Depth','Meters','maximum_ponded_depth'),('Volume','1000 m³','maximum_ponded_volume')):
            header=f'Total Maximum\nMaximum Time of Max Flood Ponded\nHours Rate Occurrence Volume {end}\nNode Flooded CMS days hr:min 10^6 ltr {unit}'
            result=table(block('Node Flooding Summary',header,' J .01 1.234 1 00:02 .345 2.678'),'swmm:node_flooding',on_error='raise')
            self.assertEqual(result.cell('J','swmm:'+key).value,2.678)
            self.assertEqual(result.cell('J','swmm:flooding_volume').unit,'10^6 ltr')
        raw=block('Node Flooding Summary',header,' J .01 1.234 1 00:02 .345 2.678').encode().replace(b'\xc2\xb3',b'\xb3')
        self.assertIsNotNone(ReportDocument.from_bytes(raw,profile=SWMM_UTF8_REPORT).text)

    def test_quality_balance_pollutant_identity_log_count_and_percent_override(self):
        text='''  **************************         Count         Mass
  Quality Routing Continuity          LogN           kg
  **************************    ----------   ----------
  External Inflow ..........         3.000        2.000
  Continuity Error (%) .....         0.123       -0.010
'''
        result=table(text,'swmm:quality_routing_continuity',on_error='raise')
        count=Ref(collection='swmm:pollutants',key='count')
        self.assertEqual(result.cell('swmm:external_inflow','swmm:load',pollutant=count).unit,'LogN')
        self.assertEqual(result.cell('swmm:continuity_error','swmm:load',pollutant=count).unit,'%')
        self.assertEqual(result.cell('swmm:external_inflow','swmm:load',pollutant=count).value,3)
        self.assertEqual(ResultTable.from_json_document(result.to_json_document()),result)
        with self.assertRaises(KeyError):result.cell('swmm:external_inflow','swmm:load')
        water='''  **************************        Volume        Volume
  Flow Routing Continuity        acre-feet      10^6 gal
  **************************     ---------     ---------
  External Inflow ..........         3.000         0.978
  Continuity Error (%) .....         0.100
'''
        value=table(water,'swmm:flow_routing_continuity')
        self.assertIsNone(value.cell('swmm:continuity_error','swmm:scaled_volume').value)
        self.assertEqual(value.cell('swmm:external_inflow','swmm:volume').unit,'acre-ft')
        self.assertEqual(table(water.replace('External Inflow','Future Quantity'),'swmm:flow_routing_continuity').status,'unsupported_layout')

    def test_table_registry_extension_uses_existing_query_and_json_contract(self):
        base=swmm_report_tables();original=table(DEPTH).to_json_document().to_bytes()
        def parser(block,context,source):
            self.assertEqual(block.title,'Future Diagnostic')
            return ((ResultColumn(key='example:score',unit=None,kind='number',semantics='test extension score'),),
                    (ResultRow(key='system',label='System',target=None,cells=(ResultCell(value=7,raw='7',unit=None,precision='decimal:0',span=None),)),))
        codec=ReportTableCodec(key='example:future',title='Future Diagnostic',parser=parser)
        registry=base.with_codecs(codec)
        text=DEPTH+'  *****************\n  Future Diagnostic\n  *****************\n  score 7\n'
        results=read_report_tables(ReportDocument.from_bytes(text.encode()),('swmm:node_depth','example:future'),registry=registry)
        self.assertEqual(results[1].cell('system','example:score').value,7)
        self.assertEqual(ResultTable.from_json_document(results[1].to_json_document()),results[1])
        self.assertEqual(table(DEPTH,registry=registry).to_json_document().to_bytes(),original)
        self.assertNotIn('example:future',base.keys)
        with self.assertRaises(ValueError):registry.with_codecs(codec)
        with self.assertRaises(ValueError):registry.with_codecs(replace(codec,key='example:other'))

    def test_value_invariants_and_config_schema_roundtrip(self):
        base=table(DEPTH)
        for changes in ({'value':None},{'value':float('nan')},{'missing_reason':'invented'}, {'qualifier':'approximately'}):
            with self.assertRaises((ValueError,TypeError)):replace(base.rows[0].cells[1],**changes)
        with self.assertRaises(ValueError):replace(base,rows=base.rows+base.rows)
        with self.assertRaises(ValueError):replace(base,columns=base.columns+base.columns)
        with self.assertRaises(ValueError):replace(base,time_origin=datetime.now().astimezone())
        options=ReportReadOptions(tables=('swmm:node_depth',),on_table_error='raise',max_bytes=4096)
        configuration=config('run',report_read=options)
        self.assertEqual(RunConfig.from_json_document(configuration.to_json_document()).report_read,options)
        from easysewer.io.json.run import run_schema
        self.assertEqual(json.loads((Path(__file__).resolve().parents[1]/'docs/run-1.0.schema.json').read_text()),run_schema())
        for changes in ({'tables':('swmm:node_depth',)*2},{'max_bytes':0},{'max_bytes':True},{'on_table_error':'ignore'}):
            with self.assertRaises((ValueError,TypeError)):replace(options,**changes)

    def test_unregistered_table_rejected_before_engine_or_workspace_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            target=Path(directory)/'run'
            result=Runner().run(network(),config(target,report_read=ReportReadOptions(tables=('missing:table',))))
            self.assertEqual(result.status,'rejected');self.assertFalse(result.native_completed)
            self.assertIsNone(result.backend);self.assertFalse(target.exists())


if __name__=='__main__':unittest.main()
