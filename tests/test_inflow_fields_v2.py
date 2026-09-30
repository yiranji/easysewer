"""Pollutant/inflow field facts, units and composite source identities."""
from dataclasses import dataclass, fields, is_dataclass, replace
from datetime import date, timedelta
import unittest

from easysewer.io.inp import InpDocument
from easysewer.model import Model, Ref
from easysewer.model import inflows as i, quality as q
from easysewer.model.resources import InlineTimeSeries, SeriesPoint, Pattern
from test_hydrology_fields_v2 import fixture as hydrology_fixture, UNITS

POLLUTANT_UNITS = ('MG/L', 'UG/L', '#/L')
POLL = Ref(collection='swmm:pollutants', key='Q0')
FLOW = Ref(collection='swmm:inflows', key=('J', 'FLOW'))
CONC = Ref(collection='swmm:inflows', key=('J', 'POLLUTANT:Q0'))
MASS = Ref(collection='swmm:inflows', key=('J', 'POLLUTANT:Q1'))
DWF = Ref(collection='swmm:dwf', key=('J', 'FLOW'))


def ref(collection, key): return Ref(collection='swmm:'+collection, key=key)
def load(text): return Model.from_document(InpDocument.from_text(text, source='inflow-fields.inp'), strict=True)


def fixture(units='CFS', pollutant_units='MG/L', mode='explicit', day=31):
    m = load(hydrology_fixture(units=units))
    when = date(2020, 1, 31) if day == 31 else date(2020, 2, day)
    m.update_options(start_date=when, end_date=when, report_start_date=when)
    other = POLLUTANT_UNITS[(POLLUTANT_UNITS.index(pollutant_units)+1)%3]
    for key, u in (('Q0', pollutant_units), ('Q1', other)):
        m.pollutants.add(q.Pollutant(id=key, units=u, rainfall_concentration=2, groundwater_concentration=3,
            rdii_concentration=4, decay_rate=-.01, **({} if mode=='defaults' else dict(snow_only=False,
                dwf_concentration=1, initial_concentration=.5))))
    if mode != 'defaults': m.pollutants.update('Q0', co_pollutant=ref('pollutants','Q1'), co_fraction=.2)
    for key, kind, values in (('M','MONTHLY',(1.,2.)),('D','DAILY',(1.,2.,3.,4.,5.,6.,7.)),
                              ('H','HOURLY',(.5,)),('W','WEEKEND',(3.,)),('H2','HOURLY',(2.,))):
        m.patterns.add(Pattern(id=key, kind=kind, factors=values))
    for key, value in (('Flow',.1),('Concentration',2.),('Mass',3.)):
        m.timeseries.add(InlineTimeSeries(id=key, points=(SeriesPoint(time=timedelta(),value=value),
            SeriesPoint(time=timedelta(minutes=20),value=value*2))))
    common = {} if mode == 'defaults' else dict(scale_factor=2., baseline=.2, pattern=ref('patterns','M'))
    m.inflows.add(i.FlowInflow(node=ref('nodes','J'), series=ref('timeseries','Flow'), **common))
    m.inflows.add(i.ConcentrationInflow(node=ref('nodes','J'), constituent=ref('pollutants','Q0'), series=ref('timeseries','Concentration'), **common))
    m.inflows.add(i.MassInflow(node=ref('nodes','J'), constituent=ref('pollutants','Q1'), series=ref('timeseries','Mass'),
        **common, **({} if mode=='defaults' else dict(mass_factor=126.))))
    patterns = (() if mode=='defaults' else tuple(ref('patterns',p) for p in ('M','D','H','W')) if mode=='explicit'
                else (ref('patterns','H'),ref('patterns','M'),None,ref('patterns','H2')))
    m.dwf.add(i.DryWeatherFlow(node=ref('nodes','J'), baseline=.1, patterns=patterns))
    m.dwf.add(i.DryWeatherConcentration(node=ref('nodes','J'), constituent=ref('pollutants','Q0'), baseline=4., patterns=patterns))
    return m.to_document().text


def queries(model):
    result = []
    def walk(owner,value,path=()):
        if is_dataclass(value):
            for f in fields(value):
                p=path+(f.name,); result.extend((model.inspect_field(owner,p),model.field_provenance(owner,p)));walk(owner,getattr(value,f.name),p)
        elif isinstance(value,tuple):
            for index,value in enumerate(value):
                p=path+(index,);result.extend((model.inspect_field(owner,p),model.field_provenance(owner,p)));walk(owner,value,p)
    for namespace in ('pollutants','inflows','dwf'):
        for key,row in model.collection('swmm:'+namespace).items():walk(ref(namespace,key),row)
    return tuple(result)


