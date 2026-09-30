"""Independent OPTIONS alternative lifecycles and lexical/numeric boundaries."""
from datetime import timedelta
import unittest

from easysewer.io.inp import InpDocument
from easysewer.model import Model, Ref
from easysewer.model.store import RecordConflictError
from easysewer.validation import ValidationError
from test_option_variants_v2 import CHOICES, BOOLEANS

OWNER=Ref(collection='swmm:options',key='settings')
SOURCE='C:/fixtures/options-gates.inp'
EVIDENCE=[]
ALIASES={'NF':'STEADY','KW':'KINWAVE','EKW':'KINWAVE','XKINWAVE':'KINWAVE','DW':'DYNWAVE'}
NUMBERS=('SYS_FLOW_TOL','LAT_FLOW_TOL','DRY_DAYS','VARIABLE_STEP','MIN_SURFAREA',
         'MIN_SLOPE','HEAD_TOLERANCE','MAX_TRIALS','THREADS')
STEPS=('REPORT_STEP','WET_STEP','DRY_STEP','RULE_STEP','ROUTING_STEP','LENGTHENING_STEP','MINIMUM_STEP')
BAD_NUMBERS=('NaN','inf','-inf','1e309','1_0','１','١','0x10','2junk','--1')


def field(keyword):return {'MIN_SURFAREA':'min_surface_area'}.get(keyword,keyword.lower())


def values(keyword,token):
    if keyword in BOOLEANS:return {field(keyword):token=='YES'}
    if keyword=='COMPATIBILITY':return {'compatibility':int(token)}
    if keyword=='FLOW_ROUTING':
        if token=='NONE':return {'flow_routing':'DYNWAVE','ignore_routing':True}
        return {'flow_routing':ALIASES.get(token,token)}
    return {field(keyword):token}


def load(text,strict=True):
    return Model.from_document(InpDocument.from_text(text,source=SOURCE),strict=strict)


