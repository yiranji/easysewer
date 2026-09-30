"""Aquifer/binding/expression semantics through the shared Model architecture."""

from dataclasses import replace
from itertools import product
import unittest

from easysewer.model import Model, Ref
from easysewer.model import groundwater as g
from easysewer.model import expressions as e
from easysewer.model.resources import Pattern
from easysewer.io.inp import InpDocument
from easysewer.io.inp.groundwater import GroundwaterExpressionCodec
from easysewer.io.hotstart import HotstartLayout
from easysewer.scenario import ScenarioPatch, RenameRecord, SetFields, FieldChange
from easysewer.validation import ValidationError
from test_quality_v2 import quality_model, ref
from test_scenario_v2 import portable
from test_files_v2 import bind


def groundwater_model(*,expressions=True):
    model=quality_model()
    model.patterns.add(Pattern(id='ET',kind='MONTHLY',factors=(.5,1.5)))
    model.aquifers.add(g.Aquifer(id='Aquifer',porosity=.45,wilting_point=.1,field_capacity=.25,conductivity=.2,
        conductivity_slope=10,tension_slope=15,upper_evaporation_fraction=.5,lower_evaporation_depth=5,
        deep_seepage=.001,bottom_elevation=-10,water_table_elevation=2,upper_moisture=.3,evaporation_pattern=ref('patterns','ET')))
    model.groundwater.add(g.Groundwater(subcatchment=ref('subcatchments','S'),aquifer=ref('aquifers','Aquifer'),node=ref('nodes','J'),
        surface_elevation=20,groundwater_coefficient=.001,groundwater_exponent=1.2,surface_water_coefficient=.0001,
        surface_water_exponent=1.1,interaction_coefficient=.00001,fixed_surface_depth=0))
    if expressions:
        codec=GroundwaterExpressionCodec()
        for kind,expr in (('LATERAL','0.002 * (HGW - HCB) + 0.0001 * HSW'),('DEEP','0.001 * (HGW / HGS)')):
            model.gwf.add(g.GroundwaterExpression(subcatchment=ref('subcatchments','S'),kind=kind,expression=codec.parse(expr)))
    return model


def groundwater_corpus():
    for flags in product((False,True),repeat=4):
        model=groundwater_model()
        changes={name:value for name,value,flag in zip(('threshold_elevation','bottom_elevation','water_table_elevation','upper_moisture'),(-1,-12,3,.35),flags) if flag}
        model.groundwater.update('S',**changes)
        yield model


