from dataclasses import replace
from datetime import datetime, timedelta, timezone
import unittest

from easysewer.io.inp import InpDocument
from easysewer.model import Model, Ref
from easysewer.model.events import EventSchedule, RoutingEvent
from easysewer.scenario import ScenarioPatch, SetFields, FieldChange
from easysewer.validation import ValidationError
from test_options_v2 import network
from test_scenario_v2 import portable


def load(source):
    return Model.from_document(InpDocument.from_text(source), strict=True)


def period(start=0, end=60):
    day = datetime(2020, 1, 1)
    return RoutingEvent(start=day + timedelta(seconds=start), end=day + timedelta(seconds=end))


class EventsTests(unittest.TestCase):
    def test_unnamed_order_duplicates_comments_and_normalized_roundtrip(self):
        source = ('[eVeNtS]\r\n; original order\r\n'
            '01/01/2020 00:04 01/01/2020 00:05 ; late\r\n'
            '[EVENTS]\r\nJan-01-2020 0 01/01/2020 .05\r\n'
            '01/01/2020 00:00 01/01/2020 00:01\r\n')
        model = load(source)
        self.assertEqual(model.to_document().text, source)
        self.assertEqual(model.events.periods, (period(240, 300), period(0, 180), period()))
        self.assertFalse(model.support.opaque_records)
        self.assertEqual({d.code for d in model.validate().diagnostics}, {'events.native_order', 'events.overlap'})
        self.assertEqual(portable(model).events, model.events)
        self.assertEqual(load(model.to_document(normalize=True).text).events, model.events)
        # Replacing/reordering unnamed entries regenerates exactly one ordered block.
        model.update_events(periods=(period(5, 15), period(1, 2), period(5, 15)))
        exported = model.to_document()
        self.assertEqual(len(exported.records('EVENTS')), 3)
        self.assertEqual(load(exported.text).events, model.events)
        self.assertIn('; late', exported.text)

    def test_creation_empty_removal_unit_conversion_and_scenario(self):
        model = network()
        self.assertEqual(model.events, EventSchedule())
        model.update_events(periods=(period(), period(120, 180)))
        before = model.events
        model.convert_units('CMS')
        self.assertEqual(model.events, before)
        patch = ScenarioPatch(operations=(SetFields(target=Ref(collection='swmm:events', key='schedule'),
            changes=(FieldChange(name='periods', value=(period(30, 50),)),)),))
        edited = ScenarioPatch.from_json_document(patch.to_json_document()).apply(model).model
        self.assertEqual(edited.events.periods, (period(30, 50),))
        self.assertEqual(model.events, before)
        edited.update_events(periods=())
        self.assertFalse(edited.to_document().records('EVENTS'))
        self.assertEqual(portable(edited).events, EventSchedule())
        edited.collection('swmm:events').remove('schedule')
        self.assertEqual(edited.events, EventSchedule())

    def test_calendar_duration_and_native_time_coercions(self):
        model = load('[EVENTS]\n02/28/2020 24:00 02/29/2020 24:00\n'
                     '01/01/2020 00:00:01.9 01/01/2020 .0005 extra\n')
        self.assertEqual(model.events.periods[0], RoutingEvent(start=datetime(2020, 2, 29), end=datetime(2020, 3, 1)))
        self.assertEqual(model.events.periods[1], period(1, 1.8))
        self.assertTrue({'events.native_time', 'events.ignored_columns'} <= {d.code for d in model.validate().diagnostics})
        self.assertEqual(load(portable(model).to_document().text).events, model.events)
        self.assertNotIn('00:00:01.8', portable(model).to_document().text)

    def test_invalid_rows_keep_source_and_changes_roll_back(self):
        for row in ('01/01/2020 0 01/01/2020 0', '02/30/2020 0 03/01/2020 0',
                    '01/01/2020 0', '01/01/2020 inf 01/01/2020 1',
                    '12/31/9999 24:00 12/31/9999 25:00'):
            with self.subTest(row=row):
                document = InpDocument.from_text('[EVENTS]\n'+row+'\n', source='events.inp')
                model = Model.from_document(document)
                self.assertFalse(model.validate().is_valid)
                issue = next(d for d in model.validate().errors if d.code == 'events.invalid_input')
                self.assertEqual(issue.span.line, 2)
                self.assertEqual(issue.span.source, 'events.inp')
                with self.assertRaises(ValidationError):
                    model.to_document()
                self.assertEqual(document.text, '[EVENTS]\n'+row+'\n')
        model = load('[EVENTS]\n01/01/2020 0 01/01/2020 1\n')
        before = model.to_document().text
        for invalid in (period(1, 0), replace(period(), start=datetime(2020, 1, 1, tzinfo=timezone.utc))):
            with self.assertRaises(ValidationError):
                model.update_events(periods=(invalid,))
            self.assertEqual(model.to_document().text, before)


if __name__ == '__main__':
    unittest.main()
