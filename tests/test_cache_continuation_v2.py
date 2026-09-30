"""Distinct antecedent-history and partial-state continuation contracts."""

from dataclasses import replace
from datetime import time, timedelta
import unittest

from easysewer.io.json import JsonDocument
from easysewer.model import FileReference
from easysewer.model.resources import FileTimeSeries
from easysewer.runtime import CacheEvidence, swmm_cache_policies
from test_cache_reuse_v2 import snapshot
from test_files_v2 import bind
from test_hydrology_v2 import hydrology_model
from test_rdii_v2 import rdii_model


def compare(kind,left,right):
    policies=swmm_cache_policies()
    return policies.assess(CacheEvidence(cache_sha256='c'*64,context=policies.capture(kind,snapshot(left))),
        policies.capture(kind,snapshot(right)),cache_sha256='c'*64,intent='require_match')


def absolute_series(model):
    start=model.effective_options.start
    for key,row in list(model.timeseries.items()):
        if hasattr(row,'points'):
            model.timeseries.update(key,points=tuple(replace(p,time=start+p.time)
                if isinstance(p.time,timedelta) else p for p in row.points))
    return model


def continuation(base):
    producer=base.copy();consumer=base.copy();day=base.effective_options.start.date()
    producer.update_options(end_date=day,end_time=time(6))
    consumer.update_options(start_date=day,start_time=time(6),end_date=day,end_time=time(12))
    bind(producer,'HOTSTART','SAVE','new.hsf');bind(consumer,'HOTSTART','USE','new.hsf')
    return producer,consumer


