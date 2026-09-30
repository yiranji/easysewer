"""Static control semantics, precise AST/clause sources and ordered identities."""
from dataclasses import dataclass, fields, is_dataclass, replace
from datetime import timedelta
import unittest

from easysewer.model import Model, Ref
from easysewer.model import controls as c
from easysewer.io.inp import InpDocument
from easysewer.validation import ValidationError
from test_controls_v2 import controlled, PROGRAM, symbol
from test_options_v2 import network
from test_regulators_v2 import regulator_model
from test_hydrology_v2 import hydrology_model

UNITS=('CFS','GPM','MGD','CMS','LPS','MLD')
OBJECTS=('NODE','LINK','CONDUIT','PUMP','ORIFICE','WEIR','OUTLET','SIMULATION')
ATTRIBUTES=tuple((obj,attr) for obj in OBJECTS for attr in c.ATTRIBUTES[obj])
SETTINGS=tuple((obj,setting) for obj in ('PUMP','ORIFICE','WEIR','OUTLET') for setting in ('.5','CURVE Control','TIMESERIES Settings','PID .1 1 .05'))
KINDS={'PUMP':'PUMP2','ORIFICE':'SIDE','WEIR':'TRANSVERSE','OUTLET':'FUNCTIONAL/HEAD'}
CALENDAR=(('TIME','.0004'),('CLOCKTIME','25:01:02'),('DATE','01/02/2020'),('DAYOFYEAR','03/01'),('DAYOFYEAR','60.5'),('DAY','1.5'),('MONTH','12'))


def load(text,strict=True):return Model.from_document(InpDocument.from_text(text,source='control-fields.inp'),strict=strict)


def fixture(mode='program',value=None,units='CFS'):
    if mode=='attribute':
        obj,attr=value
        m=network() if obj in ('NODE','LINK','CONDUIT','SIMULATION') else regulator_model(KINDS[obj])
        action='CONDUIT P STATUS = CLOSED' if obj in ('NODE','LINK','CONDUIT','SIMULATION') else f'{obj} P SETTING = .5'
        token='ON' if attr=='STATUS' else '00:00:07' if attr in c.TIME_ATTRIBUTES else '01/01/2000' if attr=='DATE' else '01/02' if attr=='DAYOFYEAR' else '1'
        lhs=f'{obj} {attr}' if obj=='SIMULATION' else f'{obj} {"J" if obj=="NODE" else "P"} {attr}'
        program=f'RULE R\nIF {lhs} > {token}\nTHEN {action}\n'
    elif mode=='setting':
        obj,setting=value;m=regulator_model(KINDS[obj])
        extra='[CURVES]\nControl CONTROL 0 .2 10 .8\n' if setting.startswith('CURVE') else '[TIMESERIES]\nSettings 0 .2 .08333333333333333 .8\n' if setting.startswith('TIMESERIES') else ''
        m=load(m.to_document().text+extra)
        program=f'RULE R\nIF NODE J DEPTH >= 1\nTHEN {obj} P SETTING = {setting}\nELSE {obj} P SETTING = .1\n'
    elif mode=='gage':
        m=hydrology_model();program=f'VARIABLE RainDepth = GAGE R {value}-HR_DEPTH\nEXPRESSION RainValue = RainDepth\nRULE R\nIF RainValue >= .1\nTHEN OUTLET P SETTING = .5\nELSE OUTLET P SETTING = 1\n'
    elif mode=='calendar':
        m=network();attr,token=value;program=f'RULE R\nIF SIMULATION {attr} >= {token}\nTHEN CONDUIT P STATUS = CLOSED\n'
    else:m=network();program=PROGRAM
    m.reinterpret_units(units);m.update_options(report_step=timedelta(minutes=5 if mode=='gage' else 1))
    return m.to_document().text+'[CONTROLS]\n'+program


