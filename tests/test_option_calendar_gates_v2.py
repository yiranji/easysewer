"""Native calendar sentinels, safe clock encoding and source-preserving errors."""
from datetime import date, datetime, time, timedelta
import unittest

from easysewer.io.inp import InpDocument
from easysewer.model import Model, Ref
from easysewer.model.options import DayTime
from easysewer.validation import ValidationError
from test_native_v2_network import NETWORK, SETTINGS

OWNER = Ref(collection='swmm:options', key='settings')
BASE = NETWORK.format(shape='CIRCULAR 2 0 0 0') + SETTINGS


def load(tail='', *, strict=True):
    return Model.from_document(InpDocument.from_text(BASE + '[OPTIONS]\n' + tail,
        source='calendar.inp'), strict=strict)


# Expected dates are independent literals, not constructed with the resolver.
CALENDARS = (
    ('', datetime(2020, 1, 1), True),
    ('REPORT_START_DATE 01/01/2020\n', datetime(2020, 1, 1), True),
    ('REPORT_START_TIME 00:05\n', datetime(2020, 1, 1), True),
    ('REPORT_START_DATE 12/31/3918\n', datetime(2020, 1, 1), True),
    ('REPORT_START_DATE 01/01/3919\n', datetime(2020, 1, 2), False),
    ('REPORT_START_DATE 01/01/4000\n', datetime(2101, 1, 2), False),
    ('REPORT_START_TIME 17698200\n', datetime(2020, 1, 1), True),
    ('REPORT_START_TIME 17698200.083333332\n', datetime(2020, 1, 1, 0, 5), True),
    ('REPORT_START_DATE 01/01/2020\nREPORT_START_TIME 00:05\n', datetime(2020, 1, 1, 0, 5), True),
    ('REPORT_START_DATE 01/01/2020\nREPORT_START_TIME 00:10\n', datetime(2020, 1, 1, 0, 10), False),
    ('REPORT_START_DATE 12/31/2019\nREPORT_START_TIME 24:09:59\n', datetime(2020, 1, 1, 0, 9, 59), True),
    ('START_DATE 02/28/2020\nSTART_TIME 23:55\nEND_DATE 02/29/2020\nEND_TIME 00:05\n'
     'REPORT_START_DATE 02/28/2020\nREPORT_START_TIME 24:00\n', datetime(2020, 2, 29), True),
)


