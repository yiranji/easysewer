"""Groundwater inheritance, empirical units and original expression sources."""
from dataclasses import dataclass, fields, is_dataclass, replace
from itertools import product
import unittest
from unittest.mock import patch

from easysewer.model import Model, Ref
from easysewer.model import groundwater as g, expressions as e
from easysewer.io.inp import InpDocument
from easysewer.io.inp.groundwater import GroundwaterExpressionCodec
from easysewer.schema.groundwater_fields import INHERITED
from easysewer.validation import ValidationError
from test_groundwater_v2 import groundwater_model
from test_hydrology_fields_v2 import fixture as hydrology_fixture, UNITS


def ref(namespace, key): return Ref(collection='swmm:' + namespace, key=key)
AQUIFER = ref('aquifers', 'Aquifer')
BINDING = ref('groundwater', 'S')
LATERAL = ref('gwf', ('S', 'LATERAL'))
DEEP = ref('gwf', ('S', 'DEEP'))
OPTIONAL = ('threshold_elevation', *INHERITED)
VALUES = (-1., -12., 3., .35)


def load(source):
    return Model.from_document(InpDocument.from_text(source, source='groundwater-fields.inp'), strict=True)


def fixture(units='CFS', flags=(False,)*4, mode='normal'):
    m = load(hydrology_fixture(units=units))
    sample = groundwater_model()
    m.patterns.add(sample.patterns['ET'])
    m.aquifers.add(sample.aquifers['Aquifer'])
    m.groundwater.add(sample.groundwater['S'])
    if mode != 'without-gwf':
        for row in sample.gwf.values(): m.gwf.add(row)
    if mode == 'without-pattern': m.aquifers.update('Aquifer', evaporation_pattern=None)
    if mode == 'saturated': m.groundwater.update('S', water_table_elevation=20, upper_moisture=.45)
    if mode == 'fixed-depth': m.groundwater.update('S', fixed_surface_depth=3)
    m.groundwater.update('S', **{name:value for name,value,flag in zip(OPTIONAL,VALUES,flags) if flag})
    return m.to_document().text


def queries(m):
    result=[]
    def walk(owner,value,path=()):
        if is_dataclass(value):
            for f in fields(value):
                p=path+(f.name,)
                result.extend((m.inspect_field(owner,p),m.field_provenance(owner,p)))
                walk(owner,getattr(value,f.name),p)
    for namespace in ('aquifers','groundwater','gwf'):
        for key,row in m.collection('swmm:'+namespace).items(): walk(ref(namespace,key),row)
    return tuple(result)