def queries(m):
    result=[]
    def walk(owner,value,path=()):
        if is_dataclass(value):
            for f in fields(value):
                p=path+(f.name,);result.extend((m.inspect_field(owner,p),m.field_provenance(owner,p)));walk(owner,getattr(value,f.name),p)
        elif isinstance(value,tuple):
            for i,child in enumerate(value):
                p=path+(i,);result.extend((m.inspect_field(owner,p),m.field_provenance(owner,p)));walk(owner,child,p)
    for key,row in m.controls.items():walk(Ref(collection='swmm:controls',key=key),row)
    return tuple(result)


def materialize(m):
    for key,row in tuple(m.controls.items()):
        if type(row) is c.ControlRule:
            fact=m.inspect_field(Ref(collection='swmm:controls',key=key),'priority').semantics.effective
            if fact.status=='known':m.controls.update(key,priority=fact.value)


class ControlFieldTests(unittest.TestCase):
    def test_all_attribute_setting_and_rain_window_queries_six_units_json(self):
        cases=[('program',None),*[('attribute',v) for v in ATTRIBUTES],*[('setting',v) for v in SETTINGS],
               *[('gage',v) for v in range(1,49)],*[('calendar',v) for v in CALENDAR]]
        for units in UNITS:
            for mode,value in cases:
                with self.subTest(units=units,mode=mode,value=value):
                    m=load(fixture(mode,value,units));before=queries(m)
                    for info in before[::2]:
                        expected = ('invalid',) if mode == 'attribute' and value == ('OUTLET','SETTING') else ('known','not_applicable')
                        self.assertIn(info.semantics.effective.status,expected,info.path)
                        self.assertNotEqual(info.provenance.status,'unknown',info.path)
                    self.assertEqual(queries(Model.from_json_document(m.to_json_document(),strict=True)),before)

    def test_omission_duration_units_calendar_and_modulated_axes(self):
        m=load(fixture('calendar',('TIME','00:00:01.9')));owner=symbol('RULE','R')
        info=m.inspect_field(owner,('conditions',0,'right','value'))
        self.assertEqual(info.semantics.effective.value,timedelta(seconds=1));self.assertEqual(info.semantics.unit.value,'s')
        self.assertEqual(m.field_provenance(owner,info.path).declarations[0].tokens[0].value,'00:00:01.9')
        self.assertEqual(m.inspect_field(owner,'priority').semantics.default.value,0)
        self.assertEqual(m.inspect_field(owner,'else_actions').semantics.default.value,())
        m=load(fixture('setting',('ORIFICE','PID .1 1 .05')))
        info=m.inspect_field(owner,('then_actions',0,'setting','integral_time'))
        self.assertEqual(info.semantics.unit.value,'s');self.assertEqual(info.semantics.effective.value,timedelta(minutes=1))
        self.assertIn('minutes',info.semantics.unit.reason)
        m=load(fixture('setting',('ORIFICE','CURVE Control'),'CMS'))
        self.assertEqual(m.inspect_field(owner,('then_actions',0,'setting','curve')).semantics.unit.value,('m','1'))
        m=load(fixture('setting',('PUMP','TIMESERIES Settings')))
        self.assertEqual(m.inspect_field(owner,('then_actions',0,'setting','series')).semantics.unit.value,('1',))

    def test_expression_spans_preserve_repeated_symbols_signs_powers_and_quotes(self):
        formulas=('(RateValue + RateValue) / 2','"(RateValue + RateValue) / 2"','-2^2','- 2^2','2^3^2','ABS(RateValue)','1e-3 + .2')
        for formula in formulas:
            with self.subTest(formula=formula):
                m=controlled(f'VARIABLE RateValue = LINK P FLOW\nEXPRESSION E = {formula}\nRULE R\nIF E > 0\nTHEN CONDUIT P STATUS = OPEN\n')
                owner=symbol('EXPRESSION','E');before=queries(m)
                self.assertEqual(queries(Model.from_json_document(m.to_json_document(),strict=True)),before)
                if 'RateValue + RateValue' in formula:
                    a=m.field_provenance(owner,('expression','left','left','reference','key',1))
                    b=m.field_provenance(owner,('expression','left','right','reference','key',1))
                    self.assertEqual(a.declarations[0].role,'derived')
                    self.assertEqual(a.declarations[0].tokens==b.declarations[0].tokens,formula.startswith('"'))
                    self.assertEqual(m.inspect_field(owner,'expression').semantics.unit.value,'CFS')
                if formula=='-2^2':self.assertEqual(m.field_provenance(owner,('expression','left','value')).declarations[0].tokens[0].value,'-2^2')

    def test_unknown_expression_and_live_controller_units_stay_explicit(self):
        m=controlled('VARIABLE D = NODE J DEPTH\nEXPRESSION E = D + 2\nRULE R\nIF E > 1\nTHEN CONDUIT P STATUS = OPEN\n')
        info=m.inspect_field(symbol('EXPRESSION','E'),'expression')
        self.assertEqual(info.semantics.effective.status,'known');self.assertEqual(info.semantics.unit.status,'unknown')
        before=m.to_document().text
        with self.assertRaises(ValidationError):m.convert_units('CMS')
        self.assertEqual(m.to_document().text,before)
        for premise in ('SIMULATION TIME = 0','ORIFICE P TIMEOPEN > 0','NODE J DEPTH > 0\nAND LINK P FLOW > 0'):
            m=load(fixture('setting',('ORIFICE','CURVE Control')).replace('NODE J DEPTH >= 1',premise))
            info=m.inspect_field(symbol('RULE','R'),('then_actions',0,'setting','curve'))
            self.assertEqual(info.semantics.effective.status,'known');self.assertEqual(info.semantics.unit.status,'unknown')

    def test_gage_window_collisions_and_unused_rain_are_not_live_observations(self):
        for hours in (8,9,13,14,15,16,17,18,19,20):
            kind=c.NATIVE_ATTRIBUTES[hours]
            token='ON' if kind=='STATUS' else '0' if kind in c.TIME_ATTRIBUTES else '01/01/2020' if kind=='DATE' else '1'
            m=controlled(f'RULE R\nIF GAGE R {hours}-HR_DEPTH > {token}\nTHEN OUTLET P SETTING = 1\n',model=hydrology_model())
            info=m.inspect_field(symbol('RULE','R'),('conditions',0,'right','value'))
            self.assertEqual(info.semantics.effective.status,'known')
            if type(info.value) in (int,float):self.assertEqual(info.semantics.unit.status,'unknown')
            self.assertTrue(any(d.code=='control.native_rain_attribute' for d in m.validate(for_run=True).diagnostics))
        m=load(fixture('gage',48,'CMS'))
        self.assertEqual(m.inspect_field(symbol('VARIABLE','RainDepth'),('value','attribute')).semantics.unit.value,'mm')
        self.assertEqual(m.inspect_field(symbol('EXPRESSION','RainValue'),'expression').semantics.unit.value,'mm')
        m=controlled('VARIABLE RainRate = GAGE R INTENSITY\nRULE R\nIF RainRate > 0\nTHEN OUTLET P SETTING = 1\n',model=hydrology_model())
        m.subcatchments.remove('S')
        self.assertTrue(any(d.code=='control.unused_gage' for d in m.validate(for_run=True).diagnostics))
        self.assertEqual(m.inspect_field(symbol('VARIABLE','RainRate'),('value','attribute')).semantics.unit.value,'in/h')

    def test_diagnostics_use_composite_statement_identity_and_follow_dependencies(self):
        m=controlled('VARIABLE Same = NODE J DEPTH\nRULE Same\nIF NODE J DEPTH > 0\nTHEN CONDUIT P STATUS = OPEN\n',model=regulator_model('SIDE'))
        self.assertEqual(m.inspect_field(symbol('VARIABLE','Same'),'value').semantics.effective.status,'known')
        self.assertEqual(m.inspect_field(symbol('RULE','Same'),'priority').semantics.effective.status,'invalid')
        m=controlled('VARIABLE Same = OUTLET P SETTING\nRULE Other\nIF Same > 0\nTHEN OUTLET P SETTING = 1\n',model=regulator_model('FUNCTIONAL/HEAD'))
        self.assertEqual(m.inspect_field(symbol('RULE','Other'),'priority').semantics.effective.status,'invalid')

    def test_custom_expression_parser_is_used_without_guessed_source_spans(self):
        from easysewer.io.inp.controls import ControlsCodec
        from easysewer.io.inp.expressions import ExpressionCodec
        from easysewer.schema import EPA_SWMM_5_2_4
        calls=[]
        class CustomParser(ExpressionCodec):
            def parse(self,text,variable=None):
                calls.append(text)
                return c.ExpressionNumber(value=9)
        decoded=ControlsCodec(expressions=CustomParser()).decode(InpDocument.from_text('[CONTROLS]\nEXPRESSION E = custom syntax\n'),EPA_SWMM_5_2_4)
        self.assertEqual(calls,['custom syntax'])
        self.assertEqual(decoded.value.records[0].value.expression,c.ExpressionNumber(value=9))
        self.assertFalse(any(item.path[0]=='expression' for item in decoded.value.field_coverage))
        self.assertFalse(any(item.path[0]=='expression' for item in decoded.value.field_bindings))

    def test_ordered_clause_sources_hoisting_rename_and_rollback(self):
        source='RULE R\nIF NODE J DEPTH >= 0\nVARIABLE V = NODE J DEPTH\nAND V > 0\nTHEN CONDUIT P STATUS = CLOSED\nAND CONDUIT P STATUS = OPEN\nELSE CONDUIT P STATUS = CLOSED\nPRIORITY 0\n'
        m=controlled(source);owner=symbol('RULE','R')
        first=m.field_provenance(owner,('then_actions',0,'setting','value'));second=m.field_provenance(owner,('then_actions',1,'setting','value'))
        self.assertNotEqual(first.declarations[0].line,second.declarations[0].line)
        self.assertEqual(m.field_provenance(owner,('conditions',1,'conjunction')).declarations[0].tokens[0].value,'AND')
        before=queries(m)
        with self.assertRaises(RuntimeError):
            with m.transaction():m.links.rename('P','Road');raise RuntimeError('rollback')
        self.assertEqual(queries(m),before)
        m.controls.rename(('VARIABLE','V'),'DepthVariable');m.controls.rename(('RULE','R'),'Rule');m.links.rename('P','Road')
        info=m.field_provenance(symbol('RULE','Rule'),('conditions',1,'left','reference','key',1))
        self.assertEqual(info.declarations[0].tokens[0].value,'V')
        self.assertEqual(queries(Model.from_json_document(m.to_json_document(),strict=True)),queries(m))

    def test_missing_target_invalid_kind_extensions_and_malformed_whole_program(self):
        m=load('[CONTROLS]\nRULE R\nIF NODE Lost DEPTH > 0\nTHEN CONDUIT Missing STATUS = OPEN\n',strict=False)
        self.assertEqual(m.inspect_field(symbol('RULE','R'),'priority').semantics.effective.status,'invalid')
        m=load(fixture('setting',('ORIFICE','CURVE Control')));m.curves.update('Control',kind='RATING')
        self.assertEqual(m.inspect_field(symbol('RULE','R'),'priority').semantics.effective.status,'invalid')
        @dataclass(frozen=True,kw_only=True)
        class FutureAttribute(c.Attribute):pass
        m=load(fixture());m.controls.update(('VARIABLE','DepthValue'),value=FutureAttribute(object_type='NODE',attribute='DEPTH',target=Ref(collection='swmm:nodes',key='J')))
        self.assertEqual(m.inspect_field(symbol('VARIABLE','DepthValue'),'value').semantics.effective.status,'unknown')
        source=fixture()+'FUTURE state\n';m=load(source,strict=False)
        self.assertFalse(m.controls);self.assertEqual(m.document.text,source)


if __name__=='__main__':unittest.main()