class GroundwaterTests(unittest.TestCase):
    def assert_domain_equal(self,a,b):
        for namespace in ('aquifers','groundwater','gwf'):
            self.assertEqual(tuple(a.collection('swmm:'+namespace).items()),tuple(b.collection('swmm:'+namespace).items()),namespace)

    def test_all_optional_overrides_source_free_json_and_canonical_inp(self):
        for model in groundwater_corpus():
            with self.subTest(row=model.groundwater['S']):
                self.assertTrue(model.validate(for_run=True).is_valid,model.validate())
                restored=portable(model)
                self.assert_domain_equal(model,restored)
                document=restored.to_document()
                self.assertGreaterEqual(len(document.records('GROUNDWATER')[0].values),11)
                self.assert_domain_equal(model,Model.from_document(document,strict=True))
                self.assert_domain_equal(model,Model.from_document(restored.to_document(normalize=True),strict=True))
                self.assertEqual(HotstartLayout.from_model(model).subcatchments[0].groundwater,'Aquifer')

    def test_all_variables_functions_and_prefixes_use_shared_arithmetic(self):
        codec=GroundwaterExpressionCodec()
        for name in g.VARIABLE_DIMENSIONS:
            self.assertEqual(codec.parse(name.lower()+'Extra'),g.GroundwaterVariable(name=name))
        self.assertEqual(codec.parse('KSuffix'),g.GroundwaterVariable(name='KS'))
        for function in e.FUNCTIONS:
            expr=codec.parse(f'{function.lower()}(.5)')
            model=groundwater_model()
            model.gwf.update(('S','LATERAL'),expression=expr)
            self.assert_domain_equal(model,portable(model))
            self.assert_domain_equal(model,Model.from_document(model.to_document(),strict=True))
        for text in ('__import__(1)','H','FOO','HGW +','HGW;evil','STEP(1,2)'):
            with self.assertRaises(ValueError):codec.parse(text)
        from easysewer.model.controls import ControlExpression
        model=groundwater_model();model.controls.add(ControlExpression(id='Bad',expression=g.GroundwaterVariable(name='HGW')))
        self.assertIn('control.expression_variant',{d.code for d in model.validate().errors})

    def test_graph_renames_derived_keys_scenario_and_rollback(self):
        model=groundwater_model()
        model.aquifers.rename('Aquifer','Soil');model.nodes.rename('J','Receiving');model.patterns.rename('ET','Monthly')
        patch=ScenarioPatch(operations=(RenameRecord(target=ref('subcatchments','S'),new_id='Catchment'),))
        changed=patch.apply(model)
        self.assertIn('S',model.groundwater)
        model=changed.model
        self.assertIn('Catchment',model.groundwater)
        self.assertIn(('Catchment','DEEP'),model.gwf)
        self.assertEqual(model.groundwater['Catchment'].aquifer.key,'Soil')
        self.assertEqual(model.aquifers['Soil'].evaporation_pattern.key,'Monthly')
        with self.assertRaises(ValidationError):model.aquifers.remove('Soil')
        before=model.to_json_document().to_bytes()
        patch=ScenarioPatch(operations=(SetFields(target=ref('groundwater','Catchment'),changes=(FieldChange(name='water_table_elevation',value=30),)),))
        with self.assertRaises(ValidationError):patch.apply(model)
        self.assertEqual(model.to_json_document().to_bytes(),before)

    def test_units_coefficients_and_expression_variable_substitution(self):
        model=groundwater_model();original=model.gwf[('S','LATERAL')].expression
        model.convert_units('GPM')
        self.assertEqual(original,model.gwf[('S','LATERAL')].expression)
        model.convert_units('CMS')
        self.assertEqual(model.aquifers['Aquifer'].tension_slope,15*.3048)
        self.assertEqual(model.aquifers['Aquifer'].conductivity,.2*25.4)
        self.assertAlmostEqual(model.groundwater['S'].groundwater_coefficient,.001*(3048/43560)/.3048**1.2)
        self.assertAlmostEqual(model.groundwater['S'].interaction_coefficient,.00001*(3048/43560)/.3048**2)
        expression=model.gwf[('S','LATERAL')].expression
        self.assertIs(type(expression),e.BinaryExpression)
        self.assertEqual(expression.operator,'*')
        self.assertEqual(expression.left.value,3048/43560)
        self.assertTrue(any(type(n) is e.BinaryExpression and n.operator=='/' for n in e.walk_expression(expression)))
        self.assert_domain_equal(model,portable(model))
        self.assert_domain_equal(model,Model.from_document(model.to_document(),strict=True))

    def test_effective_soil_elevations_monthly_pattern_and_invalid_drafts(self):
        for changes in ({'surface_elevation':1},{'bottom_elevation':25},{'upper_moisture':.05}):
            model=groundwater_model();model.groundwater.update('S',**changes)
            self.assertFalse(model.validate().is_valid)
            with self.assertRaises(ValidationError):model.to_document()
        for changes in ({'porosity':.2},{'upper_moisture':.8},{'water_table_elevation':-11},{'conductivity':0}):
            model=groundwater_model();model.aquifers.update('Aquifer',**changes)
            self.assertFalse(model.validate().is_valid)
        model=groundwater_model();model.patterns.update('ET',kind='DAILY')
        self.assertFalse(model.validate().is_valid)
        model=groundwater_model();model.groundwater.update('S',aquifer='invalid')
        self.assertFalse(model.validate().is_valid)

    def test_repeated_assignments_native_tail_and_overwritten_source_reference(self):
        base=groundwater_model(expressions=False).to_document().text
        source=base+'[GROUNDWATER]\nS Missing Missing 20 .002 1 0 1 0 0 *\nS Aquifer J 20 .003 1 0 1 0 0 *suffix * * .32 extra\n[GWF]\nS LAT .001 * HGW\nS LATERAL .002 * HGW\n'
        model=Model.from_document(InpDocument.from_text(source),strict=True)
        self.assertEqual(model.to_document().text,source)
        self.assertEqual(model.groundwater['S'].groundwater_coefficient,.003)
        self.assertEqual(model.groundwater['S'].upper_moisture,.32)
        self.assertIn('groundwater.source_reference',{d.code for d in model.validate(for_run=True).errors})
        self.assertTrue(model.validate(for_run=True,normalize=True).is_valid)
        malformed=source+'[GWF]\nS LATERAL HGW +\n'
        bad=Model.from_document(InpDocument.from_text(malformed))
        self.assertNotIn(('S','LATERAL'),bad.gwf)
        self.assertFalse(bad.validate().is_valid)
        with self.assertRaises(ValidationError):bad.to_document()

    def test_hotstart_binding_changes_require_explicit_detachment(self):
        model=groundwater_model();bind(model,'HOTSTART','USE','state.hsf')
        before=model.to_json_document().to_bytes()
        for operation in (lambda:model.groundwater.remove('S'),lambda:model.groundwater.update('S',threshold_elevation=1),lambda:model.aquifers.rename('Aquifer','Other')):
            with self.assertRaises(ValidationError):operation()
            self.assertEqual(model.to_json_document().to_bytes(),before)
        model.files.remove(('HOTSTART','USE'));model.groundwater.remove('S')
        self.assertIsNone(HotstartLayout.from_model(model).subcatchments[0].groundwater)
        self.assertIn('groundwater.inactive_expression',{d.code for d in model.validate().diagnostics})

    def test_unsafe_native_arithmetic_source_capacity_is_checked_without_execution(self):
        base=groundwater_model(expressions=False).to_document().text
        for token in ('HGW'+'x'*255,'0.'+'1'*255):
            model=Model.from_document(InpDocument.from_text(base+'[GWF]\nS LATERAL '+token+'\n'),strict=True)
            self.assertIn('groundwater.native_token_capacity',{d.code for d in model.validate(for_run=True).errors})
            self.assertTrue(model.validate(for_run=True,normalize=True).is_valid)
        model=groundwater_model();model.gwf.update(('S','LATERAL'),expression=GroundwaterExpressionCodec().parse('+'.join(['HGW']*150)))
        self.assertIn('groundwater.native_line_capacity',{d.code for d in model.validate(for_run=True).errors})


if __name__=='__main__':
    unittest.main()
