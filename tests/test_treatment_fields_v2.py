"""Treatment field context, empirical units and original arithmetic sources."""
from dataclasses import dataclass, fields, is_dataclass
from datetime import timedelta
from itertools import product
import unittest
from unittest.mock import patch

from easysewer.model import Model, Ref
from easysewer.model import treatment as t, expressions as e, network as n, quality as q
from easysewer.io.inp import InpDocument
from easysewer.io.inp.treatment import TreatmentExpressionCodec
from easysewer.validation import ValidationError

UNITS=('CFS','GPM','MGD','CMS','LPS','MLD')
POLLUTANT_UNITS=('MG/L','UG/L','#/L')
NODE_KINDS=('STORAGE','JUNCTION','DIVIDER','OUTFALL')
FORMULAS=tuple('.1+.01*'+f+'(.5)' for f in e.FUNCTIONS)+tuple('.1+.000001*'+v for v in t.PROCESS_VARIABLES)+('.1+.01*(-2^2)', '.1+.00001*(2^3^2)', '.1+.01*(-.5^2)', '.1+.01*SGN(0)')


def ref(kind,key): return Ref(collection='swmm:'+kind,key=key)
def load(text,strict=True):return Model.from_document(InpDocument.from_text(text,source='treatment-fields.inp'),strict=strict)
def expression(text):return TreatmentExpressionCodec().parse(text,pollutants=('Q0','Q1'))


def fixture(node_kind='STORAGE',units='CFS',pollutant_units='MG/L',kinds=('C','R'),*,rows=None):
    from test_native_v2_treatment import literal_base
    m=load(literal_base(node_kind,units));m.update_options(report_step=timedelta(minutes=5))
    m.reinterpret_pollutant_units('Q0',pollutant_units)
    node='O' if node_kind=='OUTFALL' else 'J'
    if rows is None:
        first='.8*Q0' if kinds[0]=='C' else '.25'
        second='.1*Q0+.5*Q1+.1*R_Q0' if kinds[1]=='C' else '.02*Q0+.2*R_Q0'
        rows=f'{node} Q0 {kinds[0]}={first}\n{node} Q1 {kinds[1]} = {second}\n'
    return m.to_document().text+'[TREATMENT]\n'+rows


def queries(m):
    result=[]
    def walk(owner,value,path=()):
        if is_dataclass(value):
            for f in fields(value):
                p=path+(f.name,);result.extend((m.inspect_field(owner,p),m.field_provenance(owner,p)));walk(owner,getattr(value,f.name),p)
    for key,row in m.treatment.items():walk(ref('treatment',key),row)
    return tuple(result)


def materialize(m):
    for key,row in tuple(m.treatment.items()):
        fact=m.inspect_field(ref('treatment',key),'expression').semantics.effective
        if fact.status!='known':raise AssertionError(fact)
        m.treatment.update(key,expression=fact.value)


