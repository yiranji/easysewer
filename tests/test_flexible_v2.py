"""Explicit ponding policy, units, conservation bounds and preparation."""

from dataclasses import replace
import math
import json
from pathlib import Path
from types import SimpleNamespace
import unittest

from easysewer.io.json import JsonDocument
from easysewer.model import FileReference, Model
from easysewer.io.inp import InpDocument
from easysewer.runtime import FlexiblePondingBackend,FlexiblePondingPolicy,RunConfig
from easysewer.runtime.flexible import adjust_ponding
from easysewer.validation import ValidationError


SOURCE='''[OPTIONS]
FLOW_UNITS CFS
FLOW_ROUTING DYNWAVE
ALLOW_PONDING YES
START_DATE 01/01/2020
END_DATE 01/01/2020
END_TIME 00:01:00
REPORT_STEP 00:00:05
ROUTING_STEP 1
VARIABLE_STEP 0
[OUTFALLS]
O -1 FREE
[JUNCTIONS]
J 0 1 0 0 100
[CONDUITS]
P J O 100 .013 0 0
[XSECTIONS]
P CIRCULAR .2 0 0 0 1
[INFLOWS]
J FLOW Pulse FLOW 1 1
[TIMESERIES]
Pulse 0:00:00 10
Pulse 0:00:15 0
Pulse 0:00:30 20
Pulse 0:00:45 0
Pulse 0:01:00 0
[REPORT]
NODES ALL
LINKS ALL
'''


def ponding_model(units='CFS'):
    model=Model.from_document(InpDocument.from_text(SOURCE),strict=True)
    if units!='CFS':model.convert_units(units)
    return model


def configuration(path, policy=None, **options):
    return RunConfig(output_directory=FileReference(path=str(path),direction='output'),
        backend='easysewer:flexible-ponding',extensions=JsonDocument.from_data({
            'easysewer:flexible-ponding':(policy or FlexiblePondingPolicy()).to_json_document().data}),**options)


def plan(model, policy=None):
    return FlexiblePondingBackend().prepare_run(model,configuration('runs',policy),
        SimpleNamespace(input_sha256='a'*64),artifact_directory=Path('assets')/'backend')


class FlexiblePolicyTests(unittest.TestCase):
    def test_explicit_policy_roundtrip_no_silent_fallback_and_ratio_endpoints(self):
        value=FlexiblePondingPolicy(external_flooding_ratio=1,depth_threshold_m=.15,flow_threshold_cms=.02,record_steps=False)
        self.assertEqual(FlexiblePondingPolicy.from_json_document(value.to_json_document()),value)
        self.assertEqual(FlexiblePondingPolicy(external_flooding_ratio=0).external_flooding_ratio,0)
        for change in ({'external_flooding_ratio':-1},{'external_flooding_ratio':1.01},
            {'external_flooding_ratio':True},{'depth_threshold_m':math.nan},{'flow_threshold_cms':math.inf},
            {'record_steps':1},{'policy':'unrecognized'}):
            with self.subTest(change=change),self.assertRaises((TypeError,ValueError)):replace(value,**change)
        with self.assertRaises(TypeError):FlexiblePondingPolicy.from_json_document(JsonDocument.from_data({'external_flooding_raito':.2}))
        schema=Path(__file__).resolve().parents[1]/'docs/flexible-ponding-policy-1.0.schema.json'
        self.assertEqual(json.loads(schema.read_text(encoding='utf-8')),FlexiblePondingPolicy.json_schema())

    def test_adjustment_uses_increment_not_initial_storage_and_conserves_removed_water(self):
        result=adjust_ponding(previous_depth=1,previous_volume=1000,depth=1.2,volume=1020,overflow=5,
            area=100,dt=2,ratio=.5,depth_threshold=.3,flow_threshold=.1,flow_per_volume_rate=1)
        self.assertAlmostEqual(result.removed_volume,10)
        self.assertAlmostEqual(result.depth,1.1)
        self.assertAlmostEqual(result.volume,1010)
        self.assertAlmostEqual(result.external_flow*2,1020-result.volume)
        self.assertAlmostEqual(result.overflow+result.external_flow,5)

    def test_overflow_volume_cap_prevents_negative_flow_and_dry_storage_removal(self):
        result=adjust_ponding(previous_depth=0,previous_volume=0,depth=.2,volume=1000,overflow=2,
            area=100,dt=3,ratio=1,depth_threshold=0,flow_threshold=0,flow_per_volume_rate=448.831)
        self.assertAlmostEqual(result.removed_volume,6/448.831)
        self.assertAlmostEqual(result.overflow,0)
        self.assertLess(result.removed_volume,20)
        self.assertGreater(result.volume,999)

    def test_falling_stationary_negative_overflow_and_nonpositive_volume_growth_are_inert(self):
        common=dict(previous_depth=1,previous_volume=100,depth=2,volume=200,overflow=10,
            area=100,dt=1,ratio=.5,depth_threshold=.3,flow_threshold=.1,flow_per_volume_rate=1)
        for changes in ({'depth':.9},{'depth':1},{'overflow':-2},{'overflow':.1},{'volume':100},{'ratio':0}):
            result=adjust_ponding(**(common|changes))
            self.assertEqual(result.removed_volume,0)
        for changes in ({'dt':0},{'area':0},{'volume':-1},{'overflow':math.nan},{'ratio':2},{'dt':True}):
            with self.subTest(changes=changes),self.assertRaises(ValueError):adjust_ponding(**(common|changes))
        with self.assertRaisesRegex(ValueError,'numeric range'):
            adjust_ponding(**(common|dict(previous_depth=0,previous_volume=0,depth=1e308,
                volume=1e308,overflow=1e308,area=1,dt=1e-10,flow_per_volume_rate=1e-10)))

    def test_six_flow_units_have_independent_flow_volume_and_geometry_factors(self):
        q=(1,448.831,.64632,.02832,28.317,2.4466)
        for index,units in enumerate(('CFS','GPM','MGD','CMS','LPS','MLD')):
            with self.subTest(units=units):
                parameters=plan(ponding_model(units)).parameters.data
                vf=1 if index<3 else .02832;lf=1 if index<3 else .3048
                self.assertAlmostEqual(parameters['flow_per_volume_rate'],q[index]/vf)
                self.assertAlmostEqual(parameters['depth_threshold'],.3/.3048*lf)
                self.assertAlmostEqual(parameters['flow_threshold'],.1/.02832*q[index])
                self.assertAlmostEqual(parameters['nodes'][0]['volume_per_depth'],100*vf/lf)

    def test_preparation_binds_current_snapshot_and_rejects_incompatible_routing(self):
        model=ponding_model();before=model.to_json_document().to_bytes()
        value=plan(model)
        self.assertEqual(value.parameters.data['input_sha256'],'a'*64)
        self.assertEqual(value.parameters.data['nodes'][0]['id'],'J')
        self.assertEqual(model.to_json_document().to_bytes(),before)
        for change in ({'allow_ponding':False},{'flow_routing':'KINWAVE'},{'ignore_routing':True}):
            changed=model.copy();changed.update_options(**change)
            with self.subTest(change=change),self.assertRaises(ValidationError):plan(changed)
        self.assertEqual(plan(model,FlexiblePondingPolicy(record_steps=False)).artifacts,())


