"""Native status/input tables checked against executed inputs and raw RPT cells."""

from datetime import date, datetime, time, timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from easysewer import get_native_capabilities
from easysewer.io.inp import InpDocument
from easysewer.model import Model, FileReference, Ref
from easysewer.model.hydrology import FileRainfall
from easysewer.runtime import ReportReadOptions, Runner, StandardBackend
from test_runner_v2 import config
from test_native_v2_report import OPTIONS, check_tables
from test_native_v2_lid import literal_source
from test_native_v2_network import NETWORK, SETTINGS, RESOURCES, CASES
from test_rdii_v2 import rdii_model
from test_regulators_v2 import regulator_model
from test_controls_v2 import controlled


def input_model(source):
    model=Model.from_document(InpDocument.from_text(source),strict=True)
    model.update_report(input=True,controls=True)
    return model


@unittest.skipUnless(get_native_capabilities()['swmm_solver'],'Native solver unavailable')
class NativeReportStatusTests(unittest.TestCase):
    def run_model(self,root,model):
        result=Runner().run(model,config(root,report_read=OPTIONS))
        check_tables(self,result)
        return result

    def test_actual_native_error_survives_cleanup_and_keeps_input_context(self):
        class FaultBackend(StandardBackend):
            # Fault injection after Runner's checks exercises a real native
            # syntax error. The echo is not claimed to match the original Model.
            def session(self,**options):
                session=super().session(**options);open_session=session.open
                def broken_open(inp,*args,**kwargs):
                    with Path(inp).open('ab') as stream:
                        stream.write(b'\n[CONDUITS]\nBroken J O BAD .01 0 0\n[XSECTIONS]\nBroken CIRCULAR 1 0 0 0\n')
                    return open_session(inp,*args,**kwargs)
                session.open=broken_open
                return session
        with tempfile.TemporaryDirectory() as directory:
            for retained,limit in ((False,65536),(True,65536),(False,23)):
                root=Path(directory)/(str(retained)+'-'+str(limit));root.mkdir()
                (root/'model.rpt').write_bytes(b'previous-success')
                result=Runner(backends={'swmm:standard':FaultBackend()}).run(
                    input_model(SETTINGS+NETWORK.format(shape='CIRCULAR 1 0 0 0')),
                    config(root,keep_failed_artifacts=retained,overwrite=True,report_read=ReportReadOptions(max_bytes=limit)))
                self.assertEqual(result.status,'failed');self.assertEqual(result.failure.stage,'open')
                self.assertIsNotNone(result.failure.native);self.assertFalse(result.native_completed)
                self.assertIsNone(result.report_document);self.assertEqual(result.report_tables,())
                self.assertEqual((root/'model.rpt').read_bytes(),b'previous-success')
                self.assertIsNotNone(result.failure_report)
                self.assertEqual(result.failure_report.truncated,limit==23)
                if limit>23:
                    context=next(m for m in result.failure_report.document.message_contexts if m.diagnostic.code=='swmm.error.211')
                    # Native SectWords uses the singular prefix; keep its claim verbatim.
                    self.assertEqual(context.input_section,'CONDUIT');self.assertIn('Broken J O BAD',context.input_text)
                    self.assertGreater(context.input_line,0)
                if not retained:
                    self.assertIsNone(result.retained_directory)
                    self.assertFalse(list(root.glob('.easysewer-*')))
                    self.assertFalse(Path(result.failure_report.document.source).exists())

    def test_capture_read_failure_and_strict_decode_never_replace_primary(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            error=RuntimeError('primary callback error')
            def fail(progress):
                if progress.phase=='finalizing':raise error
            model=input_model(SETTINGS+NETWORK.format(shape='CIRCULAR 1 0 0 0'))
            with patch('easysewer.io.report_document.ReportCapture.read',side_effect=OSError('capture unavailable')):
                result=Runner().run(model,config(root/'read',keep_failed_artifacts=False),progress=fail)
            self.assertEqual(result.failure.message,str(error))
            self.assertEqual(result.failure.stage,'callback');self.assertIsNone(result.failure_report)
            self.assertIn('run.failure_report_capture',{d.code for d in result.diagnostics.diagnostics})
            model=input_model('[TITLE]\nCafé\n'+SETTINGS+NETWORK.format(shape='CIRCULAR 1 0 0 0'))
            result=Runner().run(model,config(root/'decode',keep_failed_artifacts=False,
                report_read=ReportReadOptions(encoding='ascii',on_decode_error='raise')))
            self.assertEqual(result.failure.exception_type,'UnicodeDecodeError')
            self.assertEqual(result.failure.stage,'report_read')
            self.assertIsNone(result.failure_report.document.text)
            self.assertIn('Café'.encode(),result.failure_report.document.raw)
            self.assertFalse(result.failure_report.truncated)

    def test_input_summaries_six_units_repeated_lid_and_producer_context(self):
        with tempfile.TemporaryDirectory() as directory:
            for units in ('CFS','GPM','MGD','CMS','LPS','MLD'):
                with self.subTest(units=units):
                    model=input_model(literal_source('BC',units=units)+'[LID_USAGE]\nS L 1 200 20 20 10 0 * * 10\n')
                    result=self.run_model(Path(directory)/units,model)
                    get=lambda name:result.report_table('swmm:'+name)
                    self.assertEqual(get('analysis_options').cell('swmm:flow_units','swmm:text').value,units)
                    self.assertEqual(get('element_count').cell('swmm:nodes','swmm:count').value,2)
                    self.assertEqual(get('input_landuses').cell('Land','swmm:sweeping_removal').value,.5)
                    self.assertEqual(get('input_pollutants').cell('Q1','swmm:rainfall_concentration').unit,'UG/L')
                    self.assertEqual(get('input_subcatchments').cell('S','swmm:area').unit,'acre' if units in ('CFS','GPM','MGD') else 'ha')
                    self.assertEqual(get('input_raingages').cell('R','swmm:rain_type').value,'INTENSITY')
                    self.assertEqual(get('input_nodes').cell('J','swmm:type').value,'STORAGE')
                    self.assertEqual(get('input_links').cell('P','swmm:inlet').value,'J')
                    self.assertEqual(get('input_cross_sections').status,'empty')  # P is an outlet.
                    lids=get('input_lid_controls')
                    self.assertEqual(len(lids.rows),2);self.assertTrue(all(r.target is None for r in lids.rows))
                    self.assertEqual(sorted(r.cells[2].value for r in lids.rows),[1,2])
                    self.assertEqual(sorted(r.cells[3].value for r in lids.rows),[100,200])
                    self.assertEqual(get('analysis_timing').status,'present')
                    # Later edits do not change interpretation of captured gage data.
                    model.raingages.rename('R','Later')
                    self.assertEqual(result.report_reader().table('swmm:input_raingages'),get('input_raingages'))

    def test_normalized_geometry_arrays_three_kinds_both_unit_systems(self):
        with tempfile.TemporaryDirectory() as directory:
            for kind,key,name,collection in (('CUSTOM','input_shapes','Shape1','curves'),
                    ('IRREGULAR','input_transects','Transect1','transects'),('STREET','input_streets','Street1','streets')):
                for units in ('CMS','CFS'):
                    with self.subTest(kind=kind,units=units):
                        model=input_model(SETTINGS+NETWORK.format(shape=kind+' '+CASES[kind])+RESOURCES[kind])
                        model.convert_units(units)
                        result=self.run_model(Path(directory)/(kind+units),model)
                        table=result.report_table('swmm:'+key)
                        self.assertEqual(len(table.rows),50)
                        self.assertEqual(table.rows[0].key,(name,'1'))
                        self.assertEqual(table.rows[-1].key,(name,'50'))
                        self.assertTrue(all(r.target==Ref(collection='swmm:'+collection,key=name) for r in table.rows))
                        self.assertTrue(all(c.unit is None and c.precision=='decimal:4' for r in table.rows for c in r.cells))
                        self.assertAlmostEqual(table.rows[-1].cells[0].value,1,delta=.0001)
                        self.assertEqual(result.report_table('swmm:input_cross_sections').cell('P','swmm:shape').value,name)

    def test_rdii_file_station_dates_counts_and_volume_ratio(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);rain=root/'rain source.dat'
            rain.write_text('Station 2020 1 30 0 0 0\nStation 2020 1 30 1 0 1\nStation 2020 1 30 2 0 0\n',encoding='ascii')
            for units in ('CFS','CMS'):
                with self.subTest(units=units):
                    model=rdii_model();model.update_report(input=True)
                    model.update_options(end_date=date(2020,1,30),end_time=time(8),routing_step=timedelta(seconds=60))
                    model.convert_units(units)
                    model.raingages.update('R',form='VOLUME',interval=timedelta(hours=1),
                        source=FileRainfall(file=FileReference(path=str(rain)),station='Station',units='IN'))
                    result=self.run_model(root/units,model)
                    table=result.report_table('swmm:rainfall_file');self.assertEqual(len(table.rows),1)
                    row=table.rows[0];self.assertIsNone(row.target)
                    self.assertEqual(row.cells[0].value,'Station')
                    self.assertEqual(row.cells[1].value,datetime(2020,1,30))
                    self.assertEqual([c.value for c in row.cells[3:]],[60,3,0,0])
                    gage=result.report_table('swmm:input_raingages')
                    self.assertTrue(gage.cell('R','swmm:data_source').value.endswith('.dat'))
                    self.assertEqual(gage.cell('R','swmm:rain_type').missing_reason,'not_printed_for_file')
                    table=result.report_table('swmm:rdii')
                    rainfall=table.cell('swmm:sewershed_rainfall','swmm:volume')
                    produced=table.cell('swmm:rdii_produced','swmm:volume')
                    ratio=table.cell('swmm:rdii_ratio','swmm:volume')
                    self.assertGreater(produced.value,0);self.assertIsNone(ratio.unit)
                    self.assertEqual(rainfall.unit,'acre-ft' if units=='CFS' else 'ha-m')
                    # Two rounded volume columns constrain the independently printed ratio.
                    self.assertLess(abs(produced.value-rainfall.value*ratio.value),.0015)
                    self.assertIsNotNone(table.cell('swmm:rdii_ratio','swmm:scaled_volume').missing_reason)

    def test_control_actions_ordered_calendar_and_nonconduit_input_slots(self):
        with tempfile.TemporaryDirectory() as directory:
            for kind in ('PUMP2','SIDE','TRANSVERSE','FUNCTIONAL/HEAD'):
                with self.subTest(kind=kind):
                    model=regulator_model(kind);model.update_report(input=True)
                    result=self.run_model(Path(directory)/kind.replace('/','-'),model)
                    row=result.report_table('swmm:input_links').row('P')
                    self.assertEqual(row.cells[0].value,'J')
                    self.assertTrue(all(c.missing_reason=='not_applicable_to_link_type' for c in row.cells[3:]))
            model=controlled('RULE close\nIF SIMULATION TIME < 00:00:20\nTHEN CONDUIT P STATUS = CLOSED\nELSE CONDUIT P STATUS = OPEN\n')
            model.update_report(input=True,controls=True)
            result=self.run_model(Path(directory)/'actions',model)
            table=result.report_table('swmm:control_actions');self.assertEqual(len(table.rows),2)
            self.assertEqual([r.cells[1].value for r in table.rows],[0,1])
            self.assertTrue(all(r.target==Ref(collection='swmm:links',key='P') for r in table.rows))
            self.assertEqual([r.cells[2].value for r in table.rows],['close','close'])
            self.assertLess(table.rows[0].cells[0].value,table.rows[1].cells[0].value)


if __name__=='__main__':unittest.main()
