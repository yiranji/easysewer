"""Missing physical results, raw evidence, future scopes and portable contracts."""

from dataclasses import replace
from pathlib import Path
import struct
from types import SimpleNamespace
import tempfile
import unittest

from easysewer.io.json import JsonDocument
from easysewer.io.output import OutputReader
from easysewer.io.output_metadata import OutputMetadata
from easysewer.io.report import ReportContext, ReportReader
from easysewer.io.report_document import ReportDocument
from easysewer.io.report_details import read_lid_report, table_series
from easysewer.model import Ref
from easysewer.results import ResultApplicability, ResultContext, ResultSeries, ResultTable
from easysewer.results.applicability import context_from_model
from easysewer.runtime.backend import MassBalance
from test_output_v2 import binary_output
from test_report_details_v2 import LID, NODE
from test_options_v2 import network


def replay_context():
    return ResultContext(policy='swmm:result-context:1', facts=(('swmm:runoff','replayed'),
        ('swmm:routing','computed'), ('swmm:routing-quality','computed'), ('swmm:runoff-physics','1'),
        ('swmm:groundwater-targets','["S"]'), ('swmm:rainfall','computed'), ('swmm:report-disabled','no'),
        ('swmm:balance-without-report','no')),
        evidence=('input-sha256:'+'a'*64, 'backend-sha256:'+'b'*64))


