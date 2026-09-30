"""Independent status grammars, identities, clocks and unsupported-layout guards."""

from datetime import datetime, timedelta
import hashlib
from pathlib import Path
import tempfile
import unittest

from easysewer.io.report import ReportContext, ReportLayoutError, ReportReader, read_report_tables
from easysewer.io.report_document import ReportCapture, ReportDocument
from easysewer.model import Ref
from easysewer.results import ResultTable


def block(title,rows,header=None):
    text='\n  '+len(title)*'*'+'\n  '+title+'\n  '+len(title)*'*'+'\n'
    if header is not None:text+=header+'\n  '+'-'*80+'\n'
    return text+rows+'\n\n'


OPTIONS=block('Analysis Options','''  Flow Units ............... CMS
  Process Models:
    Rainfall/Runoff ........ NO
    RDII ................... NO
    Snowmelt ............... NO
    Groundwater ............ NO
    Flow Routing ........... YES
    Ponding Allowed ........ NO
    Water Quality .......... NO
  Flow Routing Method ...... DYNWAVE
  Surcharge Method ......... EXTRAN
  Starting Date ............ 01/30/2020 23:59:00
  Ending Date .............. 02/01/2020 01:01:02
  Antecedent Dry Days ...... 2.5
  Report Time Step ......... 25:01:02
  Routing Time Step ........ 0.25 sec
  Variable Time Step ....... YES
  Maximum Trials ........... 8
  Number of Threads ........ 1
  Head Tolerance ........... 0.001524 m''')
RAIN_HEADER='  Station First Last Recording Periods Periods Periods\n  ID Date Date Frequency w/Precip Missing Malfunc.'
NODE_HEADER='  Invert Max. Ponded External\n  Name Type Elev. Depth Area Inflow'


def table(text,key,**options):
    result=read_report_tables(ReportDocument.from_bytes(text.encode(),source='fixture.rpt'),('swmm:'+key,),**options)[0]
    if result.status in ('present','empty'):
        assert ResultTable.from_json_document(result.to_json_document())==result
        lines=text.splitlines()
        for row in result.rows:
            for cell in row.cells:
                if cell.span:
                    assert lines[cell.span.line-1][cell.span.column-1:cell.span.end_column-1]==cell.raw
    return result


