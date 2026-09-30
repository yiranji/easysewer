"""LID defaults, native overrides and original stateful declaration sources."""
from dataclasses import dataclass, fields, is_dataclass, replace
from datetime import time, timedelta
import unittest
from unittest.mock import patch

from easysewer.model import Model, Ref, FileReference
from easysewer.model import lid as l
from easysewer.model.resources import SeriesPoint, Curve, CurvePoint
from easysewer.io.inp import InpDocument
from easysewer.validation import ValidationError
from test_lid_v2 import lid_model, usage
from test_hydrology_fields_v2 import UNITS


def ref(namespace,key): return Ref(collection='swmm:'+namespace,key=key)
CONTROL=ref('lid_controls','L')
USAGE=ref('lid_usage','lid-usage-1')


def load(source):
    return Model.from_document(InpDocument.from_text(source,source='lid-fields.inp'),strict=True)


def fixture(kind='BC',units='CFS',extra=False,mode='normal',detail=None):
    m=lid_model(kind,extra=extra);m.reinterpret_units(units)
    m.update_options(end_date=m.options.start_date,end_time=time(0,20),report_step=timedelta(minutes=1),
        routing_step=timedelta(seconds=5),wet_step=timedelta(seconds=30))
    m.raingages.update('R',interval=timedelta(minutes=1))
    m.timeseries.update('Rain',points=tuple(SeriesPoint(time=timedelta(minutes=i),value=2 if i<15 else 0) for i in range(21)))
    if not extra: m.lid_usage.update('lid-usage-1',from_pervious=None)
    if extra and m.lid_controls['L'].drain:
        m.curves.add(Curve(id='Head',kind='CONTROL',points=(CurvePoint(x=0,y=.5),CurvePoint(x=36,y=1))))
        m.lid_controls.update('L',drain=replace(m.lid_controls['L'].drain,curve=ref('curves','Head')))
    if mode=='impervious':
        m.subcatchments.update('S',impervious_percent=99.9);m.lid_usage.update('lid-usage-1',to_pervious=True)
    if mode=='multiple': m.lid_usage.add(usage('lid-usage-2',number=3,area=50,drain_to=ref('nodes','O')))
    if mode=='disabled': m.lid_usage.add(l.DisabledLidUsage(record_id='lid-usage-2',subcatchment=ref('subcatchments','S'),control=CONTROL,
        parameters=('bad','bad','bad','bad','bad','ignored detail.txt','Missing')))
    if mode=='zero-surface' and m.lid_controls['L'].surface:
        m.lid_controls.update('L',surface=replace(m.lid_controls['L'].surface,storage_depth=0))
    if mode=='no-storage': m.lid_controls.update('L',storage=None)
    if detail is not None: m.lid_usage.update('lid-usage-1',report_file=FileReference(path=str(detail),direction='output'))
    return m.to_document().text


def queries(m):
    result=[]
    def walk(owner,value,path=()):
        if is_dataclass(value):
            for f in fields(value):
                p=path+(f.name,);result.extend((m.inspect_field(owner,p),m.field_provenance(owner,p)))
                walk(owner,getattr(value,f.name),p)
        elif isinstance(value,tuple):
            for index,child in enumerate(value):
                p=path+(index,);result.extend((m.inspect_field(owner,p),m.field_provenance(owner,p)));walk(owner,child,p)
    for namespace in ('lid_controls','lid_usage'):
        for key,row in m.collection('swmm:'+namespace).items():walk(ref(namespace,key),row)
    return tuple(result)


def materialize(m):
    changes={}
    for name in ('surface','pavement','soil','storage','drain','drain_mat','removals'):
        fact=m.inspect_field(CONTROL,name).semantics.effective
        if fact.status=='known': changes[name]=fact.value
    m.lid_controls.update('L',**changes)
    for key,row in tuple(m.lid_usage.items()):
        if type(row) is l.DisabledLidUsage:continue
        changes={}
        for name in ('from_pervious','drain_to','to_pervious'):
            fact=m.inspect_field(ref('lid_usage',key),name).semantics.effective
            if fact.status=='known': changes[name]=fact.value
        m.lid_usage.update(key,**changes)


