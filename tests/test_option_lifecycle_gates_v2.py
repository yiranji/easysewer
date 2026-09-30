"""Independent explicit values for every option, plus portable numeric bounds."""
from dataclasses import replace
from datetime import date, time, timedelta
import unittest

from easysewer.io.inp import InpDocument
from easysewer.io.json import JsonDocument
from easysewer.model import Model, Ref
from easysewer.model.options import DayTime, MonthDay, Options
from easysewer.model.values import FileReference
from easysewer.model.fields import validate_fields
from easysewer.schema.option_profile import OPTION_DEFINITIONS
from easysewer.validation import ValidationError
from test_options_v2 import network

OWNER = Ref(collection='swmm:options', key='settings')
# Literal input, typed expectation and a distinct valid edit. Expectations are
# independent of parse_option/format_option and profile defaults.
CASES = (
    ('FLOW_UNITS', 'flow_units', 'CMS', 'CMS', 'GPM'),
    ('INFILTRATION', 'infiltration', 'MODIFIED_GREEN_AMPT', 'MODIFIED_GREEN_AMPT', 'HORTON'),
    ('FLOW_ROUTING', 'flow_routing', 'KINWAVE', 'KINWAVE', 'DYNWAVE'),
    ('LINK_OFFSETS', 'link_offsets', 'ELEVATION', 'ELEVATION', 'DEPTH'),
    ('FORCE_MAIN_EQUATION', 'force_main_equation', 'D-W', 'D-W', 'H-W'),
    *((name.upper(), name, 'YES', True, False) for name in (
        'ignore_rainfall', 'ignore_snowmelt', 'ignore_groundwater', 'ignore_rdii',
        'ignore_routing', 'ignore_quality', 'allow_ponding', 'skip_steady_state', 'slope_weighting')),
    ('SYS_FLOW_TOL', 'sys_flow_tol', '4', 4.0, 0.0),
    ('LAT_FLOW_TOL', 'lat_flow_tol', '3', 3.0, 0.0),
    ('START_DATE', 'start_date', 'Jan-01-2020', date(2020, 1, 1), date(2020, 2, 29)),
    ('START_TIME', 'start_time', '0.5', time(0, 30), time(1, 2, 3)),
    ('END_DATE', 'end_date', '01/02/2020', date(2020, 1, 2), date(2021, 1, 1)),
    ('END_TIME', 'end_time', '24:00:00', DayTime(day_offset=1), time(2, 3, 4)),
    ('REPORT_START_DATE', 'report_start_date', '01/01/2020', date(2020, 1, 1), date(2020, 2, 29)),
    ('REPORT_START_TIME', 'report_start_time', '00:02:00', time(0, 2), DayTime(day_offset=1)),
    ('SWEEP_START', 'sweep_start', '3/1', MonthDay(month=3, day=1), MonthDay(month=12, day=31)),
    ('SWEEP_END', 'sweep_end', '10/31', MonthDay(month=10, day=31), MonthDay(month=1, day=1)),
    ('DRY_DAYS', 'dry_days', '0.5', .5, 0.0),
    ('REPORT_STEP', 'report_step', '00:01:00', timedelta(minutes=1), timedelta(seconds=30)),
    ('WET_STEP', 'wet_step', '00:05:00', timedelta(minutes=5), timedelta(seconds=45)),
    ('DRY_STEP', 'dry_step', '01:00:00', timedelta(hours=1), timedelta(minutes=15)),
    ('RULE_STEP', 'rule_step', '0.5', timedelta(minutes=30), timedelta()),
    ('ROUTING_STEP', 'routing_step', '.25', timedelta(seconds=.25), timedelta(seconds=2)),
    ('LENGTHENING_STEP', 'lengthening_step', '.5', timedelta(seconds=.5), timedelta()),
    ('MINIMUM_STEP', 'minimum_step', '.1', timedelta(seconds=.1), timedelta()),
    ('VARIABLE_STEP', 'variable_step', '.75', .75, 0.0),
    ('INERTIAL_DAMPING', 'inertial_damping', 'PARTIAL', 'PARTIAL', 'NONE'),
    ('NORMAL_FLOW_LIMITED', 'normal_flow_limited', 'NONE', 'NONE', 'BOTH'),
    ('SURCHARGE_METHOD', 'surcharge_method', 'SLOT', 'SLOT', 'EXTRAN'),
    ('MIN_SURFAREA', 'min_surface_area', '1.25', 1.25, 0.0),
    ('MIN_SLOPE', 'min_slope', '.1', .1, 0.0),
    ('MAX_TRIALS', 'max_trials', '8', 8, 0),
    ('HEAD_TOLERANCE', 'head_tolerance', '.0015', .0015, 0.0),
    ('THREADS', 'threads', '1', 1, 0),
    ('COMPATIBILITY', 'compatibility', '4', 4, 5),
    ('TEMPDIR', 'temp_directory', '"temporary files"', FileReference(path='temporary files', direction='output'),
     FileReference(path='new folder', direction='output')),
)
EVIDENCE = []


