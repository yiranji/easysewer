"""Dependency evidence, explicit reuse purposes and extensible comparisons."""

from dataclasses import replace
from datetime import time, timedelta
import hashlib
import json
from types import SimpleNamespace
import unittest

from easysewer.io.inp import InpDocument
from easysewer.io.json import JsonDocument
from easysewer.model import Model,Ref,FileReference,Point
from easysewer.model.resources import FileTimeSeries
from easysewer.runtime import (CacheContext,CacheEvidence,CacheReuse,CachePolicy,CachePolicies,
    ResourceSnapshot,RunConfig,swmm_cache_policies)
from easysewer.runtime.cache_reuse import RUNOFF_POLICY
from test_files_v2 import bind
from test_native_v2_groundwater import literal_groundwater


def model():
    return Model.from_document(InpDocument.from_text(literal_groundwater()),strict=True)


def snapshot(value, *, resources=(), backend_settings=None):
    raw=value.to_document().text.encode()
    backend=SimpleNamespace(sha256='b'*64,capabilities=('easysewer:runoff-physics:1',),
        key='swmm:standard',numerical_policy='epa-swmm:5.2.4')
    return SimpleNamespace(input_bytes=raw,input_sha256=hashlib.sha256(raw).hexdigest(),
        profile=value.profile,backend=backend,resources=resources,backend_settings=backend_settings)


def compare(left,right,**kwargs):
    policies=swmm_cache_policies()
    producer=policies.capture('RUNOFF',left);consumer=policies.capture('RUNOFF',right)
    return policies.assess(CacheEvidence(cache_sha256='c'*64,context=producer),consumer,
        cache_sha256='c'*64,**kwargs)


