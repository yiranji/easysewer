"""Independent literals for all option input defaults and unit facts."""
from datetime import date,time,timedelta
import unittest
from easysewer.io.inp import InpDocument
from easysewer.model import Model,Ref
from easysewer.model.options import MonthDay

DEFAULTS = (
    ('flow_units', 'FLOW_UNITS', 'CFS', 'CFS'),
    ('infiltration', 'INFILTRATION', 'HORTON', 'HORTON'),
    ('flow_routing', 'FLOW_ROUTING', 'DYNWAVE', 'DYNWAVE'),
    ('link_offsets', 'LINK_OFFSETS', 'DEPTH', 'DEPTH'),
    ('force_main_equation', 'FORCE_MAIN_EQUATION', 'H-W', 'H-W'),
    ('ignore_rainfall', 'IGNORE_RAINFALL', 'NO', False),
    ('ignore_snowmelt', 'IGNORE_SNOWMELT', 'NO', False),
    ('ignore_groundwater', 'IGNORE_GROUNDWATER', 'NO', False),
    ('ignore_rdii', 'IGNORE_RDII', 'NO', False),
    ('ignore_routing', 'IGNORE_ROUTING', 'NO', False),
    ('ignore_quality', 'IGNORE_QUALITY', 'NO', False),
    ('allow_ponding', 'ALLOW_PONDING', 'NO', False),
    ('skip_steady_state', 'SKIP_STEADY_STATE', 'NO', False),
    ('sys_flow_tol', 'SYS_FLOW_TOL', '5', 5.0),
    ('lat_flow_tol', 'LAT_FLOW_TOL', '5', 5.0),
    ('start_date', 'START_DATE', '01/01/2004', date(2004, 1, 1)),
    ('start_time', 'START_TIME', '00:00', time()),
    ('end_date', 'END_DATE', '01/01/2004', date(2004, 1, 1)),
    ('end_time', 'END_TIME', '00:00', time()),
    ('report_start_date', 'REPORT_START_DATE', None, None),
    ('report_start_time', 'REPORT_START_TIME', None, None),
    ('sweep_start', 'SWEEP_START', '01/01', MonthDay(month=1, day=1)),
    ('sweep_end', 'SWEEP_END', '12/31', MonthDay(month=12, day=31)),
    ('dry_days', 'DRY_DAYS', '0', 0.0),
    ('report_step', 'REPORT_STEP', '00:15', timedelta(minutes=15)),
    ('wet_step', 'WET_STEP', '00:05', timedelta(minutes=5)),
    ('dry_step', 'DRY_STEP', '01:00', timedelta(hours=1)),
    ('rule_step', 'RULE_STEP', '00:00', timedelta()),
    ('routing_step', 'ROUTING_STEP', '20', timedelta(seconds=20)),
    ('lengthening_step', 'LENGTHENING_STEP', '0', timedelta()),
    ('minimum_step', 'MINIMUM_STEP', '0.5', timedelta(seconds=.5)),
    ('variable_step', 'VARIABLE_STEP', '.75', .75),
    ('inertial_damping', 'INERTIAL_DAMPING', 'PARTIAL', 'PARTIAL'),
    ('normal_flow_limited', 'NORMAL_FLOW_LIMITED', 'BOTH', 'BOTH'),
    ('surcharge_method', 'SURCHARGE_METHOD', 'EXTRAN', 'EXTRAN'),
    ('min_surface_area', 'MIN_SURFAREA', '0', 0.0),
    ('min_slope', 'MIN_SLOPE', '0', 0.0),
    ('max_trials', 'MAX_TRIALS', '0', 0),
    ('head_tolerance', 'HEAD_TOLERANCE', '0', 0.0),
    ('threads', 'THREADS', '1', 1),
    ('slope_weighting', 'SLOPE_WEIGHTING', 'YES', True),
    ('compatibility', 'COMPATIBILITY', '4', 4),
    ('temp_directory', 'TEMPDIR', None, None),
)