class CacheContinuationTests(unittest.TestCase):
    def test_rdii_shared_antecedents_allow_shorter_end_but_not_later_start_or_extension(self):
        base=rdii_model();producer=base.copy();consumer=base.copy()
        bind(producer,'RDII','SAVE','generated.bin');bind(consumer,'RDII','USE','relocated.bin')
        consumer.update_report(disabled=True)
        # Keep a positive shorter simulation including dry intervals.
        consumer.update_options(end_date=base.effective_options.start.date(),end_time=time(12))
        self.assertEqual(compare('RDII',producer,consumer).status,'matched')
        consumer.update_options(start_time=time(1))
        result=compare('RDII',producer,consumer)
        self.assertIn('cache:rdii-antecedent-start',result.differences)
        self.assertNotIn('cache:rdii-time-coverage',result.differences)
        self.assertFalse(result.allowed)
        consumer=base.copy();consumer.update_options(end_time=time(1))
        self.assertIn('cache:rdii-time-coverage',compare('RDII',producer,consumer).differences)

    def test_rdii_monthly_rtk_initial_abstraction_recovery_area_and_rain_are_conditions(self):
        base=rdii_model();row=base.hydrographs['UH'];first=row.responses[0]
        for key,value in (('fraction',.2),('time_to_peak',1.5),('recession_ratio',3),
                ('maximum_abstraction',.2),('initial_abstraction',.05),('recovery_rate',.3)):
            with self.subTest(key=key):
                changed=base.copy();changed.hydrographs.update('UH',responses=(replace(first,**{key:value}),*row.responses[1:]))
                self.assertIn('swmm:hydrographs',compare('RDII',base,changed).differences)
        changed=base.copy();changed.rdii.update('J',sewer_area=4)
        self.assertIn('swmm:rdii',compare('RDII',base,changed).differences)
        changed=base.copy();rain=changed.timeseries['Rain']
        changed.timeseries.update('Rain',points=tuple(replace(p,value=p.value*2) for p in rain.points))
        self.assertIn('swmm:timeseries',compare('RDII',base,changed).differences)
        changed=base.copy();changed.update_options(wet_step=timedelta(seconds=17))
        self.assertIn('swmm:effective-options',compare('RDII',base,changed).differences)

    def test_hotstart_continuation_and_geometry_are_separate_from_serialized_layout(self):
        producer,consumer=continuation(absolute_series(hydrology_model()))
        result=compare('HOTSTART',producer,consumer)
        self.assertEqual(result.status,'matched',result)
        context=swmm_cache_policies().capture('HOTSTART',snapshot(producer))
        self.assertEqual(context.facts.data['swmm:hotstart-state-scope']['checkpoint'],'partial')
        consumer.update_options(start_time=time(6,1))
        self.assertIn('cache:hotstart-continuation-time',compare('HOTSTART',producer,consumer).differences)
        producer,consumer=continuation(absolute_series(hydrology_model()))
        consumer.nodes.update('J',elevation=1)
        self.assertIn('swmm:nodes',compare('HOTSTART',producer,consumer).differences)
        consumer=producer.copy()
        self.assertIn('cache:hotstart-continuation-time',compare('HOTSTART',producer,consumer).differences)

    def test_hotstart_relative_and_uninspected_external_time_origins_do_not_match(self):
        producer,consumer=continuation(hydrology_model())
        self.assertIn('swmm:time-origins',compare('HOTSTART',producer,consumer).differences)
        base=absolute_series(hydrology_model())
        base.timeseries.replace('Rain',FileTimeSeries(id='Rain',file=FileReference(path='rain.dat')))
        producer,consumer=continuation(base)
        result=compare('HOTSTART',producer,consumer)
        self.assertIn('swmm:time-origins',result.differences)
        self.assertIn('cache:resource-evidence-unavailable',result.reasons)

    def test_missing_invalid_calendar_future_fields_and_wrong_engine_do_not_certify(self):
        policies=swmm_cache_policies()
        for kind in ('RDII','HOTSTART'):
            context=policies.capture(kind,snapshot(rdii_model()))
            for calendar in (None,{},dict(start='bad',end='bad'),
                    dict(start='2020-01-01T00:00:00+00:00',end='2020-01-02T00:00:00+00:00'),
                    dict(start='2020-01-02',end='2020-01-01')):
                with self.subTest(kind=kind,calendar=calendar):
                    data=context.facts.data;data['swmm:calendar']=calendar
                    bad=replace(context,facts=JsonDocument.from_data(data))
                    result=policies.assess(CacheEvidence(cache_sha256='c'*64,context=bad),bad,cache_sha256='c'*64)
                    self.assertEqual(result.status,'unknown',result)
            snap=snapshot(rdii_model());snap.backend.capabilities=()
            self.assertEqual(policies.capture(kind,snap).limitations,('cache:unverified-engine',))
            self.assertEqual(CacheEvidence.from_bytes(CacheEvidence(cache_sha256='c'*64,context=context).to_bytes()).context,context)
            for key in ('swmm:calendar','swmm:hotstart-state-scope' if kind=='HOTSTART' else 'swmm:rdii-history'):
                data=context.facts.data;data.pop(key)
                bad=replace(context,facts=JsonDocument.from_data(data))
                self.assertNotEqual(policies.assess(CacheEvidence(cache_sha256='c'*64,context=bad),bad,cache_sha256='c'*64).status,'matched')

    def test_hotstart_cannot_claim_complete_checkpoint_through_a_modified_scope_fact(self):
        producer,consumer=continuation(absolute_series(hydrology_model()))
        policies=swmm_cache_policies();contexts=[]
        for model in (producer,consumer):
            context=policies.capture('HOTSTART',snapshot(model));data=context.facts.data
            data['swmm:hotstart-state-scope']['checkpoint']='complete'
            contexts.append(replace(context,facts=JsonDocument.from_data(data)))
        result=policies.assess(CacheEvidence(cache_sha256='c'*64,context=contexts[0]),contexts[1],cache_sha256='c'*64)
        self.assertEqual(result.status,'unknown');self.assertIn('cache:unsupported-condition-semantics',result.reasons)


if __name__=='__main__':unittest.main()