class ResultApplicabilityTests(unittest.TestCase):
    def test_scope_preserves_future_facts_and_never_claims_unknown_variables(self):
        context=replay_context().with_fact('plugin:future', 'opaque-value')
        self.assertEqual(ResultContext.from_data(context.to_data()), context)
        self.assertEqual(context.output('swmm:links','future:pressure').status,'unknown')
        self.assertEqual(replace(context,policy='plugin:future:7').output('swmm:nodes','swmm:depth').status,'unknown')
        self.assertEqual(context.artifact('HOTSTART').status,'partial')
        self.assertEqual(context.output('swmm:system','swmm:evaporation').status,'partial')
        self.assertEqual(context.with_fact('swmm:groundwater-targets','[]').output('swmm:system','swmm:evaporation').status,'replayed')
        with self.assertRaises(ValueError):ResultContext(facts=(('swmm:runoff','computed'),('swmm:runoff','replayed')))
        with self.assertRaises(TypeError):ResultApplicability(reasons=['swmm:unknown'])
        with self.assertRaises(ValueError):context.with_fact('swmm:groundwater-targets','["S","S"]')

    def test_new_model_context_and_continuity_data_roundtrip(self):
        model=network();self.assertIsNone(model.support)
        backend=SimpleNamespace(capabilities=('easysewer:runoff-physics:1',),sha256='b'*64,abi='swmm:5.2')
        context=context_from_model(model,backend,input_sha256='a'*64)
        self.assertEqual(context.fact('swmm:runoff'),'inactive')
        self.assertEqual(context.fact('swmm:routing'),'computed')
        balance=MassBalance(runoff_percent=0.,flow_percent=.1,quality_percent=0.).with_context(context)
        self.assertEqual(MassBalance.from_data(JsonDocument.from_data(balance.to_data()).data),balance)
        self.assertEqual(MassBalance.from_data(dict(runoff_percent=0.,flow_percent=.1,quality_percent=0.)).applicability('flow').status,'unknown')

    def test_groundwater_scope_is_independent_of_relation_order_and_reads_legacy_order(self):
        from test_groundwater_v2 import groundwater_model
        model=groundwater_model()
        model.subcatchments.add(replace(model.subcatchments['S'],id='A'))
        model.groundwater.add(replace(model.groundwater['S'],subcatchment=Ref(collection='swmm:subcatchments',key='A')))
        backend=SimpleNamespace(capabilities=('easysewer:runoff-physics:1',),sha256='b'*64,abi='swmm:5.2')
        before=context_from_model(model,backend,input_sha256='a'*64)
        model.groundwater.move('A',before='S')
        after=context_from_model(model,backend,input_sha256='a'*64)
        self.assertEqual(before,after)
        for key in ('A','S'):
            target=Ref(collection='swmm:subcatchments',key=key)
            self.assertEqual(after.output(target.collection,'swmm:groundwater_flow',target=target).status,'computed')
        missing=Ref(collection='swmm:subcatchments',key='Unbound')
        self.assertEqual(after.output(missing.collection,'swmm:groundwater_flow',target=missing).status,'not_applicable')
        legacy=after.with_fact('swmm:groundwater-targets','["S", "A"]')
        self.assertEqual(ResultContext.from_data(legacy.to_data()),legacy)
        self.assertEqual(ResultContext.from_data(legacy.to_data()).fact('swmm:groundwater-targets'),'["S", "A"]')

    def test_inapplicability_never_hides_corrupt_binary_values(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'model.out';path.write_bytes(binary_output())
            metadata=replace(OutputMetadata.read(path),result_context=replay_context().with_fact('swmm:routing','inactive'))
            # First saved subcatchment field is nonfinite. Turn hydrology off
            # as well: applicability must not bypass the raw numeric check.
            metadata=replace(metadata,result_context=metadata.result_context.with_fact('swmm:rainfall','inactive'))
            raw=bytearray(path.read_bytes());struct.pack_into('<f',raw,metadata.output_offset+8,float('nan'));path.write_bytes(raw)
            target=Ref(collection='swmm:subcatchments',key=metadata.names('swmm:subcatchments')[0])
            with OutputReader(path,metadata=metadata) as reader:
                with self.assertRaises(ValueError):reader.series(target,'swmm:rainfall')
            with OutputReader(path,metadata=metadata,invalid_values='preserve') as reader:
                self.assertEqual(reader.series(target,'swmm:rainfall').missing[0],'nonfinite_source_value')

    def test_raw_out_is_unknown_captured_scope_and_maximum_retain_assessments(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'model.out';path.write_bytes(binary_output())
            context=replay_context()
            with OutputReader(path) as reader:
                self.assertEqual(reader.series(None,'swmm:evaporation').applicability.status,'unknown')
            metadata=replace(OutputMetadata.read(path),result_context=context)
            with OutputReader(path,metadata=metadata) as reader:
                series=reader.series(None,'swmm:evaporation')
                self.assertEqual(series.applicability.status,'partial')
                self.assertEqual(series.maximum().applicability,series.applicability)
                self.assertEqual(ResultSeries.from_json_document(series.to_json_document()),series)
                disabled=replace(metadata,result_context=context.with_fact('swmm:routing','inactive'))
            with OutputReader(path,metadata=disabled) as reader:
                series=reader.series(Ref(collection='swmm:nodes',key='J'),'swmm:depth')
                self.assertTrue(all(v is None for v in series.values))
                self.assertEqual(set(series.missing),{'swmm:process-inactive'})
                self.assertIsNone(series.maximum().value)
                with self.assertRaises(ValueError):replace(series,values=(0.,)*len(series.values),missing=(None,)*len(series.values))

    def test_uncomputed_lid_numbers_keep_tokens_and_json_not_fake_zeros(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'lid.txt';path.write_text(LID)
            table=read_lid_report(path,result_context=replay_context(),on_error='raise')
            self.assertEqual(table.status,'present')
            self.assertEqual(table.applicability.status,'not_computed')
            self.assertEqual(table.raw_text,path.read_bytes().decode())
            cell=table.rows[0].cells[1]
            self.assertIsNone(cell.value);self.assertTrue(cell.raw)
            self.assertEqual(ResultTable.from_json_document(table.to_json_document()),table)
            series=table_series(table,'swmm:inflow')
            self.assertEqual(series.applicability,table.applicability)
            self.assertTrue(all(v is None for v in series.values))

    def test_legacy_json_defaults_to_unknown_and_new_contract_requires_evidence(self):
        table=ReportReader(ReportDocument.from_bytes(NODE.encode())).detail(Ref(collection='swmm:nodes',key='J'))
        data=table.to_json_document().data;data['schema_version']='1.0';data.pop('applicability')
        for column in data['columns']:column.pop('applicability')
        legacy=ResultTable.from_json_document(JsonDocument.from_data(data))
        self.assertEqual(legacy.applicability.status,'unknown')
        series=table_series(legacy,'swmm:depth');data=series.to_json_document().data
        data['schema_version']='1.0';data.pop('applicability')
        self.assertEqual(ResultSeries.from_json_document(JsonDocument.from_data(data)).applicability.status,'unknown')
        data['schema_version']='1.1'
        with self.assertRaises(ValueError):ResultSeries.from_json_document(JsonDocument.from_data(data))

    def test_report_details_and_mass_balance_have_process_specific_scopes(self):
        context=replay_context()
        reader=ReportReader(ReportDocument.from_bytes(NODE.encode()),context=ReportContext(result_context=context))
        self.assertEqual(reader.series(Ref(collection='swmm:nodes',key='J'),'swmm:depth').applicability.status,'computed')
        raw=MassBalance(runoff_percent=0.,flow_percent=1.2,quality_percent=.3)
        balance=raw.with_context(context)
        self.assertIsNone(balance.runoff_percent);self.assertEqual(balance.flow_percent,1.2)
        self.assertEqual(balance.raw_percentages,(0.,1.2,.3))
        disabled=raw.with_context(context.with_fact('swmm:report-disabled','yes'))
        self.assertEqual((disabled.runoff_percent,disabled.flow_percent,disabled.quality_percent),(None,)*3)
        custom=raw.with_context(disabled.result_context.with_fact('swmm:balance-without-report','yes'))
        self.assertEqual((custom.runoff_percent,custom.flow_percent,custom.quality_percent),(None,1.2,.3))
        unverified=raw.with_context(disabled.result_context.with_fact('swmm:balance-without-report','unknown'))
        self.assertEqual(unverified.applicability('flow').status,'unknown')
        early=raw.with_context(context.with_fact('swmm:completed','no'))
        self.assertEqual(early.applicability('flow').status,'partial')
