"""Treatment relations, native lexical resolution and complete unit contracts."""

from dataclasses import dataclass, replace
from itertools import product
import unittest

from easysewer.io.inp import InpDocument
from easysewer.io.inp.treatment import TreatmentExpressionCodec
from easysewer.io.hotstart import HotstartLayout
from easysewer.io.runoff_cache import RunoffLayout
from easysewer.model import Model
from easysewer.model import treatment as t, expressions as e
from easysewer.model.network import Junction
from easysewer.scenario import ScenarioPatch, RenameRecord, SetFields, FieldChange, ChangePollutantUnits
from easysewer.validation import ValidationError
from test_quality_v2 import quality_model, ref
from test_scenario_v2 import portable


def expression(text):
    return TreatmentExpressionCodec().parse(text, pollutants=('Q0','Q1'))


def treatment_model(kinds=('C','R')):
    model = quality_model()
    for id,kind,text in zip(('Q0','Q1'), kinds, ('Q0*EXP(-.1*HRT)', '.2*R_Q0+0*Q1')):
        model.treatment.add(t.Treatment(node=ref('nodes','J'), pollutant=ref('pollutants',id), kind=kind, expression=expression(text)))
    return model


def treatment_corpus():
    for kinds in product(('C','R'), repeat=2):
        yield treatment_model(kinds)
    for name in t.PROCESS_VARIABLES:
        model = treatment_model()
        model.treatment.update(('J','Q0'), expression=expression(name))
        yield model