class TreatmentFieldTests(unittest.TestCase):
    def test_four_nodes_six_units_three_pollutant_units_both_kinds_json(self):
        for kind,units,pu,first,second in product(NODE_KINDS,UNITS,POLLUTANT_UNITS,('C','R'),('C','R')):
            with self.subTest(node=kind,units=units,pollutant_units=pu,kinds=(first,second)):
                m=load(fixture(kind,units,pu,(first,second)));before=queries(m)
                for info in before[::2]:
                    self.assertEqual(info.semantics.effective.status,'known',info.path)
                    self.assertNotEqual(info.provenance.status,'unknown',info.path)
                self.assertEqual(queries(Model.from_json_document(m.to_json_document(),strict=True)),before)

    def test_process_and_pollutant_units_without_runtime_evaluation(self):
        for units in UNITS:
            expected={'HRT':'hour','DT':'s','FLOW':units,'DEPTH':'ft' if units in UNITS[:3] else 'm','AREA':'ft2' if units in UNITS[:3] else 'm2'}
            for name,unit in expected.items():
                m=load(fixture('JUNCTION',units,rows=f'J Q0 R=.1+.001*{name}\n'));owner=ref('treatment',('J','Q0'))
                info=m.inspect_field(owner,('expression','right','right','name'))
                self.assertEqual(info.semantics.unit.value,unit);self.assertEqual(info.semantics.effective.value,name)
                self.assertEqual(m.inspect_field(owner,'expression').semantics.unit.value,'1')
                self.assertEqual(m.inspect_field(owner,('expression','right','left','value')).semantics.unit.status,'unknown')
                if name=='HRT':self.assertIn('zero at non-storage',info.semantics.effective.reason)
        for pu,unit in zip(POLLUTANT_UNITS,('mg/L','ug/L','count/L')):
            m=load(fixture(pollutant_units=pu,rows='J Q0 C=Q0+R_Q1\n'));owner=ref('treatment',('J','Q0'))
            self.assertEqual(m.inspect_field(owner,'expression').semantics.unit.value,unit)
            self.assertEqual(m.inspect_field(owner,('expression','left','pollutant')).semantics.unit.value,unit)
            self.assertEqual(m.inspect_field(owner,('expression','right','pollutant')).semantics.unit.value,'1')
            self.assertTrue(any(d.code=='treatment.missing_removal' for d in m.validate().diagnostics))
            self.assertEqual(m.inspect_field(owner,'expression').semantics.default.status,'required')
        m=load(fixture(rows='J Q0 R=2\n'))
        self.assertEqual(m.inspect_field(owner,('expression','value')).semantics.effective.value,2)
        with patch('builtins.open',side_effect=AssertionError('No file I/O in field queries')):queries(m)

    def test_exact_equals_offsets_repeated_names_quotes_aliases_and_functions(self):
        owner=ref('treatment',('J','Q0'))
        for row,shared in (('J Q0 C= Q0 + Q0',False),('J Q0 C = Q0 + Q0',False),('J Q0 "C = Q0 + Q0"',True),('J Q0 cResult ignored = "Q0 + Q0"',True)):
            m=load(fixture(rows=row+'\n'))
            left=m.field_provenance(owner,('expression','left','pollutant','key'))
            right=m.field_provenance(owner,('expression','right','pollutant','key'))
            self.assertEqual(left.declarations[0].tokens==right.declarations[0].tokens,shared)
            self.assertEqual(m.field_provenance(owner,'kind').declarations[0].tokens[0].value,InpDocument.from_text(row).lines[0].values[2])
            self.assertEqual(queries(Model.from_json_document(m.to_json_document(),strict=True)),queries(m))
        for formula in (*FORMULAS,'-2^2','- 2^2','2^3^2','FLOWsuffix','Q0 + Q0'):
            m=load(fixture(rows='J Q0 R='+formula+'\n'))
            self.assertTrue(all(info.provenance.status!='unknown' for info in queries(m)[::2]))
        m=load(fixture(rows='J Q0 R=.1\nJ Q0 C=Q0 + Q0\n'))
        self.assertEqual([d.contributes for d in m.field_provenance(owner,'expression').declarations],[False,True])
        self.assertEqual(m.field_provenance(owner,('expression','left','pollutant','key')).declarations[0].tokens[0].value,'C=Q0')

    def test_shadowing_missing_targets_cycles_and_unrelated_groups(self):
        owner=ref('treatment',('J','Q0'));m=load(fixture())
        m.treatment.update(('J','Q0'),expression=expression('R_Q1'))
        self.assertEqual(m.inspect_field(owner,'expression').semantics.effective.status,'invalid')
        m=load(fixture());m.pollutants.rename('Q0','FLOWER')
        self.assertEqual(m.inspect_field(ref('treatment',('J','FLOWER')),'kind').semantics.effective.status,'invalid')
        m=load(fixture());m.treatment.update(('J','Q1'),expression=t.TreatmentConcentration(pollutant=ref('pollutants','Lost')))
        self.assertEqual(m.inspect_field(ref('treatment',('J','Q1')),'kind').semantics.effective.status,'invalid')
        self.assertEqual(m.inspect_field(owner,'kind').semantics.effective.status,'known')
        m.treatment.update(('J','Q0'),expression=expression('Q1'))
        self.assertEqual(m.inspect_field(owner,'kind').semantics.effective.status,'invalid')
        m=load(fixture(rows='J Q0 C=Q1\nJ Q1 C=Q0\n'))
        self.assertEqual(m.inspect_field(owner,'kind').semantics.effective.status,'known')

    def test_extensions_and_custom_parser_require_their_own_contract(self):
        @dataclass(frozen=True,kw_only=True)
        class FutureLeaf(e.ExpressionNumber):pass
        m=load(fixture());m.treatment.update(('J','Q0'),expression=FutureLeaf(value=1))
        self.assertEqual(m.inspect_field(ref('treatment',('J','Q0')),'kind').semantics.effective.status,'unknown')
        @dataclass(frozen=True,kw_only=True)
        class FuturePollutant(q.Pollutant):pass
        m=load(fixture());row=m.pollutants['Q0'];m.pollutants.replace('Q0',FuturePollutant(**{f.name:getattr(row,f.name) for f in fields(row)}))
        self.assertEqual(m.inspect_field(ref('treatment',('J','Q0')),'kind').semantics.effective.status,'unknown')
        from easysewer.io.inp.treatment import TreatmentCodec
        from easysewer.schema import EPA_SWMM_5_2_4
        calls=[]
        class Custom(TreatmentExpressionCodec):
            def parse(self,text,resolve=None,**kw):calls.append(text);return e.ExpressionNumber(value=9)
        codec=TreatmentCodec();codec.expressions=Custom()
        result=codec.decode(InpDocument.from_text('[TREATMENT]\nJ Q0 C=custom syntax\n'),EPA_SWMM_5_2_4)
        self.assertEqual(calls,['custom syntax']);self.assertEqual(result.value.records[0].value.expression.value,9)
        self.assertFalse(any(v.path[0]=='expression' for v in result.value.field_coverage))

    def test_shared_arithmetic_field_rules_remain_scoped_to_their_domain(self):
        from test_controls_v2 import controlled, symbol
        control=controlled('EXPRESSION E = 2\nRULE R\nIF E > 1\nTHEN CONDUIT P STATUS = OPEN\n')
        self.assertEqual(control.inspect_field(symbol('EXPRESSION','E'),('expression','value')).semantics.unit.value,'1')
        from test_groundwater_fields_v2 import fixture as groundwater_fixture,load as groundwater_load,LATERAL
        groundwater=groundwater_load(groundwater_fixture())
        self.assertEqual(groundwater.inspect_field(LATERAL,('expression','left','left','value')).semantics.unit.status,'unknown')
        self.assertEqual(groundwater.inspect_field(LATERAL,'expression').semantics.unit.value,'cfs/acre')
        treatment=load(fixture(rows='J Q0 C=2\n'));owner=ref('treatment',('J','Q0'))
        self.assertEqual(treatment.inspect_field(owner,('expression','value')).semantics.unit.status,'unknown')
        self.assertEqual(treatment.inspect_field(owner,'expression').semantics.unit.value,'mg/L')

    def test_rename_rollback_conversion_and_original_reference_preflight(self):
        m=load(fixture());before=queries(m)
        with self.assertRaises(RuntimeError):
            with m.transaction():m.nodes.rename('J','Tank');raise RuntimeError('rollback')
        self.assertEqual(queries(m),before)
        m.nodes.rename('J','Tank');m.pollutants.rename('Q0','Solids')
        info=m.field_provenance(ref('treatment',('Tank','Solids')),('expression','right','pollutant','key'))
        self.assertEqual(info.declarations[0].tokens[0].value,'C=.8*Q0')
        self.assertEqual(queries(Model.from_json_document(m.to_json_document(),strict=True)),queries(m))
        original=m.to_json_document().to_bytes()
        with self.assertRaises(ValidationError):m.convert_pollutant_units('Solids','#/L')
        self.assertEqual(original,m.to_json_document().to_bytes())
        m=load(fixture(rows='J Q0 C=Q1\nJ Q0 C=Q0\n'));m.pollutants.remove('Q1',cascade=True)
        self.assertIn('treatment.source_reference',{d.code for d in m.validate(for_run=True).errors})
        self.assertTrue(m.validate(for_run=True,normalize=True).is_valid)
        source=fixture(rows='J Q0 C=Q0\nJ Q0 C=Q0 +\n');m=load(source,strict=False)
        self.assertNotIn(('J','Q0'),m.treatment);self.assertEqual(m.document.text,source)


if __name__=='__main__':unittest.main()