class OptionLifecycleGateTests(unittest.TestCase):
    def roundtrip(self, model):
        for restored in (Model.from_document(model.to_document(normalize=True), strict=True),
                         Model.from_json_document(model.to_json_document(), strict=True)):
            self.assertEqual(restored.options, model.options)

    def test_all_43_options_create_edit_clear_and_rollback(self):
        self.assertEqual({(k, f) for k, f, *_ in CASES}, {(d.keyword, d.field) for d in OPTION_DEFINITIONS})
        self.assertEqual(len(CASES), 43)
        for keyword, field, token, expected, changed in CASES:
            with self.subTest(keyword=keyword):
                model = Model()
                model.update_options(**{field: expected})
                self.assertEqual(getattr(model.options, field), expected)
                self.assertEqual(len(model.to_document().records('OPTIONS')), 1)
                self.roundtrip(model)
                before = model.to_json_document().to_bytes()
                with self.assertRaisesRegex(RuntimeError, 'rollback'), model.transaction():
                    model.update_options(**{field: changed})
                    raise RuntimeError('rollback')
                self.assertEqual(model.to_json_document().to_bytes(), before)
                model.update_options(**{field: changed})
                self.assertEqual(getattr(model.options, field), changed)
                self.roundtrip(model)
                model.update_options(**{field: None})
                self.assertIsNone(getattr(model.options, field))
                self.assertFalse(model.to_document().records('OPTIONS'))
                self.roundtrip(model)
                EVIDENCE.append(dict(kind='option-lifecycle', keyword=keyword, field=field))

    def test_all_43_literal_values_repeated_sections_sources_and_json(self):
        for keyword, field, token, expected, changed in CASES:
            with self.subTest(keyword=keyword):
                text = '\ufeff[OPTIONS]\r\n'+keyword.lower()+' '+token+' ; first\r\n[OPTIONS]\r\n'+keyword+' '+token+' ; last\r\n'
                model = Model.from_document(InpDocument.from_text(text), strict=True)
                self.assertEqual(model.to_document().to_bytes(), text.encode('utf-8'))
                self.assertEqual(getattr(model.options, field), expected)
                source = model.field_provenance(OWNER, field)
                self.assertEqual([d.contributes for d in source.declarations], [False, True])
                self.assertEqual([d.tokens[0].span.line for d in source.declarations], [2, 4])
                self.assertEqual(source.declarations[-1].tokens[0].value, token.strip('"'))
                self.roundtrip(model)
                model.update_options(**{field: changed})
                self.assertEqual(len(model.to_document().records('OPTIONS')), 1)
                self.roundtrip(model)
                self.assertEqual(model.field_provenance(OWNER, field), source)
                model.update_options(**{field: None})
                self.assertFalse(model.to_document().records('OPTIONS'))
                self.assertIn('; first', model.to_document().text)
                self.assertIn('; last', model.to_document().text)

    def test_integer_option_upper_bound_is_shared_by_api_inp_json_and_rollback(self):
        for field in ('threads', 'max_trials'):
            for good in (0, 1, 2_147_483_647):
                with self.subTest(field=field, valid=good):
                    model = Model()
                    model.update_options(**{field: good})
                    self.roundtrip(model)
            for bad in (-1, 2_147_483_648, 10**400, True, 1.5):
                with self.subTest(field=field, invalid=str(bad)):
                    model = network()
                    before = model.to_json_document().to_bytes()
                    with self.assertRaises(ValidationError) as caught:
                        model.update_options(**{field: bad})
                    self.assertTrue(any(d.field == field for d in caught.exception.report.errors))
                    self.assertEqual(model.to_json_document().to_bytes(), before)
                    with self.assertRaises(ValidationError), model.transaction():
                        model.nodes.update('J', initial_depth=2)
                        model.update_options(**{field: bad})
                    self.assertEqual(model.to_json_document().to_bytes(), before)
                    invalid = replace(Options(), **{field: bad})
                    self.assertTrue(tuple(validate_fields(invalid)))
                    data = model.to_json_document().data
                    block = next(row for row in data['collections'] if row['collection'] == 'swmm:options')
                    block['records'][0]['value'][field] = bad
                    with self.assertRaises((ValidationError, ValueError)):
                        Model.from_json_document(JsonDocument.from_data(data), strict=True)
            for bad in (2_147_483_648, 10**400):
                text = '[OPTIONS]\n'+field.upper()+' '+str(bad)+'\n'
                imported = Model.from_document(InpDocument.from_text(text))
                self.assertEqual(imported.document.text, text)
                errors = imported.validate().errors
                self.assertTrue(errors)
                self.assertTrue(any(d.code == 'options.invalid_input' and d.span.line == 2 for d in errors))
                with self.assertRaises(ValidationError):
                    Model.from_document(InpDocument.from_text(text), strict=True)

    def test_huge_numeric_values_return_field_diagnostics_without_overflow(self):
        huge = 10**400
        for field in ('sys_flow_tol', 'lat_flow_tol', 'dry_days', 'variable_step',
                      'min_surface_area', 'min_slope', 'head_tolerance'):
            with self.subTest(field=field):
                model = network()
                before = model.to_json_document().to_bytes()
                with self.assertRaises(ValidationError) as caught:
                    model.update_options(**{field: huge})
                self.assertTrue(any(d.field == field for d in caught.exception.report.errors))
                self.assertEqual(model.to_json_document().to_bytes(), before)
        for field in ('elevation', 'max_depth', 'initial_depth', 'ponded_area'):
            with self.subTest(node_field=field):
                model = network()
                before = model.to_json_document().to_bytes()
                with self.assertRaises(ValidationError), model.transaction():
                    model.nodes.update('J', **{field: huge})
                self.assertEqual(model.to_json_document().to_bytes(), before)


if __name__ == '__main__':
    unittest.main()