class TreatmentTests(unittest.TestCase):
    def assert_domain_equal(self, a, b):
        self.assertEqual(tuple(a.treatment.items()), tuple(b.treatment.items()))

    def test_all_kinds_variables_source_free_json_and_inp(self):
        for model in treatment_corpus():
            self.assertTrue(model.validate(for_run=True).is_valid, model.validate().errors)
            self.assert_domain_equal(model, portable(model))
            self.assert_domain_equal(model, Model.from_document(portable(model).to_document(), strict=True))
            self.assertEqual(tuple(p.id for p in HotstartLayout.from_model(model).pollutants), ('Q0','Q1'))
            self.assertEqual(RunoffLayout.from_model(model).subcatchments, ('S',))

    def test_functions_arithmetic_domains_and_invalid_syntax(self):
        for function in e.FUNCTIONS:
            model = treatment_model()
            model.treatment.update(('J','Q0'), expression=expression(f'{function}(.5)'))
            self.assert_domain_equal(model, portable(model))
            self.assert_domain_equal(model, Model.from_document(model.to_document(), strict=True))
        from easysewer.model.groundwater import GroundwaterVariable
        from easysewer.model.controls import ControlExpression
        model = treatment_model(); model.treatment.update(('J','Q0'), expression=GroundwaterVariable(name='HGW'))
        self.assertIn('treatment.expression_variant', {d.code for d in model.validate().errors})
        model = treatment_model(); model.controls.add(ControlExpression(id='Bad', expression=expression('Q0')))
        self.assertIn('control.expression_variant', {d.code for d in model.validate().errors})
        @dataclass(frozen=True, kw_only=True)
        class FutureTreatment(t.Treatment):
            unknown: float = 1.
        model = treatment_model()
        model.treatment.replace(('J','Q0'), FutureTreatment(node=ref('nodes','J'),pollutant=ref('pollutants','Q0'),kind='C',expression=expression('FLOW')))
        self.assertIn('treatment.record_variant', {d.code for d in model.validate().errors})
        with self.assertRaises(ValidationError): model.to_document()
        with self.assertRaises(ValidationError): model.convert_units('CMS')
        for source in ('Q0 +', 'C(Q0)', '__import__(1)', 'sqrt(Q0,Q1)', 'Unknown', 'R_Missing', 'Q0 + nan', ''):
            with self.assertRaises(ValueError): expression(source)

    def test_source_spelling_repeat_groups_comments_and_bad_overrides(self):
        base = quality_model().to_document().text
        source = base+'[TREATMENT]\nJ Q0 Removal=.2 ; old\nJ Q0 c anything = .5 * Q0 ; final\nJ Q1 C=DTsuffix ; process\n'
        model = Model.from_document(InpDocument.from_text(source), strict=True)
        self.assertEqual(model.to_document().text, source)
        self.assertEqual(model.treatment[('J','Q0')].kind, 'C')
        codes = {d.code for d in model.validate().diagnostics}
        self.assertTrue({'treatment.native_keyword','treatment.native_variable','treatment.repeated_assignment'} <= codes)
        model.treatment.update(('J','Q0'), expression=expression('.4*Q0'))
        changed = model.to_document().text
        self.assertEqual(len(InpDocument.from_text(changed).records('TREATMENT')), 2)
        self.assertIn('; old', changed); self.assertIn('; final', changed)
        self.assert_domain_equal(model, Model.from_document(model.to_document(), strict=True))
        for bad in ('J Q0 C=Q0+','J Q0 X=2','J Q0 C Q0','J Q0 C=Unknown'):
            invalid = Model.from_document(InpDocument.from_text(base+'[TREATMENT]\nJ Q0 C=Q0\n'+bad+'\n'))
            self.assertFalse(invalid.validate().is_valid)
            self.assertNotIn(('J','Q0'), invalid.treatment)
            with self.assertRaises(ValidationError): invalid.to_document()

    def test_native_variable_precedence_and_programmatic_shadowing(self):
        codec = TreatmentExpressionCodec()
        for name in t.PROCESS_VARIABLES:
            self.assertEqual(codec.parse(name.lower()+'tail', pollutants=(name+'tail',)), t.TreatmentProcessVariable(name=name))
        self.assertEqual(codec.parse('R_Q0', pollutants=('Q0','R_Q0')), t.TreatmentConcentration(pollutant=ref('pollutants','R_Q0')))
        model = treatment_model()
        model.pollutants.rename('Q0','FLOWER')
        self.assertIn('treatment.variable_shadowed', {d.code for d in model.validate().errors})
        with self.assertRaises(ValidationError): model.to_document()
        for name in ('ABS','bad-id','123','水质'):
            model = treatment_model(); model.pollutants.rename('Q0', name)
            self.assertFalse(model.validate().is_valid)
        model = treatment_model(); model.pollutants.rename('Q1','R_Q0')
        self.assertIn('treatment.variable_shadowed', {d.code for d in model.validate().errors})
        model = quality_model(); model.pollutants.rename('Q0','123')
        model.treatment.add(t.Treatment(node=ref('nodes','J'), pollutant=ref('pollutants','Q1'), kind='R',
            expression=t.TreatmentRemoval(pollutant=ref('pollutants','123'))))
        self.assertTrue(model.validate().is_valid)

    def test_removal_cycles_missing_equations_and_nonrecursive_concentrations(self):
        for kind in ('C','R'):
            model = treatment_model()
            model.treatment.update(('J','Q0'), kind=kind, expression=expression('R_Q1'))
            self.assertEqual(sum(d.code == 'treatment.removal_cycle' for d in model.validate().errors), 2)
            with self.assertRaises(ValidationError): model.to_document()
        model = treatment_model(); model.treatment.update(('J','Q0'), expression=expression('R_Q0'))
        self.assertIn('treatment.removal_cycle', {d.code for d in model.validate().errors})
        model = treatment_model(); model.treatment.update(('J','Q0'), expression=expression('Q1'))
        model.treatment.update(('J','Q1'), kind='C', expression=expression('Q0'))
        self.assertTrue(model.validate().is_valid)
        model.treatment.remove(('J','Q0'))
        model.treatment.update(('J','Q1'), expression=expression('R_Q0'))
        self.assertTrue(model.validate().is_valid)
        self.assertIn('treatment.missing_removal', {d.code for d in model.validate().diagnostics})
        model.nodes.replace('J', Junction(id='J', elevation=0))
        model.treatment.update(('J','Q1'), expression=expression('HRT'))
        self.assertIn('treatment.hrt_nonstorage', {d.code for d in model.validate().diagnostics})

    def test_graph_scenarios_derived_keys_and_atomic_failure(self):
        model = treatment_model()
        model.nodes.rename('J','Tank')
        result = ScenarioPatch(operations=(RenameRecord(target=ref('pollutants','Q0'), new_id='Solids'),)).apply(model)
        model = result.model
        self.assertIn(('Tank','Solids'), model.treatment)
        self.assertEqual(model.treatment[('Tank','Q1')].expression.left.right.pollutant.key, 'Solids')
        with self.assertRaises(ValidationError): model.pollutants.remove('Solids')
        self.assert_domain_equal(model, portable(model))
        before = model.to_json_document().to_bytes()
        patch = ScenarioPatch(operations=(SetFields(target=ref('treatment',('Tank','Solids')),
            changes=(FieldChange(name='expression',value=t.TreatmentRemoval(pollutant=ref('pollutants','Q1'))),)),))
        with self.assertRaises(ValidationError): patch.apply(model)
        self.assertEqual(before, model.to_json_document().to_bytes())
        model.pollutants.remove('Solids', cascade=True)
        self.assertEqual(len(model.treatment), 0)

    def test_hydraulic_conversion_scales_flow_depth_area_and_keeps_time_concentration(self):
        factors = {'CFS':1.,'GPM':448.831,'MGD':.64632,'CMS':.02832,'LPS':28.317,'MLD':2.4466}
        for units,factor in factors.items():
            model = treatment_model()
            model.treatment.update(('J','Q0'), expression=expression('FLOW+DEPTH+AREA+DT+HRT+Q0'))
            model.convert_units(units)
            length = 1 if units in ('CFS','GPM','MGD') else .3048
            def variable(name, factor): return name if factor == 1 else f'({name}/{factor!r})'
            expected = expression('+'.join((variable('FLOW',factor),variable('DEPTH',length),variable('AREA',length**2),'DT','HRT','Q0')))
            self.assertEqual(model.treatment[('J','Q0')].expression, expected)
        model = treatment_model(); model.treatment.update(('J','Q0'), expression=expression('FLOW'))
        model.convert_units('CMS', basis='physical')
        self.assertEqual(model.treatment[('J','Q0')].expression.right.value, .028316846592)

    def test_concentration_conversion_scales_inputs_outputs_but_not_removals(self):
        for kind in ('C','R'):
            model = treatment_model(); model.treatment.update(('J','Q0'), kind=kind, expression=expression('Q0+Q1'))
            model.treatment.update(('J','Q1'), expression=expression('Q0/(Q1+1)+R_Q0'))
            patch = ScenarioPatch(operations=(ChangePollutantUnits(target=ref('pollutants','Q0'), units='UG/L'),))
            converted = ScenarioPatch.from_json_document(patch.to_json_document()).apply(model).model
            self.assertEqual(converted.treatment[('J','Q0')].expression,
                expression('1000*((Q0/1000)+Q1)' if kind == 'C' else '(Q0/1000)+Q1'))
            self.assertEqual(converted.treatment[('J','Q1')].expression, expression('(Q0/1000)/(Q1+1)+R_Q0'))
            self.assert_domain_equal(converted, portable(converted))
            self.assert_domain_equal(converted, Model.from_document(converted.to_document(), strict=True))

    def test_overwritten_reference_preflight_and_native_expression_capacity(self):
        base = quality_model().to_document().text
        model = Model.from_document(InpDocument.from_text(base+'[TREATMENT]\nJ Q0 C=Q1\nJ Q0 C=Q0\n'), strict=True)
        model.pollutants.remove('Q1', cascade=True)
        self.assertIn('treatment.source_reference', {d.code for d in model.validate(for_run=True).errors})
        self.assertTrue(model.validate(for_run=True, normalize=True).is_valid)
        for text,code in (('FLOW'+'s'*260,'treatment.native_token_capacity'),
                ('0.'+'0'*260+'1','treatment.native_token_capacity'), (' '*1100+'Q0','treatment.native_line_capacity')):
            model = Model.from_document(InpDocument.from_text(base+'[TREATMENT]\nJ Q0 C = '+text+'\n'), strict=True)
            self.assertIn(code, {d.code for d in model.validate(for_run=True).errors})
            self.assertTrue(model.validate(for_run=True, normalize=True).is_valid)


if __name__ == '__main__':
    unittest.main()