class OptionGateBoundaryTests(unittest.TestCase):
    def equivalent(self,m):
        for restored in (Model.from_document(m.to_document(normalize=True),strict=True),
                         Model.from_json_document(m.to_json_document(),strict=True)):
            self.assertEqual(restored.options,m.options)

    def test_singleton_identity_rejects_rename_and_duplicate_and_restores_removal(self):
        m=load('[OPTIONS]\nRULE_STEP 0\n[OPTIONS]\nRULE_STEP 00:00:10\n')
        collection=m.collection('swmm:options');original=m.options
        self.assertEqual(tuple(collection),('settings',))
        before=m.to_json_document().to_bytes()
        with self.assertRaises(TypeError):collection.rename('settings','Other')
        self.assertEqual(m.to_json_document().to_bytes(),before)
        with self.assertRaises(RecordConflictError):collection.add(original)
        self.assertEqual(m.to_json_document().to_bytes(),before)
        with self.assertRaises(RuntimeError),m.transaction():
            collection.remove('settings')
            self.assertFalse(collection)
            raise RuntimeError('rollback removal')
        self.assertEqual(m.to_json_document().to_bytes(),before)
        collection.move('settings');self.assertEqual(tuple(collection),('settings',))
        collection.remove('settings');self.assertFalse(m.to_document().records('OPTIONS'))
        collection.add(original);self.assertEqual(tuple(collection),('settings',))
        self.assertEqual(m.options,original);self.equivalent(m)
        EVIDENCE.append(dict(kind='singleton-identity',key='settings',rename_rejected=True,duplicate_rejected=True))

    def invalid(self,keyword,token):
        text='[OPTIONS]\r\n'+keyword+(' '+token if token else '')+' ; retain\r\n'
        with self.assertRaises(ValidationError):load(text)
        m=load(text,False)
        self.assertEqual(m.document.to_bytes(),text.encode())
        errors=[d for d in m.validate().errors if d.code=='options.invalid_input']
        self.assertEqual(len(errors),1)
        self.assertEqual((errors[0].field,errors[0].span.source,errors[0].span.line),(field(keyword),SOURCE,2))
        self.assertEqual(m.field_provenance(OWNER,field(keyword)).status,'unknown')
        restored=Model.from_json_document(m.to_json_document(),strict=False)
        self.assertEqual(restored.document.to_bytes(),m.document.to_bytes())
        with self.assertRaises(ValidationError):restored.to_document(normalize=True)

    def test_every_alternative_create_edit_clear_sources_json_and_rollback(self):
        alternatives={**CHOICES,**{k:('YES','NO') for k in BOOLEANS},'COMPATIBILITY':('3','4','5')}
        self.assertEqual(sum(len(v) for v in alternatives.values()),54)
        for keyword,tokens in alternatives.items():
            for token in tokens:
                with self.subTest(keyword=keyword,token=token):
                    expected=values(keyword,token)
                    text='\ufeff[options]\r\n'+keyword.lower()+' '+token.lower()+' ; first\r\n[OPTIONS]\r\n'+keyword+' '+token+' ; last\r\n'
                    imported=load(text)
                    self.assertEqual(imported.to_document().to_bytes(),text.encode())
                    for key,value in expected.items():self.assertEqual(getattr(imported.options,key),value)
                    self.equivalent(imported)
                    model=Model();model.update_options(**expected)
                    self.assertEqual(model.options,imported.options)
                    self.equivalent(model)
                    changed=values(keyword,next(t for t in tokens if values(keyword,t)!=expected))
                    # NONE owns a second semantic field; clear that side effect
                    # explicitly when switching to an ordinary routing value.
                    if keyword=='FLOW_ROUTING':changed={'ignore_routing':None,**changed}
                    before=imported.to_json_document().to_bytes()
                    with self.assertRaises(RuntimeError),imported.transaction():
                        imported.update_options(**changed)
                        raise RuntimeError('rollback')
                    self.assertEqual(imported.to_json_document().to_bytes(),before)
                    imported.update_options(**changed)
                    for key,value in changed.items():self.assertEqual(getattr(imported.options,key),value)
                    self.equivalent(imported)
                    imported.update_options(**{key:None for key in expected.keys()|changed.keys()})
                    self.assertFalse(imported.to_document().records('OPTIONS'))
                    self.assertIn('; first',imported.to_document().text)
                    self.assertIn('; last',imported.to_document().text)
                    self.equivalent(imported)
                    EVIDENCE.append(dict(kind='alternative-lifecycle',keyword=keyword,token=token))

    def test_numeric_lexical_errors_all_numeric_and_step_fields(self):
        for keyword in (*NUMBERS,*STEPS):
            for token in BAD_NUMBERS:
                with self.subTest(keyword=keyword,token=token):
                    self.invalid(keyword,token)
                    EVIDENCE.append(dict(kind='numeric-lexical-rejection',keyword=keyword,token=token))

    def test_choice_boolean_compatibility_and_extra_column_errors(self):
        for keyword,tokens in {**CHOICES,**{k:('YES','NO') for k in BOOLEANS}}.items():
            for token in (tokens[0][:-1],tokens[0]+'_FUTURE','0','1','',tokens[0]+' extra'):
                with self.subTest(keyword=keyword,token=token):
                    self.invalid(keyword,token)
                    EVIDENCE.append(dict(kind='choice-lexical-rejection',keyword=keyword,token=token))
        for token in ('2','6','03','3.0','3junk','', '4 extra'):
            with self.subTest(compatibility=token):
                self.invalid('COMPATIBILITY',token)
                EVIDENCE.append(dict(kind='compatibility-lexical-rejection',token=token))

    def test_numeric_api_boundaries_zero_omission_and_invalid_rollback(self):
        cases=(('sys_flow_tol',(-1.,0.,1.),(True,)),('lat_flow_tol',(-1.,0.,1.),(True,)),
            ('head_tolerance',(-1.,0.,1.),(True,)),('dry_days',(0.,.5),(-.001,True)),
            ('min_surface_area',(0.,1.),(-.001,True)),('min_slope',(0.,99.999),(100.,-.001,True)),
            ('variable_step',(0.,2.),(-.001,2.0001,True)),
            ('threads',(0,1,2147483647),(-1,2147483648,True,1.5)),
            ('max_trials',(0,1,2147483647),(-1,2147483648,True,1.5)))
        for name,good,bad in cases:
            for value in good:
                with self.subTest(field=name,value=value):
                    m=Model();self.assertIsNone(getattr(m.options,name))
                    m.update_options(**{name:value});self.equivalent(m)
                    self.assertEqual(getattr(m.options,name),value)
                    before=m.to_json_document().to_bytes()
                    for invalid in (*bad,float('nan'),float('inf'),-float('inf')):
                        with self.assertRaises(ValidationError):m.update_options(**{name:invalid})
                        self.assertEqual(m.to_json_document().to_bytes(),before)
                    m.update_options(**{name:None});self.assertFalse(m.to_document().records('OPTIONS'))
                    EVIDENCE.append(dict(kind='numeric-api-boundary',field=name,value=value))
        for keyword in STEPS:
            name=field(keyword);zero_valid=keyword in ('RULE_STEP','LENGTHENING_STEP','MINIMUM_STEP')
            m=Model();self.assertIsNone(getattr(m.options,name))
            if zero_valid:
                m.update_options(**{name:timedelta()});self.equivalent(m)
                self.assertIsNotNone(getattr(m.options,name))
            else:
                with self.assertRaises(ValidationError):m.update_options(**{name:timedelta()})
            m.update_options(**{name:timedelta(seconds=1)});before=m.to_json_document().to_bytes()
            with self.assertRaises(ValidationError):m.update_options(**{name:timedelta(microseconds=-1)})
            self.assertEqual(m.to_json_document().to_bytes(),before)
            m.update_options(**{name:None});self.assertFalse(m.to_document().records('OPTIONS'))
            EVIDENCE.append(dict(kind='step-zero-boundary',keyword=keyword,zero_valid=zero_valid))

    def test_native_integer_coercion_and_duration_units_are_explicit(self):
        for keyword in ('THREADS','MAX_TRIALS'):
            for token,expected in (('2.9',2),('2e3',2),('+.5',0),('-0.9',0),('+02',2)):
                with self.subTest(keyword=keyword,token=token):
                    m=load('[OPTIONS]\n'+keyword+' '+token+'\n')
                    self.assertEqual(getattr(m.options,field(keyword)),expected)
                    coercions=[d for d in m.validate().diagnostics if d.code=='options.native_coercion']
                    self.assertEqual(len(coercions),int(token!='+02'))
                    if coercions:self.assertEqual((coercions[0].span.source,coercions[0].span.line),(SOURCE,2))
                    self.equivalent(m)
                    EVIDENCE.append(dict(kind='integer-coercion',keyword=keyword,token=token,value=expected))
        for keyword in STEPS:
            scheduled=keyword in ('REPORT_STEP','WET_STEP','DRY_STEP','RULE_STEP')
            for token,seconds in (('.5',1800 if scheduled else .5),('0.0004',1 if scheduled else .0004)):
                m=load('[OPTIONS]\n'+keyword+' '+token+'\n')
                self.assertEqual(getattr(m.options,field(keyword)),timedelta(seconds=seconds));self.equivalent(m)
                EVIDENCE.append(dict(kind='numeric-duration-units',keyword=keyword,token=token,seconds=seconds))
            if keyword=='MINIMUM_STEP':self.invalid(keyword,'00:00:02')
            else:
                m=load('[OPTIONS]\n'+keyword+' 00:00:02.9\n')
                self.assertEqual(getattr(m.options,field(keyword)),timedelta(seconds=2));self.equivalent(m)
                self.assertTrue(any(d.code=='options.native_coercion' for d in m.validate().diagnostics))
            for token in ('00:60:00','00:00:60','00:00:-1','00:00:01_extra'):
                self.invalid(keyword,token)


if __name__=='__main__':unittest.main()
