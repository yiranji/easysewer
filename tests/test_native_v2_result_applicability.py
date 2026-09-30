"""Actual SAVE/USE results, disabled reports and public C continuity evidence."""

import ctypes
from datetime import time
import json
import os
from pathlib import Path
import tempfile
import unittest

from easysewer import get_native_capabilities
from easysewer.io.inp import InpDocument
from easysewer.io.report import ReportReader
from easysewer.io.report_document import ReportDocument, SWMM_UTF8_REPORT
from easysewer.model import Model, Ref
from easysewer.results import ResultSeries, ResultTable, ResultContext
from easysewer.runtime import Runner, StandardBackend, ReportReadOptions
from test_files_v2 import bind
from test_native_v2_groundwater import literal_groundwater
from test_native_v2_standard_io import direct_library
from test_options_v2 import network
from test_runner_v2 import config


TABLES=('swmm:runoff_quantity_continuity','swmm:runoff_quality_continuity','swmm:groundwater_continuity',
        'swmm:subcatchment_runoff','swmm:groundwater','swmm:subcatchment_washoff','swmm:flow_routing_continuity')


@unittest.skipUnless(get_native_capabilities()['swmm_solver'] and get_native_capabilities()['flexible_ponding'], 'Both native solvers required')
class NativeApplicabilityTests(unittest.TestCase):
    def success(self, result):
        self.assertTrue(result.succeeded,repr(result.failure)+' '+repr(result.diagnostics.errors))
        return result

    def test_replay_runner_both_backends_report_modes_out_json_and_partial_hotstart(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for family in ('swmm:standard','easysewer:flexible-ponding'):
                base=Model.from_document(InpDocument.from_text(literal_groundwater()),strict=True)
                base.update_options(allow_ponding=True)
                folder=root/family.split(':')[-1];folder.mkdir()
                producer=base.copy();bind(producer,'RUNOFF','SAVE',folder/'runoff.bin')
                saved=self.success(Runner().run(producer,config(folder/'producer',backend=family)))
                evidence,=saved.produced_caches
                self.assertEqual(saved.mass_balance.applicability('runoff').status,'computed')
                for averages,disabled,late in ((False,False,False),(True,False,True),(False,True,True),(True,True,False)):
                    with self.subTest(family=family,averages=averages,disabled=disabled,late=late):
                        model=base.copy();model.update_report(averages=averages,disabled=disabled)
                        if late:model.update_options(report_start_time=time(1))
                        bind(model,'RUNOFF','USE',evidence.artifact.path)
                        dest=folder/f'consumer-{averages}-{disabled}-{late}'
                        bind(model,'HOTSTART','SAVE',folder/f'state-{averages}-{disabled}-{late}.hsf')
                        run=self.success(Runner().run(model,config(dest,backend=family,
                            report_read=ReportReadOptions(tables=TABLES)),producers={'RUNOFF':evidence}))
                        self.assertIsNone(run.mass_balance.runoff_percent)
                        skipped=disabled and family=='swmm:standard'
                        self.assertEqual(run.mass_balance.applicability('flow').status,'not_computed' if skipped else 'computed')
                        if skipped:self.assertIsNone(run.mass_balance.flow_percent)
                        else:self.assertEqual(run.mass_balance.flow_percent,run.mass_balance.raw_percentages[1])
                        self.assertEqual(run.produced_caches[0].applicability.status,'partial')
                        context=run.result_context
                        self.assertEqual(context.fact('swmm:runoff'),'replayed')
                        record=json.loads(run.artifact('run:execution-record').read_bytes())
                        self.assertEqual(ResultContext.from_data(record['result_context']),context)
                        self.assertEqual(record['cache_applicability'][0]['applicability']['status'],'partial')
                        for key in TABLES[:-1]:
                            table=run.report_table(key)
                            self.assertEqual(table.applicability.status,'not_computed')
                            self.assertEqual(run.report_reader().table(key),table)
                            for row in table.rows:
                                for col,cell in zip(table.columns,row.cells):
                                    if col.kind=='number':
                                        self.assertIsNone(cell.value)
                                        self.assertEqual(cell.missing_reason,'swmm:runoff-hydrology-not-recomputed')
                            self.assertEqual(ResultTable.from_json_document(table.to_json_document()),table)
                        if not disabled:
                            raw=run.report_document.raw
                            bare=ReportReader(ReportDocument.from_bytes(raw,profile=SWMM_UTF8_REPORT))
                            self.assertEqual(bare.table('swmm:groundwater').applicability.status,'not_computed')
                            table=run.report_table('swmm:subcatchment_runoff')
                            self.assertTrue(any('0.00' in c.raw for row in table.rows for c in row.cells))
                            flow=run.report_table('swmm:flow_routing_continuity')
                            self.assertGreater(flow.row('swmm:wet_weather_inflow').cells[0].value,0)
                        with run.open_output() as reader:
                            runoff=reader.series(Ref(collection='swmm:subcatchments',key='S'),'swmm:runoff')
                            evap=reader.series(None,'swmm:evaporation')
                            depth=reader.series(Ref(collection='swmm:nodes',key='J'),'swmm:depth')
                            self.assertEqual(runoff.applicability.status,'replayed')
                            self.assertGreater(max(runoff.values),0)
                            self.assertEqual(evap.applicability.status,'partial')
                            self.assertEqual(depth.applicability.status,'computed')
                            self.assertEqual(ResultSeries.from_json_document(evap.to_json_document()),evap)

    def test_session_retains_raw_c_values_but_does_not_call_disabled_balance_computed(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for disabled in (False,True):
                model=network();model.update_report(disabled=disabled)
                inp=root/'model.inp';inp.write_text(model.to_document().text)
                lib,_=direct_library()
                lib.swmm_getMassBalErr.argtypes=[ctypes.POINTER(ctypes.c_float)]*3
                values=[ctypes.c_float() for _ in range(3)]
                try:
                    self.assertEqual(lib.swmm_open(os.fsencode(inp),os.fsencode(root/'raw.rpt'),os.fsencode(root/'raw.out')),0)
                    self.assertEqual(lib.swmm_start(1),0)
                    elapsed=ctypes.c_double()
                    for _ in range(20000):
                        self.assertEqual(lib.swmm_step(ctypes.byref(elapsed)),0)
                        if not elapsed.value:break
                    else:self.fail('Direct native step budget exceeded')
                    self.assertEqual(lib.swmm_end(),0)
                    self.assertEqual(lib.swmm_getMassBalErr(*(ctypes.byref(v) for v in values)),0)
                finally:lib.swmm_close()
                with StandardBackend().session(working_directory=root) as session:
                    session.open('model.inp','wrapped.rpt','wrapped.out',overwrite=True);session.start()
                    for _ in range(20000):
                        if session.step(max_steps=100).finished:break
                    else:self.fail('Worker step budget exceeded')
                    balance=session.end()
                self.assertEqual(balance.raw_percentages,tuple(v.value for v in values))
                self.assertIsNone(balance.runoff_percent)
                self.assertIsNone(balance.quality_percent)
                if disabled:self.assertIsNone(balance.flow_percent)
                else:self.assertAlmostEqual(balance.flow_percent,values[1].value)

    def test_early_stop_and_opaque_input_are_explicitly_limited(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);model=network()
            (root/'model.inp').write_text(model.to_document().text)
            with StandardBackend().session(working_directory=root) as session:
                session.open('model.inp','model.rpt','model.out');session.start();session.step()
                balance=session.end()
            self.assertEqual(balance.applicability('flow').status,'partial')
            from easysewer.results.applicability import context_from_input
            info=StandardBackend().probe()
            context=context_from_input((model.to_document().text+'[FUTURE]\nextra 42\n').encode(),info)
            self.assertEqual(context.output('swmm:nodes','swmm:depth').status,'unknown')
