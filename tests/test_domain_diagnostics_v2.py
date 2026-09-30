"""Cross-record semantic diagnostics use typed owners and contributing inputs."""
from dataclasses import dataclass, replace
from datetime import timedelta
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from easysewer.io.inp import InpDocument
from easysewer.model import Model
from easysewer.model import controls as c, groundwater as g, lid as l, quality as q, treatment as t
from easysewer.model.inflows import ConcentrationInflow, DryWeatherFlow
from easysewer.model.network import Junction
from easysewer.model.resources import Pattern
from easysewer.runtime._result_codec import Codec
from easysewer.validation import DiagnosticSubject, ValidationError
from test_groundwater_v2 import groundwater_model
from test_lid_v2 import lid_model, control, usage
from test_quality_v2 import quality_model, ref
from test_rdii_v2 import rdii_model, response
from test_treatment_v2 import treatment_model, expression
from test_controls_v2 import controlled, symbol
from test_regulators_v2 import regulator_model

EVIDENCE=[]


def parsed(model):
    return Model.from_document(InpDocument.from_text(model.to_document().text,source='original-domains.inp'),strict=True)


def subject(collection,key,*path):
    return DiagnosticSubject(collection='swmm:'+collection,key=key,path=path)


class DomainDiagnosticTests(unittest.TestCase):
    def check(self,m,code,*,run=True,portable=True):
        report=m.validate(for_run=run)
        ds=[d for d in report.diagnostics if d.code==code];self.assertTrue(ds,report)
        for d in ds:
            self.assertIsNotNone(d.subject);self.assertTrue(d.subject.collection.startswith('swmm:'))
            self.assertEqual({v.subject for v in d.locations},{d.subject,*d.related})
            self.assertEqual(Codec(None).decode(Codec(None).encode(d)),d)
            EVIDENCE.append(dict(code=code,collection=d.subject.collection,path=d.subject.path,
                                 statuses=[v.status for v in d.locations]))
        if portable:
            restored=Model.from_json_document(m.to_json_document(),strict=False)
            self.assertEqual(restored.validate(for_run=run),report)
        return ds[0]

    def test_coverage_aggregate_and_copollutant_units(self):
        m=parsed(quality_model());m.landuses.add(q.LandUse(id='Second'))
        m.coverages.add(q.Coverage(subcatchment=ref('subcatchments','S'),landuse=ref('landuses','Second'),percent=10))
        d=self.check(m,'quality.coverage_sum')
        self.assertEqual(d.subject,subject('subcatchments','S'))
        self.assertEqual(d.related,(subject('coverages',('S','Land'),'percent'),subject('coverages',('S','Second'),'percent')))
        self.assertEqual([v.status for v in d.locations],['context','current','programmatic'])
        m.pollutants.update('Q0',co_pollutant=ref('pollutants','Q1'),co_fraction=.1)
        d=self.check(m,'quality.co_units')
        self.assertEqual(d.subject,subject('pollutants','Q0','co_pollutant','key'))
        self.assertIn(subject('pollutants','Q1','units'),d.related)

    def test_inflow_composite_keys_and_overridden_patterns(self):
        m=quality_model();m.patterns.add(Pattern(id='First',kind='MONTHLY',factors=(1.,)))
        m.patterns.add(Pattern(id='Last',kind='MONTHLY',factors=(2.,)))
        m.dwf.add(DryWeatherFlow(node=ref('nodes','J'),baseline=1,patterns=(ref('patterns','First'),ref('patterns','Last'))))
        m.inflows.add(ConcentrationInflow(node=ref('nodes','J'),constituent=ref('pollutants','Q0'),baseline=1))
        m=parsed(m);d=self.check(m,'inflow.repeated_pattern_kind')
        self.assertEqual(d.subject,subject('dwf',('J','FLOW'),'patterns',1))
        self.assertEqual(d.locations[0].status,'untracked')
        self.assertTrue(d.locations[1].spans)
        self.assertIn(subject('patterns','First','kind'),d.related)
        d=self.check(m,'inflow.concentration_needs_flow')
        self.assertEqual(d.subject,subject('inflows',('J','POLLUTANT:Q0')))
        self.assertEqual(d.locations[-1].status,'absent')
        m.nodes.rename('J','Tank');d=self.check(m,'inflow.concentration_needs_flow')
        self.assertEqual(d.subject.key,('Tank','POLLUTANT:Q0'));self.assertEqual(d.locations[0].status,'changed')

    def test_groundwater_overrides_and_inherited_aquifer_values(self):
        m=parsed(groundwater_model());m.groundwater.update('S',surface_elevation=1)
        d=self.check(m,'groundwater.local_elevations')
        self.assertEqual(d.subject,subject('groundwater','S','surface_elevation'))
        self.assertEqual([v.status for v in d.locations],['changed','omitted','omitted','current','current'])
        self.assertIn(subject('aquifers','Aquifer','water_table_elevation'),d.related)
        m=parsed(groundwater_model());m.groundwater.update('S',upper_moisture=.9)
        d=self.check(m,'groundwater.local_moisture')
        self.assertEqual(d.subject.path,('upper_moisture',));self.assertIn(subject('aquifers','Aquifer','porosity'),d.related)
        m=parsed(groundwater_model());m.groundwater.update('S',surface_elevation=2)
        self.check(m,'groundwater.initial_clamp')
        m.groundwater.update('S',threshold_elevation=-1e10)
        d=self.check(m,'groundwater.native_missing_value');self.assertEqual(d.subject.path,('threshold_elevation',))
        m.groundwater.remove('S');d=self.check(m,'groundwater.inactive_expression')
        self.assertEqual(d.subject,subject('gwf',('S','LATERAL'),'expression'))
        self.assertEqual(d.locations[-1].status,'absent')

    def test_lid_aggregate_contributors_disabled_rows_and_units(self):
        m=parsed(lid_model());m.lid_usage.add(usage('Second',from_impervious=90,area=1e6))
        m.lid_usage.add(l.DisabledLidUsage(record_id='Disabled',subcatchment=ref('subcatchments','S'),
            control=ref('lid_controls','L'),parameters=('1','2','3','4','5')))
        d=self.check(m,'lid.capture_total')
        self.assertEqual(d.subject,subject('subcatchments','S'))
        self.assertEqual(d.related,(subject('lid_usage','lid-usage-1','from_impervious'),subject('lid_usage','Second','from_impervious')))
        d=self.check(m,'lid.area_total')
        self.assertEqual(d.subject.path,('area',));self.assertEqual(d.related[-1],subject('options','settings','flow_units'))
        self.assertNotIn('Disabled',{s.key for s in d.related})
        m=parsed(lid_model('RB',extra=False));d=self.check(m,'lid.barrel_cover_default')
        self.assertEqual(d.subject.path,('storage','covered'));self.assertEqual(d.locations[0].status,'omitted')
        m.lid_controls.update('L',drain=None);self.check(m,'lid.implicit_layer')
        m=parsed(lid_model('VS'));m.lid_usage.update('lid-usage-1',width=0)
        d=self.check(m,'lid.swale_width');self.assertIn(subject('lid_controls','L','kind'),d.related)
        m=parsed(lid_model());m.subcatchments.add(replace(m.subcatchments['S'],id='O'))
        m.lid_usage.update('lid-usage-1',drain_to=ref('nodes','O'));d=self.check(m,'lid.shadowed_drain')
        self.assertEqual({s.collection for s in d.related},{'swmm:nodes','swmm:subcatchments'})

    def test_extension_variant_diagnostics_keep_known_collection_identity(self):
        @dataclass(frozen=True,kw_only=True)
        class FutureControl(l.LidControl):pass
        @dataclass(frozen=True,kw_only=True)
        class FutureUsage(l.LidUsage):pass
        @dataclass(frozen=True,kw_only=True)
        class FutureTreatment(t.Treatment):pass
        m=parsed(lid_model());m.lid_controls.replace('L',FutureControl(**m.lid_controls['L'].__dict__))
        d=self.check(m,'lid.record_variant',portable=False);self.assertEqual(d.subject,subject('lid_controls','L'))
        m.lid_usage.replace('lid-usage-1',FutureUsage(**m.lid_usage['lid-usage-1'].__dict__))
        self.check(m,'lid.usage_variant',portable=False)
        m=parsed(treatment_model());m.treatment.replace(('J','Q0'),FutureTreatment(**m.treatment[('J','Q0')].__dict__))
        d=self.check(m,'treatment.record_variant',portable=False);self.assertEqual(d.subject,subject('treatment',('J','Q0')))

    def test_rdii_month_override_indices_and_aggregate_context(self):
        m=rdii_model();m.hydrographs.update('UH',responses=(response(fraction=.8),response(kind='LONG',fraction=.4),response(month='FEB',fraction=.1)))
        # Invalid semantic ratios remain parseable, so retain original INP by
        # modifying a previously valid source instead of bypassing export checks.
        original=rdii_model().to_document().text
        prefix=original[:original.index('[HYDROGRAPHS]')]
        source=prefix+'[HYDROGRAPHS]\nUH R\nUH ALL SHORT .8 1 2\nUH ALL LONG .4 1 2\nUH FEB SHORT .1 1 2\n[RDII]\nJ UH 2\n'
        m=Model.from_document(InpDocument.from_text(source,source='original-domains.inp'))
        d=self.check(m,'rdii.response_sum')
        self.assertEqual(d.subject.path,('responses',))
        self.assertEqual([v.path for v in d.related],[('responses',0),('responses',1)])
        self.assertEqual([v.status for v in d.locations],['current','untracked','untracked'])
        ds=[d for d in m.validate().diagnostics if d.code=='rdii.response_sum'];self.assertEqual(len(ds),11)
        self.assertFalse(any(d.message.startswith('FEB') for d in ds))
        m=parsed(rdii_model());m.hydrographs.update('UH',responses=(response(fraction=1.005),))
        self.check(m,'rdii.response_tolerance')
        m.hydrographs.update('UH',responses=(response(peak=.00005),))
        self.check(m,'rdii.truncated_seconds');self.check(m,'rdii.inactive_response')
        m.hydrographs.update('UH',rain_gage=None);d=self.check(m,'rdii.missing_gage')
        self.assertIn(subject('rdii','J','hydrograph'),d.related)

    def test_treatment_dependency_cycles_missing_targets_and_shadowing(self):
        m=parsed(treatment_model());m.treatment.update(('J','Q0'),expression=expression('R_Q1'))
        d=self.check(m,'treatment.removal_cycle')
        self.assertEqual(d.subject,subject('treatment',('J','Q0'),'expression'))
        self.assertEqual(d.related,(subject('treatment',('J','Q1'),'expression'),))
        m=parsed(treatment_model());m.treatment.remove(('J','Q0'))
        d=self.check(m,'treatment.missing_removal');self.assertEqual(d.locations[1].status,'absent')
        m.nodes.replace('J',Junction(id='J',elevation=0));m.treatment.update(('J','Q1'),expression=expression('HRT'))
        d=self.check(m,'treatment.hrt_nonstorage');self.assertEqual(d.related,(subject('nodes','J'),))
        m=parsed(treatment_model());m.pollutants.rename('Q0','FLOWER')
        d=self.check(m,'treatment.variable_shadowed');self.assertIn(subject('pollutants','FLOWER'),d.related)

    def test_control_namespaces_transitive_references_and_typed_paths(self):
        m=controlled('VARIABLE Same = NODE J DEPTH\nEXPRESSION Mean = Same\nRULE Same\nIF Mean > 1\nTHEN CONDUIT P STATUS = OPEN\n')
        row=m.controls[('RULE','Same')]
        m.controls.update(('RULE','Same'),conditions=(replace(row.conditions[0],right=c.Constant(value='ON')),))
        d=self.check(m,'control.native_constant_type')
        self.assertEqual(d.subject,subject('controls',('RULE','Same'),'conditions',0,'right','value'))
        self.assertEqual(d.locations[0].status,'untracked');self.assertTrue(d.locations[1].spans)
        self.assertIn(subject('controls',('VARIABLE','Same')),d.related)
        self.assertIn(subject('controls',('EXPRESSION','Mean')),d.related)
        self.assertIn(subject('nodes','J'),d.related)
        m.controls.rename(('RULE','Same'),'Renamed');d=self.check(m,'control.native_constant_type')
        self.assertEqual(d.subject.key,('RULE','Renamed'))
        self.assertEqual(d.locations[1].status,'changed')

    def test_all_control_semantic_branches(self):
        m=controlled('VARIABLE V = NODE J DEPTH\nEXPRESSION E = V\nRULE R\nIF V > 1\nTHEN CONDUIT P STATUS = OPEN\n')
        m.controls.rename(('VARIABLE','V'),'LongName'*5);self.check(m,'control.symbol_length',run=False)
        m.controls.rename(('VARIABLE','LongName'*5),'DEPTH');self.check(m,'control.reserved_variable',run=False)
        m=controlled('VARIABLE V = NODE J DEPTH\nEXPRESSION E = V\n')
        m.controls.update(('EXPRESSION','E'),expression=g.GroundwaterVariable(name='HGW'))
        self.check(m,'control.expression_variant',run=False,portable=False)
        m.controls.update(('EXPRESSION','E'),expression=c.FunctionExpression(function='FUTURE',argument=c.ExpressionNumber(value=1)))
        self.check(m,'control.function',run=False)
        m.controls.rename(('VARIABLE','V'),'bad-name')
        m.controls.update(('EXPRESSION','E'),expression=c.ExpressionVariable(reference=symbol('VARIABLE','bad-name')))
        self.check(m,'control.expression_name',run=False)
        for premise,code in [('IF OUTLET P SETTING > 0','control.native_outlet_setting'),
                             ('IF LINK P LENGTH > 0','control.native_missing_attribute')]:
            m=controlled(premise.replace('IF','RULE R\nIF',1)+'\nTHEN OUTLET P SETTING = .5\n',model=regulator_model('FUNCTIONAL/HEAD'))
            self.check(m,code)
        m=controlled('RULE R\nIF SIMULATION MONTH > 12\nTHEN CONDUIT P STATUS = OPEN\n')
        row=m.controls[('RULE','R')]
        m.controls.update(('RULE','R'),conditions=(replace(row.conditions[0],right=c.Constant(value=13)),))
        self.check(m,'control.calendar_range')
        m=controlled('RULE R\nIF NODE J DEPTH > 0\nTHEN CONDUIT P STATUS = OPEN\n')
        row=m.controls[('RULE','R')]
        m.controls.update(('RULE','R'),then_actions=(replace(row.then_actions[0],object_type='PUMP',setting=c.StatusSetting(value='ON')),))
        self.check(m,'control.action_target_kind')
        m.controls.update(('RULE','R'),conditions=(replace(row.conditions[0],left=c.Constant(value=1)),))
        self.check(m,'control.operand_position')
        m=controlled('RULE R\nIF SIMULATION TIME > 00:01:00\nAND NODE J DEPTH > 0\nTHEN ORIFICE P SETTING = PID .1 1 .05\n',model=regulator_model('SIDE'))
        self.check(m,'control.short_circuit_controller');self.check(m,'control.native_time_controller')
        from test_hydrology_v2 import hydrology_model
        base=hydrology_model();base.raingages.add(replace(base.raingages['R'],id='Unused'))
        m=controlled('VARIABLE Rain = GAGE Unused 8-HR_DEPTH\n',model=base)
        self.check(m,'control.native_rain_attribute');self.check(m,'control.unused_gage')

    def test_validation_uses_retained_source_without_io_and_rolls_back(self):
        m=parsed(lid_model());before=m.to_json_document()
        with self.assertRaises(ValidationError):
            with m.transaction():m.lid_usage.update('lid-usage-1',area=1e9)
        self.assertEqual(m.to_json_document(),before)
        m.lid_usage.update('lid-usage-1',area=1e9)
        with patch('builtins.open',side_effect=AssertionError('no IO')),patch.object(Model,'to_document',side_effect=AssertionError('no render')):
            d=next(d for d in m.validate().errors if d.code=='lid.area_total')
        self.assertEqual(d.related[1],subject('lid_usage','lid-usage-1','area'))

    def test_rejected_runner_result_preserves_aggregate_evidence(self):
        from easysewer.runtime import Runner, RunResult
        from test_runner_v2 import config
        m=parsed(lid_model());m.lid_usage.add(usage('Second',area=1e9))
        with tempfile.TemporaryDirectory() as directory,patch('ctypes.CDLL',side_effect=AssertionError('No solver load')):
            root=Path(directory);result=Runner().run(m,config(root/'run',keep_failed_artifacts=False))
            self.assertEqual(result.status,'rejected')
            d=next(d for d in result.diagnostics.errors if d.code=='lid.area_total')
            self.assertEqual(d,self.check(m,'lid.area_total'))
            result.save(root/'archive');self.assertEqual(RunResult.load(root/'archive'),result)

    def test_resume_revalidation_error_is_labelled_before_backend_probe(self):
        # A synthetic container tests Python rejection only, not native state.
        import test_runner_checkpoint_context_v2 as cp
        from easysewer.model import FileReference
        from easysewer.runtime import Runner, ResumeConfig, RunResult
        with tempfile.TemporaryDirectory() as directory,patch('ctypes.CDLL',side_effect=AssertionError('No solver load')):
            root=Path(directory);snapshot=cp.fixture.snapshot(root)
            raw=snapshot.input_bytes+b'\n[OPTIONS]\nFLOW_UNITS INVALID\n'
            snapshot=replace(snapshot,input_bytes=raw,input_sha256=hashlib.sha256(raw).hexdigest())
            context=cp.context(snapshot);saved=cp.capture(cp.FakeSession(snapshot),context,root/'saved')
            result=Runner(backends={}).resume(saved,ResumeConfig(output_directory=FileReference(path=str(root/'out'),direction='output')))
            self.assertEqual(result.status,'rejected');self.assertEqual(result.failure.stage,'checkpoint_load')
            self.assertEqual(result.diagnostics.diagnostics[:len(context.diagnostics.diagnostics)],context.diagnostics.diagnostics)
            errors=[d for d in result.diagnostics.errors if d.span]
            self.assertTrue(errors);self.assertTrue(all(d.span.source=='checkpoint-input:'+snapshot.input_sha256 for d in errors))
            self.assertFalse(any(d.code=='run.backend' for d in result.diagnostics.errors))
            result.save(root/'archive');self.assertEqual(RunResult.load(root/'archive'),result)


if __name__=='__main__':unittest.main()
