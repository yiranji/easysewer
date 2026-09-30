"""Archive actual standard/custom results, not simulated serializer fixtures."""

from dataclasses import fields, replace
import json
from pathlib import Path
import tempfile
import unittest

from easysewer import get_native_capabilities
from easysewer.io.inp import InpDocument
from easysewer.model import Model, Ref
from easysewer.model.report import ReportSelection
from easysewer.runtime import Runner, RunResult, ReportReadOptions
from test_options_v2 import network
from test_runner_v2 import config
from test_files_v2 import bind


@unittest.skipUnless(get_native_capabilities()['swmm_solver'] and get_native_capabilities()['flexible_ponding'], 'Both solvers required')
class NativeResultArchiveTests(unittest.TestCase):
    def solve(self,root,family,*,cache=False):
        model=network();model.update_options(allow_ponding=True)
        model.update_report(nodes=ReportSelection(mode='ALL'),links=ReportSelection(mode='ALL'))
        if cache:bind(model,'HOTSTART','SAVE',root/'state.hsf')
        result=Runner().run(model,config(root/'run',backend=family,
            report_read=ReportReadOptions(tables=('swmm:node_depth','swmm:node_flooding'))))
        self.assertTrue(result.succeeded,(result.failure,result.diagnostics))
        return result

    def assert_same(self,original,loaded):
        for field in fields(RunResult):
            if field.name not in ('artifacts','produced_caches'):
                self.assertEqual(getattr(original,field.name),getattr(loaded,field.name),field.name)
        self.assertEqual(len(original.artifacts),len(loaded.artifacts))
        for a,b in zip(original.artifacts,loaded.artifacts):
            self.assertEqual(a,replace(b,path=a.path))
            self.assertEqual(a.read_bytes(),b.read_bytes())
        for a,b in zip(original.produced_caches,loaded.produced_caches):
            self.assertEqual(a,replace(b,artifact=a.artifact))

    def test_real_outputs_tables_snapshots_and_custom_results_survive_move(self):
        for family in ('swmm:standard','easysewer:flexible-ponding'):
            with self.subTest(family=family),tempfile.TemporaryDirectory() as directory:
                root=Path(directory);original=self.solve(root,family,cache=True)
                original.save(root/'archive')
                self.assert_same(original,RunResult.load(root/'archive'))
                with original.open_output() as output:
                    before=output.series(Ref(collection='swmm:nodes',key='J'),'swmm:depth')
                (root/'run').rename(root/'unavailable-original')
                (root/'archive').rename(root/'移動 archive')
                loaded=RunResult.load(root/'移動 archive')
                with loaded.open_output() as output:
                    after=output.series(Ref(collection='swmm:nodes',key='J'),'swmm:depth')
                self.assertEqual(before.values,after.values)
                self.assertEqual(before.times,after.times)
                self.assertEqual(before.applicability,after.applicability)
                self.assertEqual(loaded.snapshot.model().to_json_document().to_bytes(),original.snapshot.model_json)
                self.assertEqual(loaded.report_table('swmm:node_depth'),original.report_table('swmm:node_depth'))
                reparsed=loaded.report_reader().table('swmm:node_depth')
                captured=original.report_table('swmm:node_depth')
                expected=tuple(replace(row,cells=tuple(replace(cell,
                    span=replace(cell.span,source=loaded.report.path) if cell.span else None)
                    for cell in row.cells)) for row in captured.rows)
                self.assertEqual(reparsed.rows,expected)
                self.assertEqual(loaded.produced_caches[0].manifest.to_bytes(),original.produced_caches[0].manifest.to_bytes())
                loaded.save(root/'resaved')
                self.assertEqual(RunResult.load(root/'resaved').snapshot,original.snapshot)

    def test_changed_source_or_forged_out_metadata_cannot_be_archived_or_loaded(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);original=self.solve(root,'swmm:standard')
            with self.assertRaises(TypeError):
                replace(original.snapshot,options=replace(original.snapshot.options,defaults_used={'mutable'}))
            with self.assertRaises(ValueError):
                replace(original,report_document=replace(original.report_document,text='invented report'))
            with self.assertRaises(ValueError):
                replace(original,mass_balance=replace(original.mass_balance,flow_percent=123))
            original.save(root/'archive')
            path=root/'archive'/'result.json';data=json.loads(path.read_bytes())
            data['result']['fields']['output_metadata']['fields']['output_offset']+=4
            path.write_text(json.dumps(data),encoding='utf-8')
            with self.assertRaisesRegex(ValueError,'OUT metadata'):RunResult.load(root/'archive')
            Path(original.output.path).write_bytes(b'changed')
            with self.assertRaises(ValueError):original.save(root/'bad')
            self.assertFalse((root/'bad').exists())

    def test_all_cache_kinds_restored_evidence_can_be_consumed_and_resaved(self):
        from test_quality_v2 import quality_model
        from test_rdii_v2 import rdii_model
        for family in ('swmm:standard','easysewer:flexible-ponding'):
            for kind in ('HOTSTART','RUNOFF','RDII'):
                with self.subTest(family=family,kind=kind),tempfile.TemporaryDirectory() as directory:
                    root=Path(directory);factory=rdii_model if kind=='RDII' else quality_model
                    base=factory();base.update_options(allow_ponding=True)
                    producer=base.copy();bind(producer,kind,'SAVE',root/'state.cache')
                    result=Runner().run(producer,config(root/'producer',backend=family))
                    self.assertTrue(result.succeeded,result.failure)
                    result.save(root/'archive')
                    restored=RunResult.load(root/'archive');self.assert_same(result,restored)
                    evidence,=restored.produced_caches
                    bind(base,kind,'USE',evidence.artifact.path)
                    consumed=Runner().run(base,config(root/'consumer',backend=family),producers={kind:evidence})
                    self.assertTrue(consumed.succeeded,consumed.failure)
                    self.assertEqual(consumed.consumed_caches[0].producer_run_id,result.run_id)
                    consumed.save(root/'consumed')
                    self.assert_same(consumed,RunResult.load(root/'consumed'))

    def test_lid_detail_and_multi_pollutant_outputs_remain_queryable(self):
        from test_native_v2_lid import literal_source
        for family in ('swmm:standard','easysewer:flexible-ponding'):
            with self.subTest(family=family),tempfile.TemporaryDirectory() as directory:
                root=Path(directory);source=literal_source('BC',detail=str(root/'detail.txt'))
                model=Model.from_document(InpDocument.from_text(source),strict=True)
                model.update_options(allow_ponding=True)
                result=Runner().run(model,config(root/'run',backend=family))
                self.assertTrue(result.succeeded,result.failure)
                owner=next(a.owner for a in result.artifacts if a.role=='swmm:lid-detail')
                before=result.read_lid_report(owner)
                result.save(root/'archive');self.assert_same(result,RunResult.load(root/'archive'))
                (root/'run').rename(root/'old-run')
                loaded=RunResult.load(root/'archive');after=loaded.read_lid_report(owner)
                self.assertEqual(before.rows,tuple(replace(row,cells=tuple(replace(cell,
                    span=replace(cell.span,source=before.source.path) if cell.span else None)
                    for cell in row.cells)) for row in after.rows))

    def test_post_native_failure_with_and_without_retained_files_roundtrips(self):
        for keep in (False,True):
            with self.subTest(keep=keep),tempfile.TemporaryDirectory() as directory:
                root=Path(directory)
                def fail(progress):
                    if progress.phase=='finalizing':raise RuntimeError('failed final callback')
                result=Runner().run(network(),config(root/'run',keep_failed_artifacts=keep),progress=fail)
                self.assertEqual(result.status,'failed');self.assertTrue(result.native_completed)
                self.assertIsNotNone(result.failure_report)
                result.save(root/'archive')
                loaded=RunResult.load(root/'archive');self.assert_same(result,loaded)
                self.assertEqual(loaded.failure_report.document.raw,result.failure_report.document.raw)