class InflowFieldTests(unittest.TestCase):
    def test_all_units_variants_defaults_and_dimensions(self):
        for units in UNITS:
            for pu in POLLUTANT_UNITS:
                m=load(fixture(units,pu,'defaults'))
                for info in queries(m)[::2]:
                    self.assertIn(info.semantics.effective.status,('known','not_applicable'),info.path)
                    self.assertNotEqual(info.semantics.unit.status,'unknown',info.path)
                self.assertEqual(m.inspect_field(POLL,'snow_only').semantics.effective.value,False)
                self.assertEqual(m.inspect_field(POLL,'dwf_concentration').semantics.effective.value,0)
                self.assertEqual(m.inspect_field(POLL,'co_fraction').semantics.effective.status,'not_applicable')
                self.assertEqual(m.inspect_field(CONC,'baseline').semantics.unit.value,{'MG/L':'mg/L','UG/L':'ug/L','#/L':'count/L'}[pu])
                self.assertEqual(m.inspect_field(FLOW,'baseline').semantics.default.value,0)
                self.assertEqual(m.inspect_field(FLOW,'scale_factor').semantics.effective.value,1)
                self.assertEqual(m.inspect_field(MASS,'mass_factor').semantics.effective.value,1)
                self.assertIn('per user mass-rate unit',m.inspect_field(MASS,'mass_factor').semantics.unit.value)
                self.assertEqual(m.inspect_field(POLL,'decay_rate').semantics.unit.value,'1/day')
                self.assertEqual(queries(Model.from_json_document(m.to_json_document(),strict=True)),queries(m))

    def test_dwf_slots_duplicate_kinds_and_weekend_not_multiplied(self):
        m=load(fixture(mode='duplicate'))
        active=m.inspect_field(DWF,'patterns').semantics.effective.value
        self.assertEqual(active,(None,ref('patterns','M'),None,ref('patterns','H2')))
        for path in (('patterns',0),('patterns',0,'key'),('patterns',2)):
            self.assertEqual(m.inspect_field(DWF,path).semantics.effective.status,'not_applicable')
        self.assertEqual(m.inspect_field(DWF,('patterns',3,'key')).semantics.effective.value,'H2')
        self.assertEqual(m.field_provenance(DWF,('patterns',2)).declarations[0].role,'marker')
        self.assertEqual(m.inspect_field(DWF,('patterns',3)).provenance.status,'untracked_path')
        m=load(fixture());info=m.inspect_field(DWF,'patterns').semantics.effective
        self.assertEqual(len(info.value),4);self.assertIn('replaces',info.reason)

    def test_optional_columns_history_ignored_slots_and_variant_switch(self):
        source=fixture()+'[INFLOWS]\nJ FLOW "" ignored ignored\nJ Q1 Mass CONCEN ignored\nJ Q1 Mass MASS\n[DWF]\nJ FLOW .3 "" H\n'
        m=load(source)
        self.assertEqual(m.to_document().text,source)
        self.assertEqual(m.inspect_field(FLOW,'scale_factor').semantics.effective.status,'not_applicable')
        for name in ('scale_factor','baseline','pattern'):
            old=m.field_provenance(FLOW,name)
            self.assertIsNone(old.value.value)
            self.assertTrue(all(not d.contributes for d in old.declarations))
        self.assertEqual(m.field_provenance(FLOW,'series').declarations[-1].role,'marker')
        self.assertTrue(any(d.role=='retained' and not d.contributes for d in m.field_provenance(FLOW,'constituent').declarations))
        self.assertIsNone(m.field_provenance(MASS,'mass_factor').value.value)
        self.assertTrue(all(not d.contributes for d in m.field_provenance(MASS,'mass_factor').declarations))
        self.assertEqual([d.contributes for d in m.field_provenance(DWF,'baseline').declarations],[False,True])
        self.assertEqual(queries(Model.from_json_document(m.to_json_document(),strict=True)),queries(m))

    def test_pollutant_ignored_unpaired_tokens_and_co_units(self):
        for tail in ('NO Missing','NO *','NO * not_a_number 1 2'):
            m=load('[POLLUTANTS]\nQ0 MG/L 0 0 0 -.1 '+tail+'\n')
            self.assertIsNone(m.pollutants['Q0'].co_pollutant)
            self.assertEqual(m.inspect_field(POLL,'co_fraction').semantics.effective.status,'not_applicable')
            decl=m.field_provenance(POLL,'co_pollutant').declarations[0]
            self.assertEqual(decl.contributes,tail.startswith('NO * not'))
        m=load(fixture())
        self.assertEqual(m.inspect_field(POLL,'co_fraction').semantics.unit.value,'MG/L per UG/L')
        m.pollutants.update('Q0',co_fraction=None)
        self.assertEqual(m.inspect_field(POLL,'co_fraction').semantics.effective.value,0)

    def test_references_context_extensions_and_shared_dimensions(self):
        m=load(fixture());m.inflows.remove(FLOW.key)
        self.assertEqual(m.inspect_field(CONC,'baseline').semantics.effective.status,'invalid')
        m=load(fixture());m.inflows.update(CONC.key,series=ref('timeseries','Flow'))
        self.assertEqual(m.inspect_field(CONC,'series').semantics.effective.status,'ambiguous')
        m=load(fixture());m.inflows.update(FLOW.key,pattern=ref('patterns','missing'))
        self.assertEqual(m.inspect_field(FLOW,'baseline').semantics.effective.status,'invalid')
        @dataclass(frozen=True,kw_only=True)
        class ExtendedPattern(Pattern): pass
        m=load(fixture());m.patterns.replace('M',ExtendedPattern(id='M',kind='MONTHLY',factors=(1.,)))
        self.assertEqual(m.inspect_field(DWF,'patterns').semantics.effective.status,'unknown')
        @dataclass(frozen=True,kw_only=True)
        class ExtendedPollutant(q.Pollutant): pass
        m=load(fixture());row=m.pollutants['Q1']
        m.pollutants.replace('Q1',ExtendedPollutant(**{f.name:getattr(row,f.name) for f in fields(row)}))
        self.assertEqual(m.inspect_field(MASS,'mass_factor').semantics.effective.status,'unknown')
        self.assertEqual(m.inspect_field(POLL,'co_fraction').semantics.effective.status,'unknown')

    def test_identity_units_negative_values_and_transaction_rollback(self):
        m=load(fixture());before=queries(m)
        with self.assertRaises(RuntimeError):
            with m.transaction():
                m.nodes.rename('J','Upstream');m.pollutants.rename('Q0','Quality');raise RuntimeError
        self.assertEqual(queries(m),before)
        m.nodes.rename('J','Upstream');m.pollutants.rename('Q0','Quality')
        owner=ref('inflows',('Upstream','POLLUTANT:Quality'))
        self.assertTrue(m.inspect_field(owner,('node','key')).changed)
        self.assertEqual(m.field_provenance(owner,('constituent','key')).value.value,'Q0')
        m.convert_units('CMS')
        self.assertEqual(m.inspect_field(owner,'baseline').semantics.unit.value,'mg/L')
        self.assertEqual(queries(Model.from_json_document(m.to_json_document(),strict=True)),queries(m))
        m=load(fixture());m.inflows.update(FLOW.key,scale_factor=-2,baseline=-.1)
        self.assertEqual(m.inspect_field(FLOW,'scale_factor').semantics.effective.value,-2)
        self.assertEqual(m.inspect_field(FLOW,'baseline').semantics.effective.value,-.1)
        m.inflows.update(MASS.key,mass_factor=0)
        self.assertEqual(m.inspect_field(MASS,'mass_factor').semantics.effective.status,'invalid')

    def test_explicit_zero_and_pollutant_unit_conversion_keep_original_values(self):
        m=load(fixture());m.inflows.update(FLOW.key,scale_factor=0,baseline=0)
        self.assertEqual(m.inspect_field(FLOW,'scale_factor').semantics.effective.value,0)
        self.assertEqual(m.inspect_field(FLOW,'baseline').semantics.effective.value,0)
        m.convert_pollutant_units('Q0','UG/L')
        self.assertEqual(m.inspect_field(CONC,'baseline').semantics.unit.value,'ug/L')
        self.assertAlmostEqual(m.inspect_field(CONC,'baseline').semantics.effective.value,200)
        self.assertAlmostEqual(m.field_provenance(CONC,'baseline').value.value,.2)
        self.assertTrue(m.inspect_field(CONC,'baseline').changed)
        self.assertAlmostEqual(m.inspect_field(POLL,'co_fraction').semantics.effective.value,200)
        self.assertEqual(m.inspect_field(POLL,'co_fraction').semantics.unit.value,'1')
        self.assertEqual(queries(Model.from_json_document(m.to_json_document(),strict=True)),queries(m))

    def test_malformed_groups_unclaimed(self):
        m=Model.from_document(InpDocument.from_text(fixture()+'[INFLOWS]\nJ FLOW Q FLOW 1 bad\n'))
        self.assertNotIn(FLOW.key,m.inflows)
        m=Model.from_document(InpDocument.from_text('[POLLUTANTS]\nP MG/L 0 0 0 0\nP MG/L 0 0 0 0\n'))
        self.assertFalse(m.pollutants)


if __name__=='__main__':unittest.main()