class CacheReuseTests(unittest.TestCase):
    def test_events_are_physical_conditions_and_old_missing_facts_are_not_assumed(self):
        from test_events_v2 import period
        base=model();changed=base.copy()
        changed.update_events(periods=(period(),))
        result=compare(snapshot(base),snapshot(changed),intent='require_match')
        self.assertEqual(result.status,'changed')
        self.assertIn('swmm:events',result.differences)
        self.assertFalse(result.allowed)
        self.assertEqual(compare(snapshot(changed),snapshot(changed.copy())).status,'matched')
        policies=swmm_cache_policies();context=policies.capture('RUNOFF',snapshot(base))
        facts=context.facts.data;facts.pop('swmm:events')
        older=replace(context,facts=JsonDocument.from_data(facts))
        result=policies.assess(CacheEvidence(cache_sha256='c'*64,context=older),context,
            cache_sha256='c'*64,intent='require_match')
        self.assertEqual(result.status,'changed')
        self.assertFalse(result.allowed)
        self.assertIn('cache:required-facts-unavailable',result.reasons)
        result=policies.assess(CacheEvidence(cache_sha256='c'*64,context=older),older,
            cache_sha256='c'*64,intent='require_match')
        self.assertEqual(result.status,'unknown')
        self.assertIn('cache:required-facts-unavailable',result.reasons)

    def test_all_lid_variants_include_layers_and_deployments_without_report_paths(self):
        from test_native_v2_lid import literal_source,TYPE_LAYERS
        for kind in TYPE_LAYERS:
            with self.subTest(kind=kind):
                base=Model.from_document(InpDocument.from_text(literal_source(kind,detail='first.txt')),strict=True)
                changed=base.copy();key=next(iter(changed.lid_usage))
                changed.lid_usage.update(key,report_file=FileReference(path='different.txt',direction='output'))
                self.assertEqual(compare(snapshot(base),snapshot(changed)).status,'matched')
                changed.lid_usage.update(key,initial_saturation=75)
                self.assertIn('swmm:lid_usage',compare(snapshot(base),snapshot(changed)).differences)
                control=base.lid_controls['L'];changed=base.copy()
                field='surface' if control.surface is not None else 'storage'
                parameter='roughness' if field=='surface' else 'thickness'
                layer=getattr(control,field)
                changed.lid_controls.update('L',**{field:replace(layer,**{parameter:getattr(layer,parameter)*1.5})})
                self.assertIn('swmm:lid_controls',compare(snapshot(base),snapshot(changed)).differences)

    def test_model_changes_include_groundwater_hydraulic_feedback_and_quality(self):
        base=model()
        cases=(('subcatchments','S',dict(area=base.subcatchments['S'].area*2),'swmm:subcatchments'),
            ('groundwater','S',dict(bottom_elevation=-12.),'swmm:groundwater-bindings'),
            ('groundwater','S',dict(node=Ref(collection='swmm:nodes',key='O')),'swmm:groundwater-bindings'),
            ('nodes','J',dict(elevation=1.),'swmm:nodes'),
            ('aquifers','Aquifer',dict(conductivity=.4),'swmm:aquifers'))
        for collection,key,fields,difference in cases:
            with self.subTest(collection=collection,fields=fields):
                changed=base.copy();changed.collection('swmm:'+collection).update(key,**fields)
                result=compare(snapshot(base),snapshot(changed),intent='require_match')
                self.assertEqual(result.status,'changed');self.assertIn(difference,result.differences)
                self.assertFalse(result.allowed)
                self.assertTrue(replace(result,intent='frozen').allowed)
        for field,value in (('ignore_quality',True),('ignore_groundwater',True),('start_time',time(0,1)),
                            ('rule_step',timedelta(seconds=13))):
            changed=base.copy();changed.update_options(**{field:value})
            self.assertIn('swmm:effective-options',compare(snapshot(base),snapshot(changed)).differences)

    def test_report_paths_maps_and_cached_binding_do_not_change_conditions(self):
        base=model();producer=base.copy();bind(producer,'RUNOFF','SAVE','first.bin')
        consumer=base.copy();bind(consumer,'RUNOFF','USE','other.bin')
        consumer.update_report(disabled=True,averages=True)
        consumer.update_options(report_start_time=time(1),temp_directory=FileReference(path='different',direction='output'))
        consumer.nodes.update('J',position=Point(x=999,y=-50))
        result=compare(snapshot(producer),snapshot(consumer),intent='require_match')
        self.assertEqual(result.status,'matched',result);self.assertTrue(result.allowed)
        self.assertEqual(CacheReuse.from_data(result.to_data()),result)
        explicit=base.copy();aquifer=base.aquifers['Aquifer']
        explicit.groundwater.update('S',bottom_elevation=aquifer.bottom_elevation,
            water_table_elevation=aquifer.water_table_elevation,upper_moisture=aquifer.upper_moisture,
            threshold_elevation=base.nodes['J'].elevation)
        self.assertEqual(compare(snapshot(base),snapshot(explicit)).status,'matched')

    def test_external_resource_content_and_missing_evidence(self):
        base=model();key=next(iter(base.timeseries))
        base.timeseries.replace(key,FileTimeSeries(id=key,file=FileReference(path='first.dat')))
        def resource(sha):
            return ResourceSnapshot(owner=Ref(collection='swmm:timeseries',key=key),field=('file',),
                role='swmm:timeseries',format='swmm:timeseries.data',kind='file',access='read',active=True,
                required=True,original_path='unused',relative_path='unused',sha256=sha,size=13)
        same=base.copy();same.timeseries.replace(key,FileTimeSeries(id=key,file=FileReference(path='renamed.dat')))
        self.assertEqual(compare(snapshot(base,resources=(resource('d'*64),)),snapshot(same,resources=(resource('d'*64),))).status,'matched')
        result=compare(snapshot(base,resources=(resource('d'*64),)),snapshot(same,resources=(resource('e'*64),)))
        self.assertEqual(result.status,'changed');self.assertIn('swmm:timeseries',result.differences)
        result=compare(snapshot(base),snapshot(same))
        self.assertEqual(result.status,'unknown');self.assertIn('cache:resource-evidence-unavailable',result.reasons)

    def test_hotstart_and_backend_physics_are_dependencies_but_trace_is_not(self):
        base=model();bind(base,'HOTSTART','USE','state.hsf')
        def resource(sha):
            return ResourceSnapshot(owner=Ref(collection='swmm:files',key=('HOTSTART','USE')),field=('file',),
                role='swmm:interface.hotstart',format='swmm:hotstart.interface',kind='file',access='read',
                active=True,required=True,original_path=None,relative_path=None,sha256=sha,size=100)
        left=snapshot(base,resources=(resource('d'*64),));right=snapshot(base,resources=(resource('e'*64),))
        self.assertIn('swmm:initial-interfaces',compare(left,right).differences)
        left= snapshot(model());right=snapshot(model())
        for item in (left,right):item.backend.key='easysewer:flexible-ponding'
        def settings(ratio,trace):
            return JsonDocument.from_data(dict(policy=dict(external_flooding_ratio=ratio,record_steps=bool(trace)),trace=trace,input_sha256='f'*64)).to_bytes()
        left.backend_settings=settings(.5,'a');right.backend_settings=settings(.5,None)
        self.assertEqual(compare(left,right).status,'matched')
        right.backend_settings=settings(.7,None)
        self.assertIn('easysewer:backend-settings',compare(left,right).differences)

    def test_evidence_json_old_missing_future_policy_and_invalid_bindings(self):
        policies=swmm_cache_policies();context=policies.capture('RUNOFF',snapshot(model()))
        evidence=CacheEvidence(cache_sha256='c'*64,context=context)
        restored=CacheEvidence.from_bytes(evidence.to_bytes());self.assertEqual(restored,evidence)
        self.assertEqual(restored.context.sha256,context.sha256)
        with self.assertRaises(ValueError):policies.assess(evidence,context,cache_sha256='d'*64)
        self.assertEqual(policies.assess(None,context,cache_sha256='c'*64).status,'unknown')
        future=replace(context,policy='plugin:future:2',facts=JsonDocument.from_data({'plugin:extra':[1,2]}))
        unknown=CacheEvidence.from_bytes(CacheEvidence(cache_sha256='c'*64,context=future).to_bytes())
        self.assertEqual(unknown.context.facts.data,{'plugin:extra':[1,2]})
        self.assertEqual(policies.assess(unknown,future,cache_sha256='c'*64).status,'unknown')
        incomplete=replace(context,facts=JsonDocument.from_data({'swmm:calendar':{}}))
        self.assertEqual(policies.assess(CacheEvidence(cache_sha256='c'*64,context=incomplete),incomplete,cache_sha256='c'*64).status,'unknown')
        anonymous=replace(context,engine_sha256=None)
        self.assertEqual(policies.assess(CacheEvidence(cache_sha256='c'*64,context=anonymous),anonymous,cache_sha256='c'*64).status,'unknown')
        with self.assertRaises(ValueError):CacheReuse(status='matched',reasons=())
        value=json.loads(evidence.to_bytes());value['context']['limitations']='not an array'
        with self.assertRaises(TypeError):CacheEvidence.from_bytes(json.dumps(value).encode())

    def test_unknown_engine_profile_input_and_explicit_policy_extension(self):
        policies=swmm_cache_policies();snap=snapshot(model());snap.backend.capabilities=()
        context=policies.capture('RUNOFF',snap);self.assertIn('cache:unverified-engine',context.limitations)
        snap=snapshot(model());snap.input_bytes+=b'[FUTURE]\nunknown 42\n';snap.input_sha256=hashlib.sha256(snap.input_bytes).hexdigest()
        self.assertIn('cache:unsupported-input',policies.capture('RUNOFF',snap).limitations)
        snap.input_sha256='a'*64
        with self.assertRaises(ValueError):policies.capture('RUNOFF',snap)
        snap=snapshot(model())
        def capture(value):
            return CacheContext(policy='plugin:demo:1',kind='DEMO',input_sha256=value.input_sha256,
                engine_sha256=value.backend.sha256,facts=JsonDocument.from_data({'plugin:value':42}))
        custom=CachePolicies(policies=(CachePolicy(key='plugin:demo:1',kind='DEMO',capture=capture,
            compare=lambda a,b:('matched',(),())),))
        context=custom.capture('DEMO',snap)
        result=custom.assess(CacheEvidence(cache_sha256='c'*64,context=context),context,cache_sha256='c'*64,intent='require_match')
        self.assertEqual(result.status,'matched');self.assertTrue(result.allowed)
        self.assertEqual(policies.assess(CacheEvidence(cache_sha256='c'*64,context=context),context,cache_sha256='c'*64).status,'unknown')

    def test_config_roundtrip_legacy_defaults_and_bad_intents(self):
        config=RunConfig(output_directory=FileReference(path='out',direction='output'),cache_reuse=(('RUNOFF','require_match'),('HOTSTART','frozen')))
        self.assertEqual(RunConfig.from_json_document(config.to_json_document()).cache_reuse,config.cache_reuse)
        data=config.to_json_document().data;data['settings'].pop('cache_reuse')
        self.assertEqual(RunConfig.from_json_document(JsonDocument.from_data(data)).cache_intent('RUNOFF'),'inspect')
        for policy in ((('RUNOFF','wrong'),),(('RUNOFF','inspect'),('RUNOFF','frozen')),['RUNOFF']):
            with self.assertRaises(ValueError):replace(config,cache_reuse=policy)