class FlexibleNativeGuardTests(unittest.TestCase):
    def solver(self, *, failure=None, clock=1., setter=True):
        from easysewer.runtime._native_flexible import NativeFlexibleSolver
        class Library:
            calls=[]
            values={310:2.,305:200.,308:10.,311:0.}
            def swmm_execRouting(self):self.calls.append('route');return 101 if failure=='route' else 0
            def swmm_getCurrentTime(self):return clock/86400
            def swmm_getPondingStep(self):return clock-solver.time
            def swmm_getValue(self,code,index):return self.values[code]
            def swmm_setValue(self,code,index,value):
                self.calls.append(code)
                if setter:self.values[code]=value
            def swmm_saveResults(self):self.calls.append('save');return 202 if failure=='save' else 0
            def swmm_getError(self,buffer,size):buffer.value=b'injected native error';return 0
        solver=object.__new__(NativeFlexibleSolver);solver.lib=Library();solver.time=0.;solver.duration=10.;solver.steps=0
        solver.policy=FlexiblePondingPolicy(depth_threshold_m=0,flow_threshold_cms=0)
        solver.parameters=dict(depth_threshold=0.,flow_threshold=0.,flow_per_volume_rate=1.,quality_enabled=False)
        solver.pollutants=[]
        solver.trace=None;solver.records=[dict(id='J',index=4,area=100.,volume_per_depth=100.,removed_volume=0.,active_steps=0)]
        solver.previous={4:dict(depth=1.,volume=100.,overflow=10.)}
        return solver

    def test_native_route_failure_prevents_setters_and_save(self):
        from easysewer.runtime._native_solver import NativeCallFailure
        solver=self.solver(failure='route')
        with self.assertRaises(NativeCallFailure) as caught:solver.step()
        self.assertEqual(caught.exception.failure.stage,'exec_routing')
        self.assertEqual(solver.lib.calls,['route'])

    def test_zero_or_invalid_clock_prevents_state_updates(self):
        for clock in (0.,-1.,math.nan,11.):
            with self.subTest(clock=clock):
                solver=self.solver(clock=clock)
                with self.assertRaises(ValueError):solver.step()
                self.assertEqual(solver.lib.calls,['route'])

    def test_void_setter_must_read_back_and_save_errors_propagate(self):
        from easysewer.runtime._native_solver import NativeCallFailure
        solver=self.solver(setter=False)
        with self.assertRaisesRegex(ValueError,'read-back'):solver.step()
        self.assertNotIn('save',solver.lib.calls)
        solver=self.solver(failure='save')
        with self.assertRaises(NativeCallFailure) as caught:solver.step()
        self.assertEqual(caught.exception.failure.stage,'save_results')

    def test_inactive_step_refreshes_history_before_later_activation(self):
        solver=self.solver();solver.parameters['depth_threshold']=3.
        solver.step();self.assertEqual(solver.previous[4]['volume'],200)
        solver.lib.values.update({310:4.,305:400.,308:1000.});solver.lib.swmm_getCurrentTime=lambda:2/86400
        solver.lib.swmm_getPondingStep=lambda:1.
        solver.step();self.assertAlmostEqual(solver.records[0]['removed_volume'],100.)


if __name__=='__main__':unittest.main()
