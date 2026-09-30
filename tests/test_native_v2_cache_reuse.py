"""Native producer/consumer scope checks before open, persistence and rollback."""

from dataclasses import replace
from datetime import time
import json
from pathlib import Path
import tempfile
import unittest

from easysewer import get_native_capabilities
from easysewer.model import Ref,FileReference
from easysewer.model.resources import FileTimeSeries
from easysewer.runtime import Runner,CacheEvidence,CacheReuse
from test_cache_reuse_v2 import model
from test_files_v2 import bind
from test_runner_v2 import config


@unittest.skipUnless(get_native_capabilities()['swmm_solver'] and get_native_capabilities()['flexible_ponding'],'Both solvers required')
class NativeCacheReuseTests(unittest.TestCase):
    def run_success(self,value,path,family,**kwargs):
        settings=kwargs.pop('settings',{})
        result=Runner().run(value,config(path,backend=family,**settings),**kwargs)
        self.assertTrue(result.succeeded,repr(result.failure)+' '+repr(result.diagnostics.errors))
        return result

    def produce(self,root,family,base=None):
        base=base or model();base.update_options(allow_ponding=True)
        producer=base.copy();bind(producer,'RUNOFF','SAVE',root/'history.bin')
        result=self.run_success(producer,root/'producer',family)
        evidence,=result.produced_caches
        self.assertEqual(CacheEvidence.from_bytes(result.artifact('run:cache-evidence').read_bytes()),evidence.reuse_evidence)
        return base,evidence,result

    def test_both_families_match_metadata_changes_and_reject_physical_edits_before_open(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for family in ('swmm:standard','easysewer:flexible-ponding'):
                folder=root/family.split(':')[-1];folder.mkdir()
                base,evidence,producer=self.produce(folder,family)
                consumer=base.copy();consumer.update_report(disabled=True,averages=True)
                consumer.update_options(report_start_time=time(1))
                bind(consumer,'RUNOFF','USE',evidence.artifact.path)
                good=self.run_success(consumer,folder/'matched',family,producers={'RUNOFF':evidence},
                    settings=dict(cache_reuse=(('RUNOFF','require_match'),)))
                reuse=good.consumed_caches[0].reuse
                self.assertEqual((reuse.status,reuse.origin),('matched','runner-observed'))
                record=json.loads(good.artifact('run:execution-record').read_bytes())
                self.assertEqual(CacheReuse.from_data(record['consumed_caches'][0]['reuse']),reuse)
                edits=(('area','subcatchments','S',dict(area=base.subcatchments['S'].area*2),'swmm:subcatchments'),
                    ('bottom','groundwater','S',dict(bottom_elevation=-12.),'swmm:groundwater-bindings'),
                    ('receiver','groundwater','S',dict(node=Ref(collection='swmm:nodes',key='O')),'swmm:groundwater-bindings'),
                    ('hydraulics','nodes','J',dict(elevation=1.),'swmm:nodes'))
                for name,collection,key,fields,expected in edits:
                    with self.subTest(family=family,edit=name):
                        changed=base.copy();changed.collection('swmm:'+collection).update(key,**fields)
                        bind(changed,'RUNOFF','USE',evidence.artifact.path)
                        destination=folder/name;destination.mkdir()
                        for file in ('model.inp','model.rpt','model.out'):(destination/file).write_bytes(b'previous-success')
                        phases=[]
                        result=Runner().run(changed,config(destination,backend=family,overwrite=True,
                            keep_failed_artifacts=False,cache_reuse=(('RUNOFF','require_match'),)),
                            producers={'RUNOFF':evidence},progress=lambda p:phases.append(p.phase))
                        self.assertEqual(result.status,'rejected',result.failure)
                        self.assertIn('run.cache_conditions',{d.code for d in result.diagnostics.errors})
                        self.assertNotIn('opening',phases);self.assertIsNone(result.engine_objects)
                        self.assertIn(expected,result.consumed_caches[0].reuse.differences)
                        for file in ('model.inp','model.rpt','model.out'):self.assertEqual((destination/file).read_bytes(),b'previous-success')
                        self.assertFalse(list(destination.glob('.easysewer-*')))

    def test_frozen_history_keeps_explicit_difference_and_actual_cached_flows(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for family in ('swmm:standard','easysewer:flexible-ponding'):
                folder=root/family.split(':')[-1];folder.mkdir()
                base,evidence,producer=self.produce(folder,family)
                changed=base.copy();changed.subcatchments.update('S',area=base.subcatchments['S'].area*2)
                fresh=self.run_success(changed,folder/'fresh',family)
                bind(changed,'RUNOFF','USE',evidence.artifact.path)
                replay=self.run_success(changed,folder/'frozen',family,producers={'RUNOFF':evidence},
                    settings=dict(cache_reuse=(('RUNOFF','frozen'),)))
                self.assertEqual(replay.consumed_caches[0].reuse.status,'changed')
                self.assertIn('run.cache_frozen',{d.code for d in replay.diagnostics.diagnostics})
                with fresh.open_output() as a,replay.open_output() as b,producer.open_output() as c:
                    target=Ref(collection='swmm:subcatchments',key='S')
                    original=c.series(target,'swmm:runoff').values
                    actual=b.series(target,'swmm:runoff').values
                    computed=a.series(target,'swmm:runoff').values
                    self.assertGreater(max(computed)-max(actual),.7)
                    self.assertAlmostEqual(max(actual),max(original),places=6)
                self.assertEqual(evidence.artifact.read_bytes(),(folder/'history.bin').read_bytes())

    def test_portable_asserted_evidence_missing_legacy_and_wrong_cache_are_distinct(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);family='swmm:standard'
            base,evidence,_=self.produce(root,family)
            consumer=base.copy();bind(consumer,'RUNOFF','USE',evidence.artifact.path)
            portable=CacheEvidence.from_bytes(evidence.reuse_evidence.to_bytes())
            result=self.run_success(consumer,root/'asserted',family,
                interface_manifests={'RUNOFF':evidence.manifest},cache_evidence={'RUNOFF':portable},
                settings=dict(cache_reuse=(('RUNOFF','require_match'),)))
            self.assertEqual(result.consumed_caches[0].reuse.origin,'caller-asserted')
            cases=(('missing',dict(interface_manifests={'RUNOFF':evidence.manifest}),'run.cache_conditions'),
                ('legacy',dict(producers={'RUNOFF':replace(evidence,reuse_evidence=None)}),'run.cache_conditions'),
                ('no-layout',dict(cache_evidence={'RUNOFF':portable}),'run.incomplete_inspection'),
                ('wrong-cache',dict(interface_manifests={'RUNOFF':evidence.manifest},
                    cache_evidence={'RUNOFF':replace(portable,cache_sha256='a'*64)}),'run.cache_evidence'))
            # Public production records now reject contradictory provenance at
            # construction, before they can be supplied to Runner or archived.
            with self.assertRaisesRegex(ValueError,'production conditions'):
                replace(evidence,reuse_evidence=replace(portable,
                    context=replace(portable.context,input_sha256='a'*64)))
            for name,kwargs,code in cases:
                result=Runner().run(consumer,config(root/name,keep_failed_artifacts=False,cache_reuse=(('RUNOFF','require_match'),)),**kwargs)
                self.assertEqual(result.status,'rejected',result.failure)
                self.assertIn(code,{d.code for d in result.diagnostics.errors})
                self.assertIsNone(result.engine_objects)

    def test_external_series_relocation_content_change_and_capture_integrity(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);base=model();key=next(iter(base.timeseries))
            first=root/'first.dat';second=root/'second.dat'
            raw=b'0 0.6\n1:00 0\n3:00 0\n';first.write_bytes(raw);second.write_bytes(raw)
            base.timeseries.replace(key,FileTimeSeries(id=key,file=FileReference(path=str(first))))
            base,evidence,_=self.produce(root,'swmm:standard',base)
            consumer=base.copy();consumer.timeseries.replace(key,FileTimeSeries(id=key,file=FileReference(path=str(second))))
            bind(consumer,'RUNOFF','USE',evidence.artifact.path)
            result=self.run_success(consumer,root/'relocated','swmm:standard',producers={'RUNOFF':evidence},
                settings=dict(cache_reuse=(('RUNOFF','require_match'),)))
            self.assertEqual(result.consumed_caches[0].reuse.status,'matched')
            second.write_bytes(raw.replace(b'0.6',b'1.2'))
            result=Runner().run(consumer,config(root/'different',keep_failed_artifacts=False,
                cache_reuse=(('RUNOFF','require_match'),)),producers={'RUNOFF':evidence})
            self.assertEqual(result.status,'rejected',result.failure)
            self.assertIn('swmm:timeseries',result.consumed_caches[0].reuse.differences)
            second.write_bytes(raw)
            def corrupt(progress):
                if progress.phase=='opening':
                    stage=next(p for p in (root/'corrupt').glob('.easysewer-*') if p.is_dir())
                    resource=next(stage.glob('easysewer-assets-*/inputs/*.dat'))
                    resource.write_bytes(b'changed after comparison')
            result=Runner().run(consumer,config(root/'corrupt',keep_failed_artifacts=False,
                cache_reuse=(('RUNOFF','require_match'),)),producers={'RUNOFF':evidence},progress=corrupt)
            self.assertEqual(result.status,'rejected',result.failure)
            self.assertIn('run.resource_changed',{d.code for d in result.diagnostics.errors})
            self.assertIsNone(result.engine_objects)