class LidFieldTests(unittest.TestCase):
    def test_eight_types_six_units_optional_tails_and_json(self):
        for kind in l.LID_KINDS:
            for units in UNITS:
                for extra in (False,True):
                    with self.subTest(kind=kind,units=units,extra=extra):
                        m=load(fixture(kind,units,extra));before=queries(m)
                        for info in before[::2]:
                            self.assertIn(info.semantics.effective.status,('known','not_applicable'),info.path)
                            self.assertNotEqual(info.semantics.unit.status,'unknown',info.path)
                        self.assertEqual(queries(Model.from_json_document(m.to_json_document(),strict=True)),before)
                        self.assertEqual(m.inspect_field(USAGE,'drain_to').semantics.effective.value,ref('nodes','J'))
                        self.assertEqual(m.inspect_field(USAGE,'from_pervious').semantics.effective.value,10 if extra else 0)
                        if kind in ('BC','RG','GR'):
                            self.assertEqual(m.inspect_field(CONTROL,'soil').semantics.default.status,'required')
                        if kind!='GR':
                            self.assertEqual(m.inspect_field(CONTROL,'drain_mat').semantics.default.status,'not_applicable')
                        if m.lid_controls['L'].drain:
                            unit=m.inspect_field(CONTROL,('drain','coefficient')).semantics.unit.value
                            self.assertEqual(unit,('in/h' if units in UNITS[:3] else 'mm/h') if kind=='RD' else
                                '(in/h)/(in)^0.5' if units in UNITS[:3] else '(mm/h)/(mm)^0.5')

    def test_defaults_overrides_and_input_ratios(self):
        m=load(fixture('RB'))
        self.assertFalse(m.inspect_field(CONTROL,('storage','covered')).semantics.effective.value)
        self.assertEqual(m.inspect_field(CONTROL,('storage','void_ratio')).semantics.effective.value,.5)
        self.assertIn('ratio/(1+ratio)',m.inspect_field(CONTROL,('storage','void_ratio')).semantics.effective.reason)
        m=load(fixture('PP',mode='no-storage'))
        self.assertIsNone(m.inspect_field(CONTROL,'storage').semantics.effective.value)
        self.assertEqual(m.inspect_field(CONTROL,('drain','offset')).semantics.effective.value,0)
        self.assertEqual(m.inspect_field(CONTROL,('pavement','regeneration_fraction')).semantics.effective.value,0)
        m=load(fixture('BC',mode='zero-surface'))
        self.assertEqual(m.inspect_field(CONTROL,('surface','vegetation_fraction')).semantics.effective.value,0)
        m=load(fixture('RD'))
        for name in ('exponent','offset','delay','open_head','close_head','curve'):
            self.assertEqual(m.inspect_field(CONTROL,('drain',name)).semantics.effective.status,'not_applicable')
        m=load(fixture('BC'))
        for percent,expected in ((99.89,True),(99.9,False),(100,False)):
            m.subcatchments.update('S',impervious_percent=percent);m.lid_usage.update('lid-usage-1',to_pervious=True)
            self.assertEqual(m.inspect_field(USAGE,'to_pervious').semantics.effective.value,expected)

    def test_stateful_layer_history_removal_pairs_and_ignored_drainmat(self):
        m=load(fixture('GR'));source=m.document.text
        source=source.replace('[LID_CONTROLS]\n','[LID_CONTROLS]\nL DRAINMAT bad bad bad\n',1)
        source+='[LID_CONTROLS]\nL SURFACE 3 .1 .1 3 2\nL REMOVALS Q0 10 Q1 20 Q0 35\nL REMOVALS Q1 40\n'
        m=load(source)
        self.assertEqual([d.contributes for d in m.field_provenance(CONTROL,('surface','slope')).declarations],[False,True])
        mat=m.field_provenance(CONTROL,'drain_mat').declarations
        self.assertEqual([d.role for d in mat],['retained','derived']);self.assertEqual([d.contributes for d in mat],[False,True])
        for index,expected in ((0,35),(1,40)):
            self.assertEqual(m.inspect_field(CONTROL,('removals',index)).semantics.effective.value,m.lid_controls['L'].removals[index])
            d=m.field_provenance(CONTROL,('removals',index,'percent')).declarations
            self.assertEqual(sum(v.contributes for v in d),1);self.assertEqual(float([v for v in d if v.contributes][0].tokens[0].value),expected)
        self.assertEqual(queries(Model.from_json_document(m.to_json_document(),strict=True)),queries(m))
        malformed=Model.from_document(InpDocument.from_text(source+'[LID_CONTROLS]\nL SOIL bad\n'))
        self.assertNotIn('L',malformed.lid_controls);self.assertFalse(malformed.validate().is_valid)

    def test_tail_replacement_keeps_omission_and_zero_distinct(self):
        for name,first,last,path in (
                ('PP','L PAVEMENT 4 .25 .1 5 10 2 .5','L PAVEMENT 4 .25 .1 5 10',('pavement','regeneration_days')),
                ('BC','L DRAIN .2 .5 1 2 2 .5','L DRAIN .2 .5 1 2',('drain','open_head')),
                ('RB','L STORAGE 12 .5 .1 10 YES','L STORAGE 12 .5 .1 10',('storage','covered'))):
            m=load(fixture(name)+'[LID_CONTROLS]\n'+first+'\n'+last+'\n')
            self.assertIsNone(m.inspect_field(CONTROL,path).value)
            self.assertTrue(all(not d.contributes for d in m.field_provenance(CONTROL,path).declarations))
            self.assertEqual(m.inspect_field(CONTROL,path).semantics.effective.value,False if name=='RB' else 0)
            materialize(m);self.assertTrue(m.validate().is_valid)

    def test_usage_identity_raw_prefixes_markers_and_disabled_tokens(self):
        source=fixture('RB')+'[LID_USAGE]\nS L 2.7 10 0 0 0 3 * * 0\nS L nope bad bad bad bad bad "ignored.txt" Missing\n'
        m=load(source);second=ref('lid_usage','lid-usage-2');disabled=ref('lid_usage','lid-usage-3')
        self.assertEqual(m.inspect_field(second,'number').semantics.effective.value,2)
        self.assertEqual(m.field_provenance(second,'number').declarations[0].tokens[0].raw,'2.7')
        self.assertEqual(m.field_provenance(second,'drain_to').declarations[0].role,'marker')
        self.assertTrue(m.inspect_field(second,'to_pervious').semantics.effective.value)
        self.assertEqual(m.field_provenance(disabled,('parameters',5)).declarations[0].tokens[0].raw,'"ignored.txt"')
        self.assertEqual(m.inspect_field(disabled,('parameters',5)).semantics.effective.status,'not_applicable')
        self.assertFalse(m.file_uses())
        original=m.field_provenance(second,'number')
        m.lid_usage.move('lid-usage-2',before='lid-usage-1')
        self.assertEqual(m.field_provenance(second,'number'),original)
        self.assertEqual(queries(Model.from_json_document(m.to_json_document(),strict=True)),queries(m))

    def test_aggregate_constraints_shadowing_extensions_and_no_file_io(self):
        m=load(fixture('BC',mode='multiple',detail='报告 detail.txt'))
        with patch('builtins.open',side_effect=AssertionError('Field queries cannot open reports')): queries(m)
        info=m.field_provenance(USAGE,('report_file','direction'))
        self.assertEqual(info.declarations[0].role,'derived');self.assertEqual(info.value.value,'output')
        m.lid_usage.update('lid-usage-2',from_impervious=90)
        self.assertEqual(m.inspect_field(USAGE,'area').semantics.effective.status,'invalid')
        m=load(fixture());m.lid_usage.update('lid-usage-1',area=1e9)
        self.assertEqual(m.inspect_field(USAGE,'area').semantics.effective.status,'invalid')
        m=load(fixture());m.subcatchments.add(replace(m.subcatchments['S'],id='J'))
        self.assertEqual(m.inspect_field(USAGE,'drain_to').semantics.effective.status,'ambiguous')
        m=load(fixture('VS'));m.lid_usage.update('lid-usage-1',width=0)
        self.assertEqual(m.inspect_field(USAGE,'width').semantics.effective.status,'invalid')
        @dataclass(frozen=True,kw_only=True)
        class FutureSoil(l.LidSoil): pass
        m=load(fixture());row=m.lid_controls['L'].soil
        m.lid_controls.update('L',soil=FutureSoil(**{f.name:getattr(row,f.name) for f in fields(row)}))
        self.assertEqual(m.inspect_field(CONTROL,'soil').semantics.effective.status,'unknown')

    def test_references_curve_dimensions_and_invalid_layer_inputs(self):
        m=load(fixture('BC',extra=True));m.curves.update('Head',kind='STORAGE')
        self.assertEqual(m.inspect_field(CONTROL,('drain','curve')).semantics.effective.status,'invalid')
        m=load(fixture());m.lid_usage.update('lid-usage-1',control=ref('lid_controls','Missing'))
        self.assertEqual(m.inspect_field(USAGE,'area').semantics.effective.status,'invalid')
        m=load(fixture('RB'));m.lid_controls.update('L',storage=replace(m.lid_controls['L'].storage,void_ratio=0))
        self.assertEqual(m.inspect_field(CONTROL,('storage','void_ratio')).semantics.effective.status,'invalid')

    def test_rename_rollback_conversion_and_quoted_paths(self):
        m=load(fixture('PP',extra=True,detail='报告 detail.txt'))
        m.lid_controls.rename('L','Pavement');m.subcatchments.rename('S','Catchment');m.pollutants.rename('Q0','TSS')
        self.assertEqual(m.inspect_field(USAGE,('control','key')).value,'Pavement')
        self.assertEqual(m.field_provenance(USAGE,('control','key')).value.value,'L')
        before=m.to_json_document().to_bytes()
        with self.assertRaises(ValidationError):m.lid_controls.remove('Pavement')
        self.assertEqual(m.to_json_document().to_bytes(),before)
        m.convert_units('CMS');owner=ref('lid_controls','Pavement')
        self.assertAlmostEqual(m.inspect_field(owner,('pavement','thickness')).semantics.effective.value,4*25.4)
        self.assertEqual(queries(Model.from_json_document(m.to_json_document(),strict=True)),queries(m))


if __name__=='__main__':unittest.main()