class ReportStatusTests(unittest.TestCase):
    def test_multiline_errors_echo_location_and_model_clock_are_explicit(self):
        text='  ERROR 211: invalid number bad at line 19 of [CONDUITS] section:\r\n  P A B bad .01\r\n'
        text+='  ERROR 201: too many characters in input line at line 7 of input file:\n  ERROR 999: this is input text\n'
        text+='  ERROR 173: Time Series Q has its data out of sequence. at 02/01/2020 01:02:03.\n'
        text+='  WARNING 02: maximum depth increased for Node J\n  ERROR 200 detected. Execution halted.\n'
        document=ReportDocument.from_bytes(text.encode(),source='failed.rpt')
        self.assertEqual(len(document.messages),5)
        first=document.message_contexts[0]
        self.assertEqual((first.input_line,first.input_section,first.input_text),(19,'CONDUITS','P A B bad .01'))
        self.assertEqual(first.input_report_span.line,2)
        self.assertEqual(first.input_report_span.source,'failed.rpt')
        self.assertEqual(first.raw_text,''.join(text.splitlines(keepends=True)[:2]))
        self.assertEqual(document.message_contexts[1].input_text,'ERROR 999: this is input text')
        self.assertIsNone(document.message_contexts[1].input_section)
        self.assertEqual(document.message_contexts[2].model_time,datetime(2020,2,1,1,2,3))
        self.assertEqual(document.messages[-1].code,'swmm.error.200')
        self.assertTrue(all(message.object_id is None for message in document.messages))

    def test_failure_capture_budget_hash_undecoded_prefix_and_file_closure(self):
        raw='  ERROR 211: invalid number β at line 19 of [CONDUITS] section:\n  P A B β .01\n'.encode()
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'failed.rpt';path.write_bytes(raw)
            for limit in (1,len(raw)-1,len(raw),len(raw)+1):
                capture=ReportCapture.read(path,max_bytes=limit)
                self.assertEqual(capture.document.raw,raw[:limit])
                self.assertEqual(capture.sha256,hashlib.sha256(raw[:limit]).hexdigest())
                self.assertEqual(capture.truncated,len(raw)>limit)
            limited=ReportCapture.read(path,max_bytes=raw.index('β'.encode())+1)
            self.assertTrue(limited.truncated);self.assertIsNone(limited.document.text)
            explicit=ReportCapture.read(path,encoding='ascii')
            self.assertEqual(explicit.document.raw,raw);self.assertIsNone(explicit.document.text)
            path.rename(path.with_suffix('.moved'))
            for invalid in (0,-1,True,1.5):
                with self.assertRaises(ValueError):ReportCapture.read(path,max_bytes=invalid)

    def test_effective_options_kinds_context_and_calendar_rollover(self):
        actual=table(OPTIONS,'analysis_options',on_error='raise')
        self.assertEqual(actual.cell('swmm:start','swmm:calendar').value,datetime(2020,1,30,23,59))
        self.assertEqual(actual.cell('swmm:end','swmm:calendar').value,datetime(2020,2,1,1,1,2))
        self.assertEqual(actual.cell('swmm:report_time_step','swmm:duration').value,timedelta(hours=25,minutes=1,seconds=2))
        self.assertEqual(actual.cell('swmm:routing_time_step','swmm:number').value,.25)
        self.assertEqual(actual.cell('swmm:routing_time_step','swmm:number').unit,'s')
        self.assertEqual(actual.cell('swmm:head_tolerance','swmm:number').unit,'m')
        self.assertIsNotNone(actual.cell('swmm:head_tolerance','swmm:calendar').missing_reason)
        node=block('Node Summary','  A JUNCTION 100.00 5.00 0.0',NODE_HEADER)
        self.assertEqual(table(OPTIONS+node,'input_nodes').cell('A','swmm:elevation').unit,'m')
        self.assertIsNone(table(node,'input_nodes').cell('A','swmm:elevation').unit)
        self.assertEqual(table(node,'input_nodes',context=ReportContext(flow_units='CFS')).cell('A','swmm:elevation').unit,'ft')
        with self.assertRaisesRegex(ValueError,'flow units differ'):
            table(OPTIONS,'analysis_options',context=ReportContext(flow_units='CFS'))
        for invalid in (OPTIONS.replace('0.001524 m','0.001524 ft'),OPTIONS.replace('........... 8','........... 8.5'),
                        OPTIONS.replace('01/30/2020','02/30/2020'),OPTIONS.replace('Routing Time Step','Future Time Step')):
            self.assertEqual(table(invalid,'analysis_options').status,'unsupported_layout')

    def test_station_occurrences_unknown_dates_counts_and_no_gage_guess(self):
        text=block('Rainfall File Summary','  shared 01/01/2020 02/01/2020 15 min 4 3 2\n  shared *********** *********** 60 min 0 0 0',RAIN_HEADER)
        actual=table(text,'rainfall_file',on_error='raise')
        self.assertEqual(len(actual.rows),2);self.assertNotEqual(actual.rows[0].key,actual.rows[1].key)
        self.assertTrue(all(row.target is None for row in actual.rows))
        self.assertEqual(actual.rows[0].cells[1].value,datetime(2020,1,1))
        self.assertEqual(actual.rows[1].cells[1].missing_reason,'native_date_unavailable')
        self.assertEqual(actual.rows[1].cells[4].value,0)
        self.assertEqual(table(text.replace('15 min','15.5 min'),'rainfall_file').status,'unsupported_layout')

    def test_gage_file_names_cannot_be_inferred_from_looks_like_series(self):
        text=block('Raingage Summary','  G Rain INTENSITY 60 min.','  Data Recording\n  Name Data Source Type Interval')
        unknown=table(text,'input_raingages').cell('G','swmm:data_source')
        self.assertEqual(unknown.missing_reason,'source_kind_not_proven')
        file=table(text,'input_raingages',context=ReportContext(rain_gage_sources=(('g','FILE'),)))
        self.assertEqual(file.cell('G','swmm:data_source').value,'Rain INTENSITY 60 min.')
        inline=table(text,'input_raingages',context=ReportContext(rain_gage_sources=(('G','TIMESERIES'),)))
        self.assertEqual(inline.cell('G','swmm:data_source').value,'Rain')
        self.assertEqual(inline.cell('G','swmm:recording_interval').value,60)
        with self.assertRaises(ValueError):ReportContext(rain_gage_sources=(('G','FILE'),('g','TIMESERIES')))

    def test_rdii_unit_override_zero_ratio_and_incompatible_header(self):
        for u,v,expected in (('acre-feet','gal','acre-ft'),('hectare-m','ltr','ha-m')):
            text=f'''  **********************           Volume        Volume
  Rainfall Dependent I/I        {u}      10^6 {v}
  **********************        ---------     ---------
  Sewershed Rainfall ......         0.000         0.000
  RDII Produced ...........         0.000         0.000
  RDII Ratio ..............         0.000
'''
            actual=table(text,'rdii',on_error='raise')
            self.assertEqual(actual.cell('swmm:sewershed_rainfall','swmm:volume').unit,expected)
            self.assertIsNone(actual.cell('swmm:rdii_ratio','swmm:volume').unit)
            self.assertEqual(actual.cell('swmm:rdii_ratio','swmm:volume').value,0)
            self.assertEqual(actual.cell('swmm:rdii_ratio','swmm:scaled_volume').missing_reason,'ratio_only_in_first_column')
            self.assertEqual(table(text.replace('10^6 '+v,'10^6 UNKNOWN'),'rdii').status,'unsupported_layout')

    def test_actions_keep_same_second_events_targets_and_precision(self):
        text=block('Control Actions Taken','  01/31/2020: 23:59:59 Link α setting changed to   0.50 by Control R\n'
            '  01/31/2020: 23:59:59 Link α setting changed to   0.75 by Control R\n'
            '  02/01/2020: 00:00:00 Link β setting changed to   1.00 by Control S')
        actual=table(text,'control_actions',on_error='raise')
        self.assertEqual(len(actual.rows),3)
        self.assertEqual(actual.rows[0].cells[0].value,actual.rows[1].cells[0].value)
        self.assertNotEqual(actual.rows[0].key,actual.rows[1].key)
        self.assertEqual(actual.rows[0].target,Ref(collection='swmm:links',key='α'))
        self.assertEqual(actual.rows[1].cells[1].value,.75)
        self.assertEqual(actual.rows[1].cells[1].precision,'decimal:2')
        self.assertEqual(table(text.replace('changed to','future action'),'control_actions').status,'unsupported_layout')

    def test_wall_time_remains_separate_from_model_calendar_and_less_than(self):
        text='  Analysis begun on: Mon Sep 21 01:02:03 2026\n  Analysis ended on: Tue Sep 22 03:04:06 2026\n  Total elapsed time: 1.02:02:03\n'
        actual=table(OPTIONS+text,'analysis_timing',on_error='raise')
        self.assertEqual(actual.cell('swmm:started','swmm:wall_clock').value,datetime(2026,9,21,1,2,3))
        self.assertEqual(actual.cell('swmm:elapsed','swmm:wall_seconds').value,93723)
        actual=table(text.replace('1.02:02:03','< 1 sec'),'analysis_timing',on_error='raise')
        self.assertEqual(actual.cell('swmm:elapsed','swmm:wall_seconds').qualifier,'less_than')
        self.assertEqual(table(text.replace('1.02:02:03','1 minute'),'analysis_timing').status,'unsupported_layout')

    def test_input_numbers_typed_missing_slots_copollutants_and_wide_fields(self):
        text=block('Pollutant Summary','  Count #/L12345678901.2323456789012.34      0.05 Co (0.25)',
            '  Ppt. GW Kdecay\n  Name Units Concen. Concen. 1/days CoPollutant')
        actual=table(text,'input_pollutants',on_error='raise')
        self.assertEqual(actual.cell('Count','swmm:rainfall_concentration').value,12345678901.23)
        self.assertEqual(actual.cell('Count','swmm:groundwater_concentration').value,23456789012.34)
        self.assertEqual(actual.cell('Count','swmm:co_fraction').value,.25)
        text=block('Node Summary','  A JUNCTION 100.00 5.00 0.0 Yes\n  B OUTFALL 90.00 0.00 0.0',NODE_HEADER)
        actual=table(text,'input_nodes',on_error='raise')
        self.assertEqual(actual.cell('A','swmm:external_inflow').value,'Yes')
        self.assertEqual(actual.cell('B','swmm:external_inflow').missing_reason,'not_flagged')
        self.assertEqual(table(text.replace('Yes','No'),'input_nodes').status,'unsupported_layout')

    def test_normalized_geometry_requires_complete_ordered_arrays(self):
        rows='  Shape Wide\n'+''.join('  '+metric+':\n'+('  0.0200 0.0400 0.0600 0.0800 0.1000\n'*10) for metric in ('Area','Hrad','Width'))
        text=block('Shape Summary',rows)
        actual=table(text,'input_shapes',on_error='raise')
        self.assertEqual(len(actual.rows),50)
        self.assertEqual(actual.rows[0].key,('Wide','1'))
        self.assertEqual(actual.rows[-1].key,('Wide','50'))
        self.assertEqual(actual.rows[-1].cells[0].value,.1)
        self.assertIsNone(actual.rows[-1].cells[0].unit)
        damaged=text.replace('0.1000','',1)
        self.assertEqual(table(damaged,'input_shapes').status,'unsupported_layout')
        self.assertEqual(table(text.replace('Width:','Area:'),'input_shapes').status,'unsupported_layout')


if __name__=='__main__':unittest.main()