class GroundwaterFieldTests(unittest.TestCase):
    def test_inheritance_all_overrides_units_and_json(self):
        for units in UNITS:
            for flags in product((False,True),repeat=4):
                with self.subTest(units=units,flags=flags):
                    m=load(fixture(units,flags));before=queries(m)
                    self.assertEqual(queries(Model.from_json_document(m.to_json_document(),strict=True)),before)
                    for info in before[::2]:
                        self.assertEqual(info.semantics.effective.status,'known',info.path)
                        self.assertNotEqual(info.provenance.status,'unknown',info.path)
                    for name,value,flag in zip(OPTIONAL,VALUES,flags):
                        info=m.inspect_field(BINDING,name)
                        inherited=m.nodes['J'].elevation if name=='threshold_elevation' else getattr(m.aquifers['Aquifer'],name)
                        self.assertEqual(info.semantics.default.value,inherited)
                        self.assertEqual(info.semantics.effective.value,value if flag else inherited)
                    self.assertEqual(m.inspect_field(AQUIFER,'tension_slope').semantics.unit.value,'ft' if units in UNITS[:3] else 'm')
                    prefix='(cfs/acre)/(ft)' if units in UNITS[:3] else '(cms/ha)/(m)'
                    self.assertEqual(m.inspect_field(BINDING,'groundwater_coefficient').semantics.unit.value,prefix+'^1.2')
                    self.assertEqual(m.inspect_field(BINDING,'interaction_coefficient').semantics.unit.value,prefix+'^2')

    def test_markers_omission_zero_and_equal_explicit_inheritance(self):
        source=fixture(mode='without-gwf')
        m=load(source);line=m.document.records('GROUNDWATER')[0]
        base=' '.join(line.values[:10])
        for tail in ('*','*suffix * * *','-1e10 -1e10 -1e10 -1e10','0 -10 2 .3'):
            m=load(source+'[GROUNDWATER]\n'+base+' '+tail+'\n')
            for name in OPTIONAL:
                declarations=m.field_provenance(BINDING,name).declarations
                active=[d for d in declarations if d.contributes]
                column=OPTIONAL.index(name)
                if column>=len(tail.split()): self.assertFalse(active)
                else: self.assertEqual(active[0].role,'value' if tail.startswith('0 ') else 'marker')
            self.assertEqual(m.inspect_field(BINDING,'upper_moisture').semantics.effective.value,.3)
        for units in UNITS[3:]:
            m=load(fixture(units,mode='without-gwf'));v=m.document.records('GROUNDWATER')[0].values
            m=load(m.document.text+'[GROUNDWATER]\n'+' '.join(v[:10])+' -3048000000 -3048000000 -3048000000 -1e10\n')
            self.assertTrue(all(getattr(m.groundwater['S'],name) is None for name in OPTIONAL))

    def test_saturation_and_dynamic_node_depth_remain_configuration(self):
        m=load(fixture(mode='saturated'))
        self.assertEqual(m.inspect_field(BINDING,'water_table_elevation').semantics.effective.value,20)
        self.assertEqual(m.inspect_field(BINDING,'upper_moisture').semantics.effective.value,.45)
        fact=m.inspect_field(BINDING,'fixed_surface_depth').semantics.effective
        self.assertEqual(fact.value,0);self.assertIn('live',fact.reason)
        m.nodes.update('J',initial_depth=2)
        self.assertEqual(m.inspect_field(BINDING,'fixed_surface_depth').semantics.effective.value,0)
        self.assertIn('groundwater.initial_clamp',{d.code for d in m.validate().diagnostics})

    def test_original_expression_positions_repetition_parentheses_and_quotes(self):
        base=fixture(mode='without-gwf')
        expression='(HGW) + 2 * (HGW + 2)'
        m=load(base+'[GWF]\nS LAT '+expression+'\n')
        paths=(('left','name'),('right','right','left','name'),('right','left','value'),('right','right','right','value'))
        tokens=[m.field_provenance(LATERAL,('expression',)+p).declarations[0].tokens for p in paths]
        self.assertEqual([[t.value for t in row] for row in tokens],[['(HGW)'],['(HGW'],['2'],['2)']])
        self.assertNotEqual(tokens[0][0].start,tokens[1][0].start)
        self.assertNotEqual(tokens[2][0].start,tokens[3][0].start)
        self.assertEqual(m.field_provenance(LATERAL,('expression','operator')).declarations[0].tokens[0].value,'+')
        quoted=load(base+'[GWF]\nS LAT "'+expression+'" ; 注释\n')
        for p in paths:
            d=quoted.field_provenance(LATERAL,('expression',)+p).declarations[0]
            self.assertEqual(d.role,'derived');self.assertEqual(len(d.tokens),1);self.assertTrue(d.tokens[0].quoted)
        m=load(base+'[GWF]\nS LAT 1 + HGW\nS LATERAL HGW + 1\n')
        self.assertEqual([d.contributes for d in m.field_provenance(LATERAL,('expression',)).declarations],[False,True])
        d=m.field_provenance(LATERAL,('expression','left','name')).declarations
        self.assertEqual(len(d),1);self.assertEqual(d[0].tokens[0].value,'HGW')
        self.assertEqual(queries(Model.from_json_document(m.to_json_document(),strict=True)),queries(m))

    def test_trace_preserves_native_signed_grammar_and_all_variable_units(self):
        codec=GroundwaterExpressionCodec()
        expressions=['-2^2','2^-2','2^3^2','+(HGW - HCB) * .001','- HGW * .001',
                     '-.5 * HGW','((HGW))','HGW / HGS + HSW','HGW - 2']
        expressions += [f'{function}(.5)' for function in e.FUNCTIONS]
        expressions += [name.lower()+'Suffix' for name in g.VARIABLE_DIMENSIONS]
        for text in expressions:
            ast,spans=codec._parse_with_spans(text,codec.resolve_variable)
            self.assertEqual(ast,codec.parse(text))
            self.assertEqual(text[slice(*spans[()])],text)
            self.assertTrue(all(0<=a<b<=len(text) for a,b in spans.values()))
        ast,spans=codec._parse_with_spans('2^3^2',codec.resolve_variable)
        self.assertIsInstance(ast.right,e.BinaryExpression)
        self.assertEqual(spans[('right','operator')],(3,4))
        ast,spans=codec._parse_with_spans('- HGW * .001',codec.resolve_variable)
        self.assertIsInstance(ast,e.UnaryExpression);self.assertIsInstance(ast.operand,e.BinaryExpression)
        self.assertEqual(spans[('operator',)],(0,1));self.assertEqual(spans[('operand','operator')],(6,7))
        base=fixture(mode='without-gwf')
        for units in UNITS:
            from easysewer.model.units import UnitContext
            for name,dimension in g.VARIABLE_DIMENSIONS.items():
                m=load(base+'[GWF]\nS LATERAL '+name+'Suffix\n');m.reinterpret_units(units)
                self.assertEqual(m.inspect_field(LATERAL,('expression','name')).semantics.unit.value,UnitContext(flow_units=units).unit(dimension))
        m=load(fixture())
        self.assertEqual(m.inspect_field(LATERAL,'expression').semantics.unit.value,'cfs/acre')
        self.assertEqual(m.inspect_field(DEEP,'expression').semantics.unit.value,'in/h')
        info=m.inspect_field(LATERAL,('expression','left','left','value'))
        self.assertEqual(info.semantics.effective.status,'known');self.assertEqual(info.semantics.unit.status,'unknown')

    def test_invalid_references_patterns_extensions_inactive_and_no_file_io(self):
        m=load(fixture())
        with patch('builtins.open',side_effect=AssertionError('Queries must not open files')): queries(m)
        m.patterns.update('ET',kind='DAILY')
        self.assertEqual(m.inspect_field(BINDING,'surface_elevation').semantics.effective.status,'invalid')
        m=load(fixture());m.groundwater.update('S',water_table_elevation=21)
        self.assertEqual(m.inspect_field(BINDING,'water_table_elevation').semantics.effective.status,'invalid')
        m=load(fixture());m.groundwater.update('S',threshold_elevation=-1e10)
        self.assertEqual(m.inspect_field(BINDING,'threshold_elevation').semantics.effective.status,'invalid')
        m=load(fixture());m.groundwater.update('S',aquifer=ref('aquifers','Missing'))
        self.assertEqual(m.inspect_field(BINDING,'upper_moisture').semantics.effective.status,'invalid')
        m=load(fixture());m.groundwater.remove('S')
        self.assertEqual(m.inspect_field(LATERAL,'expression').semantics.effective.status,'not_applicable')
        @dataclass(frozen=True,kw_only=True)
        class Extension(g.Aquifer): pass
        m=load(fixture());row=m.aquifers['Aquifer']
        m.aquifers.add(Extension(**{f.name:getattr(row,f.name) for f in fields(row) if f.name!='id'},id='Extended'))
        m.groundwater.update('S',aquifer=ref('aquifers','Extended'))
        self.assertEqual(m.inspect_field(BINDING,'upper_moisture').semantics.effective.status,'unknown')

    def test_rename_rollback_conversion_and_failed_group_source(self):
        m=load(fixture());m.aquifers.rename('Aquifer','Soil');m.nodes.rename('J','Receiver');m.subcatchments.rename('S','Area')
        self.assertEqual(m.inspect_field(ref('groundwater','Area'),'bottom_elevation').semantics.effective.value,-10)
        self.assertEqual(m.field_provenance(ref('groundwater','Area'),('aquifer','key')).declarations[0].tokens[0].value,'Aquifer')
        before=m.to_json_document().to_bytes()
        with self.assertRaises(ValidationError): m.aquifers.remove('Soil')
        self.assertEqual(m.to_json_document().to_bytes(),before)
        m.convert_units('CMS')
        self.assertAlmostEqual(m.inspect_field(ref('groundwater','Area'),'bottom_elevation').semantics.effective.value,-3.048)
        self.assertEqual(queries(Model.from_json_document(m.to_json_document(),strict=True)),queries(m))
        source=fixture()+'[GWF]\nS LAT HGW +\n'
        bad=Model.from_document(InpDocument.from_text(source));self.assertNotIn(('S','LATERAL'),bad.gwf)
        self.assertFalse(bad.validate().is_valid)


if __name__=='__main__': unittest.main()
