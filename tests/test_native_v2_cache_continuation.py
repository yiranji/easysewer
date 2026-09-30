"""Actual RDII replay and partial HOTSTART continuation on both backends."""

from dataclasses import replace
from datetime import time, timedelta
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from easysewer import get_native_capabilities
from easysewer.io.inp import InpDocument
from easysewer.io.hotstart import HotstartData
from easysewer.io.hotstart_manifest import HotstartManifest
from easysewer.io.runoff_cache import RdiiData
from easysewer.model import Model, Ref
from easysewer.runtime import CacheEvidence, Runner
from test_cache_continuation_v2 import absolute_series, continuation
from test_files_v2 import bind, routing
from test_hydrology_v2 import hydrology_model
from test_native_v2_files import selected
from test_native_v2_lid import literal_source
from test_rdii_v2 import rdii_model, response
from test_runner_v2 import config


@unittest.skipUnless(get_native_capabilities()['swmm_solver'] and get_native_capabilities()['flexible_ponding'],'Both solvers required')
class NativeCacheContinuationTests(unittest.TestCase):
    def solve(self,model,path,family='swmm:standard',**kwargs):
        settings=kwargs.pop('settings',{})
        result=Runner().run(model,config(path,backend=family,**settings),**kwargs)
        self.assertTrue(result.succeeded,repr(result.failure)+' '+repr(result.diagnostics.errors))
        return result

    def produce_rdii(self,folder,family,*,dry=False):
        base=selected(rdii_model());base.update_options(routing_step=timedelta(seconds=60),allow_ponding=True)
        if dry:base.hydrographs.update('UH',responses=(response(fraction=0),))
        producer=base.copy();bind(producer,'RDII','SAVE',folder/'history.rdii')
        run=self.solve(producer,folder/'producer',family)
        return base,run.produced_caches[0],run

    def test_rdii_shared_start_prefix_and_header_only_dry_coverage(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for family in ('swmm:standard','easysewer:flexible-ponding'):
                for dry in (False,True):
                    folder=root/(family.split(':')[-1]+str(dry));folder.mkdir()
                    base,evidence,_=self.produce_rdii(folder,family,dry=dry)
                    data=RdiiData.from_bytes(evidence.artifact.read_bytes())
                    self.assertEqual(bool(data.frames),not dry)
                    consumer=base.copy();consumer.update_options(end_date=base.effective_options.start.date(),end_time=time(12))
                    fresh=self.solve(consumer,folder/'fresh',family)
                    bind(consumer,'RDII','USE',evidence.artifact.path)
                    replay=self.solve(consumer,folder/'replay',family,producers={'RDII':evidence},
                        settings=dict(cache_reuse=(('RDII','require_match'),)))
                    self.assertEqual(replay.consumed_caches[0].reuse.status,'matched')
                    with fresh.open_output() as a,replay.open_output() as b:
                        self.assertEqual(a.series(None,'swmm:rdii_inflow').values,b.series(None,'swmm:rdii_inflow').values)
                    # Neither silent dry gaps nor a header-only file prove
                    # coverage outside the producer's declared full calendar.
                    consumer.update_options(end_date=base.effective_options.end.date(),end_time=time(1))
                    phases=[]
                    bad=Runner().run(consumer,config(folder/'outside',backend=family,
                        keep_failed_artifacts=False,cache_reuse=(('RDII','require_match'),)),
                        producers={'RDII':evidence},progress=lambda p:phases.append(p.phase))
                    self.assertEqual(bad.status,'rejected',bad.failure)
                    self.assertIn('cache:rdii-time-coverage',bad.consumed_caches[0].reuse.differences)
                    self.assertNotIn('opening',phases)

    def test_rdii_frozen_changed_area_and_portable_assertions(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for family in ('swmm:standard','easysewer:flexible-ponding'):
                folder=root/family.split(':')[-1];folder.mkdir()
                base,evidence,producer=self.produce_rdii(folder,family)
                changed=base.copy();changed.rdii.update('J',sewer_area=4)
                fresh=self.solve(changed,folder/'fresh',family)
                bind(changed,'RDII','USE',evidence.artifact.path)
                replay=self.solve(changed,folder/'frozen',family,interface_manifests={'RDII':evidence.manifest},
                    cache_evidence={'RDII':CacheEvidence.from_bytes(evidence.reuse_evidence.to_bytes())},
                    settings=dict(cache_reuse=(('RDII','frozen'),)))
                reuse=replay.consumed_caches[0].reuse
                self.assertEqual((reuse.status,reuse.origin),('changed','caller-asserted'))
                self.assertIn('swmm:rdii',reuse.differences)
                with fresh.open_output() as a,replay.open_output() as b,producer.open_output() as c:
                    actual=b.series(None,'swmm:rdii_inflow').values
                    self.assertEqual(actual,c.series(None,'swmm:rdii_inflow').values)
                    self.assertGreater(max(a.series(None,'swmm:rdii_inflow').values),max(actual)*1.99)

    def test_binary_rdii_condition_assertion_cannot_certify_text_interface(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);base,evidence,_=self.produce_rdii(root,'swmm:standard')
            document=routing();shift=base.effective_options.start-document.frames[0].time
            document=replace(document,frames=tuple(replace(f,time=f.time+shift) for f in document.frames))
            path=root/'text.rdii';raw=document.to_bytes();path.write_bytes(raw)
            bind(base,'RDII','USE',path)
            assertion=replace(evidence.reuse_evidence,cache_sha256=hashlib.sha256(raw).hexdigest())
            result=Runner().run(base,config(root/'wrong-format',keep_failed_artifacts=False,
                cache_reuse=(('RDII','require_match'),)),cache_evidence={'RDII':assertion})
            self.assertEqual(result.status,'rejected',result.failure)
            self.assertIn('run.cache_evidence',{d.code for d in result.diagnostics.errors})
            self.assertIsNone(result.engine_objects)
            # Text remains usable under its existing named-node format guard.
            run=self.solve(base,root/'text-default')
            self.assertEqual(run.consumed_caches[0].reuse.status,'unknown')

    def test_current_hotstart_condition_assertion_cannot_certify_legacy_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);producer,consumer=continuation(absolute_series(selected(hydrology_model())))
            producer.update_options(routing_step=timedelta(seconds=60));consumer.update_options(routing_step=timedelta(seconds=60))
            producer.files.remove(('HOTSTART','SAVE'));bind(producer,'HOTSTART','SAVE',root/'state.hsf')
            run=self.solve(producer,root/'producer');evidence=run.produced_caches[0]
            document=HotstartData.from_bytes(evidence.artifact.read_bytes(),layout=evidence.manifest.layout)
            raw=replace(document,version=3,nodes=tuple(replace(n,residence_time=None) for n in document.nodes)).to_bytes()
            legacy=root/'legacy.hsf';legacy.write_bytes(raw)
            consumer.files.remove(('HOTSTART','USE'));bind(consumer,'HOTSTART','USE',legacy)
            manifest=HotstartManifest.asserted(raw,layout=evidence.manifest.layout)
            assertion=replace(evidence.reuse_evidence,cache_sha256=hashlib.sha256(raw).hexdigest())
            result=Runner().run(consumer,config(root/'wrong-version',keep_failed_artifacts=False,
                cache_reuse=(('HOTSTART','require_match'),)),interface_manifests={'HOTSTART':manifest},
                cache_evidence={'HOTSTART':assertion})
            self.assertEqual(result.status,'rejected',result.failure)
            self.assertIn('run.cache_evidence',{d.code for d in result.diagnostics.errors})
            self.assertIsNone(result.engine_objects)

    def test_hotstart_boundary_conditions_preserve_partial_scope_and_reject_before_open(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for family in ('swmm:standard','easysewer:flexible-ponding'):
                folder=root/family.split(':')[-1];folder.mkdir()
                base=absolute_series(selected(hydrology_model()))
                base.update_options(routing_step=timedelta(seconds=60),allow_ponding=True)
                producer,consumer=continuation(base)
                producer.files.remove(('HOTSTART','SAVE'));bind(producer,'HOTSTART','SAVE',folder/'state.hsf')
                run=self.solve(producer,folder/'producer',family);evidence=run.produced_caches[0]
                self.assertEqual(evidence.applicability.status,'partial')
                consumer.files.remove(('HOTSTART','USE'));bind(consumer,'HOTSTART','USE',evidence.artifact.path)
                good=self.solve(consumer,folder/'continuation',family,producers={'HOTSTART':evidence},
                    settings=dict(cache_reuse=(('HOTSTART','require_match'),)))
                self.assertEqual(good.consumed_caches[0].reuse.status,'matched')
                self.assertIn('partial',json.dumps(evidence.reuse_evidence.context.facts.data))
                for name,edit in (('time',lambda m:m.update_options(start_time=time(6,1))),
                                  ('geometry',lambda m:m.nodes.update('J',elevation=1))):
                    changed=consumer.copy();edit(changed);destination=folder/name;destination.mkdir()
                    for file in ('model.inp','model.rpt','model.out'):(destination/file).write_bytes(b'previous')
                    phases=[]
                    result=Runner().run(changed,config(destination,backend=family,overwrite=True,
                        keep_failed_artifacts=False,cache_reuse=(('HOTSTART','require_match'),)),
                        producers={'HOTSTART':evidence},progress=lambda p:phases.append(p.phase))
                    self.assertEqual(result.status,'rejected',result.failure);self.assertNotIn('opening',phases)
                    for file in ('model.inp','model.rpt','model.out'):self.assertEqual((destination/file).read_bytes(),b'previous')

    def test_lid_split_run_is_not_a_complete_checkpoint_even_when_conditions_match(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for family in ('swmm:standard','easysewer:flexible-ponding'):
                folder=root/family.split(':')[-1];folder.mkdir()
                base=absolute_series(selected(Model.from_document(InpDocument.from_text(literal_source('BC')),strict=True)))
                base.update_options(routing_step=timedelta(seconds=60),allow_ponding=True)
                producer,consumer=continuation(base)
                full=base.copy();full.update_options(end_date=consumer.effective_options.end.date(),end_time=time(12))
                uninterrupted=self.solve(full,folder/'full',family)
                producer.files.remove(('HOTSTART','SAVE'));bind(producer,'HOTSTART','SAVE',folder/'state.hsf')
                before=self.solve(producer,folder/'first',family);evidence=before.produced_caches[0]
                consumer.files.remove(('HOTSTART','USE'));bind(consumer,'HOTSTART','USE',evidence.artifact.path)
                after=self.solve(consumer,folder/'second',family,producers={'HOTSTART':evidence},
                    settings=dict(cache_reuse=(('HOTSTART','require_match'),)))
                self.assertEqual(after.consumed_caches[0].reuse.status,'matched')
                self.assertEqual(evidence.applicability.status,'partial')
                with uninterrupted.open_output() as a,after.open_output() as b:
                    target=Ref(collection='swmm:subcatchments',key='S')
                    continuous=a.series(target,'swmm:runoff');split=b.series(target,'swmm:runoff')
                    by_time=dict(zip(continuous.times,continuous.values))
                    errors=[abs(value-by_time[t]) for t,value in zip(split.times,split.values)]
                    self.assertGreater(max(errors),1e-6)


if __name__=='__main__':unittest.main()