class OptionCalendarGateTests(unittest.TestCase):
    def test_shared_control_and_series_clocks_keep_large_and_fractional_values(self):
        for token in ('17698200.0833333333333333333333333', '596523.2355555555555555555555556',
                      '0.0000000002777777777777777777777777777778'):
            with self.subTest(token=token):
                tail = ('[TIMESERIES]\nBig 0 1\nBig '+token+' 2\n'
                        '[CONTROLS]\nRULE Clock\nIF SIMULATION TIME > '+token+'\n'
                        'THEN CONDUIT P STATUS = CLOSED\n')
                m = load(tail)
                restored = Model.from_document(m.to_document(normalize=True), strict=True)
                for collection in ('swmm:controls', 'swmm:timeseries'):
                    self.assertEqual(tuple(m.collection(collection).items()), tuple(restored.collection(collection).items()))

    def test_partial_calendars_defaults_json_and_field_facts(self):
        for tail, expected, valid in CALENDARS:
            with self.subTest(tail=tail):
                model = load(tail)
                for m in (model, Model.from_json_document(model.to_json_document(), strict=True),
                          Model.from_document(model.to_document(normalize=True), strict=True)):
                    self.assertEqual(m.validate(for_run=True).is_valid, valid)
                    self.assertLessEqual(abs(m.effective_options.report_start - expected), timedelta(microseconds=20))
                    for field in ('report_start_date', 'report_start_time'):
                        fact = m.inspect_field(OWNER, field).semantics.effective
                        self.assertEqual(fact.status, 'known' if valid else 'invalid')
                        self.assertEqual(field in m.effective_options.defaults_used, getattr(m.options, field) is None)
                    if not valid:
                        errors = [d for d in m.validate(for_run=True).errors if d.code == 'options.invalid_report_start']
                        self.assertEqual(len(errors), 1)
                        self.assertEqual(errors[0].subject.collection, 'swmm:options')
                self.assertEqual(model.to_document().text, BASE + '[OPTIONS]\n' + tail)

    def test_safe_decimal_encoding_at_native_integer_clock_boundary(self):
        # The native colon parser multiplies hours by 3600 in signed int.
        for seconds in (2_147_483_647, 2_147_483_648, 63_713_520_000):
            for field in ('start_time', 'end_time', 'report_start_time'):
                with self.subTest(seconds=seconds, field=field):
                    duration = timedelta(seconds=seconds)
                    value = DayTime(day_offset=duration.days,
                        clock=time(duration.seconds//3600, duration.seconds%3600//60, duration.seconds%60))
                    m = Model(); m.update_options(**{field: value})
                    doc = m.to_document()
                    token = doc.records('OPTIONS')[0].values[1]
                    self.assertEqual(':' in token, seconds <= 2_147_483_647)
                    self.assertEqual(Model.from_document(doc, strict=True).options, m.options)

    def test_overflowing_colon_source_is_retained_with_precise_diagnostic(self):
        for keyword in ('START_TIME', 'END_TIME', 'REPORT_START_TIME', 'REPORT_STEP',
                        'WET_STEP', 'DRY_STEP', 'RULE_STEP', 'ROUTING_STEP', 'LENGTHENING_STEP'):
            for token in ('596523:14:08', '17698200:05:00'):
                with self.subTest(keyword=keyword, token=token):
                    text = '[OPTIONS]\n' + keyword + ' ' + token + ' ; keep invalid source\n'
                    document = InpDocument.from_text(text, source='overflow.inp')
                    with self.assertRaises(ValidationError): Model.from_document(document, strict=True)
                    m = Model.from_document(document, strict=False)
                    errors = [d for d in m.validate().errors if d.code == 'options.invalid_input']
                    self.assertEqual(len(errors), 1)
                    self.assertEqual((errors[0].span.source, errors[0].span.line), ('overflow.inp', 2))
                    self.assertEqual(errors[0].field, keyword.lower())
                    self.assertEqual(m.field_provenance(OWNER, keyword.lower()).status, 'unknown')
                    with self.assertRaises(ValidationError): m.to_document()

    def test_calendar_overflow_is_diagnostic_and_edit_clear_rollback_work(self):
        for changes in ({'report_start_time': DayTime(day_offset=4_000_000)},
                        {'report_start_time': DayTime(day_offset=10**400)},
                        {'report_start_date': date.max, 'report_start_time': DayTime(day_offset=1)}):
            with self.subTest(changes=changes):
                m = load(); m.update_options(**changes)
                self.assertIn('options.calendar_overflow', [d.code for d in m.validate(for_run=True).errors])
                with self.assertRaises(ValidationError): _ = m.effective_options
        m = load('REPORT_START_DATE 01/01/4000\n')
        before = m.to_json_document()
        with self.assertRaises(RuntimeError):
            with m.transaction():
                m.update_options(report_start_date=date(2020, 1, 1), report_start_time=time(0, 5))
                raise RuntimeError('rollback')
        self.assertEqual(m.to_json_document(), before)
        m.update_options(report_start_date=None)
        self.assertTrue(m.validate(for_run=True).is_valid)
        m.update_options(report_start_time=DayTime(day_offset=737425, clock=time(0, 5)))
        self.assertLessEqual(abs(m.effective_options.report_start-datetime(2020, 1, 1, 0, 5)), timedelta(microseconds=20))
        restored = Model.from_document(m.to_document(normalize=True), strict=True)
        self.assertEqual(restored.options, m.options)
        self.assertEqual(restored.effective_options, m.effective_options)


if __name__ == '__main__': unittest.main()