EVIDENCE=[]

class OptionDefaultTests(unittest.TestCase):
    def test_all_43_input_defaults_explicit_omission_and_unit_facts(self):
        self.assertEqual(len(DEFAULTS),43)
        self.assertEqual(set(dict(Model().profile.option_defaults)),{n for n,_,_,_ in DEFAULTS})
        dimensional={'sys_flow_tol':'%','lat_flow_tol':'%','dry_days':'day','variable_step':'1',
            'min_slope':'%','max_trials':'count','threads':'count'}
        clocks={'start_time','end_time','report_start_time'}
        dates={'start_date','end_date','report_start_date'}
        steps={'report_step','wet_step','dry_step','rule_step','routing_step','lengthening_step','minimum_step'}
        owner=Ref(collection='swmm:options',key='settings')
        for unit in ('CFS','GPM','MGD','CMS','LPS','MLD'):
            for name,keyword,literal,expected in DEFAULTS:
                if name=='flow_units' and unit!='CFS':continue
                with self.subTest(unit=unit,field=name):
                    text='[OPTIONS]\n'
                    if name!='flow_units':text+='FLOW_UNITS '+unit+'\n'
                    text+=('START_DATE 12/31/2003\nSTART_TIME 23:50\n' if name=='end_time' else 'END_TIME 00:10\n')
                    m=Model.from_document(InpDocument.from_text(text),strict=True)
                    self.assertTrue(m.validate(for_run=True).is_valid)
                    self.assertIsNone(getattr(m.options,name))
                    self.assertIn(name,m.effective_options.defaults_used)
                    info=m.inspect_field(owner,name)
                    self.assertEqual((info.provenance.status,info.semantics.default.status),('omitted','known'))
                    self.assertEqual(info.semantics.default.value,expected)
                    self.assertEqual(m.profile.option_default(name),expected)
                    us=unit in ('CFS','GPM','MGD')
                    expected_unit=dimensional.get(name)
                    if name=='min_surface_area':expected_unit='ft2' if us else 'm2'
                    elif name=='head_tolerance':expected_unit='ft' if us else 'm'
                    elif name in clocks:expected_unit='local model clock'
                    elif name in dates:expected_unit='local model date'
                    elif name in steps:expected_unit='s'
                    elif name in ('sweep_start','sweep_end'):expected_unit='month/day'
                    if expected_unit is None:self.assertEqual(info.semantics.unit.status,'not_applicable')
                    else:self.assertEqual((info.semantics.unit.status,info.semantics.unit.value),('known',expected_unit))
                    if name in ('min_surface_area','head_tolerance','max_trials'):
                        adjusted={'min_surface_area':12.566*(1 if us else .3048**2),
                                  'head_tolerance':.005*(1 if us else .3048),'max_trials':8}[name]
                        self.assertAlmostEqual(info.semantics.effective.value,adjusted)
                    if literal is not None:
                        explicit=Model.from_document(InpDocument.from_text(text+keyword+' '+literal+'\n'),strict=True)
                        self.assertEqual(getattr(explicit.options,name),expected)
                        self.assertNotIn(name,explicit.effective_options.defaults_used)
                        self.assertEqual(explicit.field_provenance(owner,name).status,'explicit')
                        for restored in (Model.from_document(explicit.to_document(normalize=True),strict=True),
                                         Model.from_json_document(explicit.to_json_document(),strict=True)):
                            self.assertEqual(restored.options,explicit.options)
                            self.assertEqual(restored.inspect_field(owner,name).semantics,explicit.inspect_field(owner,name).semantics)
                        explicit.update_options(**{name:None})
                        self.assertIsNone(getattr(explicit.options,name))
                        self.assertIn(name,explicit.effective_options.defaults_used)
                    EVIDENCE.append(dict(kind='independent-option-default',unit=unit,keyword=keyword,
                        explicit_spelling=literal,unit_fact=expected_unit))

if __name__=='__main__':unittest.main()
