"""F acceptance: joint metadata edits across INP, JSON, scenarios and units."""
from dataclasses import replace
from datetime import date, time
import unittest

from easysewer.io.inp import InpDocument
from easysewer.io.inp.network import default_schema
from easysewer.model import Model, Point
from easysewer.scenario import MoveRecord, RenameRecord, RemoveRecord, ScenarioPatch
from easysewer.schema.structured import ModelSchema
from easysewer.validation import ValidationError
from test_events_v2 import period
from test_map_geometry_v2 import geometry_model, geometry
from test_scenario_v2 import fields, portable, target


METADATA = '''[EVENTS]
01/01/2020 00:06 01/01/2020 00:12 ; later first
[EVENTS]
01/01/2020 00:01 01/01/2020 00:04
01/01/2020 00:01 01/01/2020 00:04
[TAGS]
Gage R rain
Subcatch S catchment
Node J storage
Link P pipe
[LABELS]
1 2 "same label" J
[LABELS]
1 2 "same label" J
3 4 free
[PROFILES]
"Main profile" P
[PROFILES]
"Main profile" P
[MAP]
DIMENSIONS -10 -10 100 100
UNITS FEET
[BACKDROP]
FILE ""
DIMENSIONS 10 10 0 0
UNITS METERS
OFFSET -1 2
SCALING 1 -1
'''


def f_model():
    base = geometry_model()
    base.update_options(start_date=date(2020, 1, 1), end_date=date(2020, 1, 1),
                        report_start_date=date(2020, 1, 1), start_time=time(0))
    base.set_annotation('user:f-domain', {'id': 'J', 'note': '独立元数据', 'scale': 2.5})
    return Model.from_document(InpDocument.from_text(
        base.to_document().text + METADATA,
        source='f-original.inp'), strict=True)


def state(model):
    """Only F-owned values; hydraulic numbers may change with flow units."""
    return (model.events, tuple(model.tags.items()), model.labels,
            tuple(model.profiles.items()), model.map, model.backdrop,
            model.effective_map, geometry(model), tuple(model.metadata.items()))


def edit_patch(model):
    return ScenarioPatch(operations=(
        *(RenameRecord(target=target(c, old), new_id=new) for c, old, new in (
            ('nodes', 'J', 'Tank'), ('links', 'P', 'Pipe'),
            ('raingages', 'R', 'Gauge'), ('subcatchments', 'S', 'Area'))),
        MoveRecord(target=target('tags', ('swmm:links', 'Pipe')),
                   before=('swmm:raingages', 'Gauge')),
        fields('tags', ('swmm:nodes', 'Tank'), text='edited storage'),
        fields('labels', 'layer', entries=(model.labels.entries[2],
            replace(model.labels.entries[0], anchor=target('nodes', 'Tank')),
            replace(model.labels.entries[0], anchor=target('nodes', 'Tank')))),
        fields('events', 'schedule', periods=(period(360, 720), period(90, 270), period(90, 270))),
        fields('map', 'settings', units_precedence='MAP'),
        fields('links', 'Pipe', vertices=(Point(x=9, y=8), Point(x=9, y=8))),
        RenameRecord(target=target('profiles', 'Main profile'), new_id='Edited profile'),
    ))


def edited_model():
    base = f_model()
    patch = edit_patch(base)
    return ScenarioPatch.from_json_document(patch.to_json_document()).apply(base).model


class FDomainTests(unittest.TestCase):
    def test_joint_scenario_roundtrips_units_references_and_order(self):
        base = f_model()
        original = base.to_json_document().data
        changed = ScenarioPatch.from_json_document(edit_patch(base).to_json_document()).apply(base).model
        self.assertEqual(base.to_json_document().data, original)
        self.assertEqual(changed.events.periods, (period(360, 720), period(90, 270), period(90, 270)))
        self.assertNotEqual(changed.events, base.events)
        self.assertTrue(all(row.start.date() == changed.options.start_date for row in changed.events.periods))
        self.assertEqual([v.target for v in changed.tags.values()], [target(c, k) for c, k in
            (('links', 'Pipe'), ('raingages', 'Gauge'), ('subcatchments', 'Area'), ('nodes', 'Tank'))])
        self.assertEqual(changed.labels.entries[1:], (changed.labels.entries[1],) * 2)
        self.assertEqual(changed.labels.entries[1].anchor, target('nodes', 'Tank'))
        self.assertEqual(changed.profiles['Edited profile'].links, (target('links', 'Pipe'),) * 2)
        self.assertEqual(changed.effective_map.units, 'FEET')
        self.assertEqual(changed.metadata['user:f-domain'].value['id'], 'J')
        self.assertEqual(changed.nodes['Tank'].polygon, base.nodes['J'].polygon)
        for unit in (None, 'CFS', 'GPM', 'MGD', 'CMS', 'LPS', 'MLD'):
            converted = changed.copy()
            if unit is not None:
                converted.convert_units(unit)
            self.assertEqual(state(converted), state(changed))
            for restored in (Model.from_document(converted.to_document(), strict=True),
                Model.from_document(converted.to_document(normalize=True), strict=True),
                Model.from_json_document(converted.to_json_document(), strict=True), portable(converted)):
                self.assertEqual(state(restored), state(changed))
        self.assertEqual(changed.provenance(target('nodes', 'Tank')).original, target('nodes', 'J'))
        self.assertEqual(changed.provenance(target('tags', ('swmm:nodes', 'Tank'))).original,
                         target('tags', ('SWMM:NODES', 'J')))

    def test_failed_joint_patch_leaves_source_and_references_unchanged(self):
        model = f_model()
        original = model.to_json_document().data
        patch = ScenarioPatch(operations=edit_patch(model).operations +
            (RemoveRecord(target=target('nodes', 'Tank')),))
        with self.assertRaises(ValidationError):
            patch.apply(model)
        self.assertEqual(model.to_json_document().data, original)
        # Cascades remove whole referencing records, including the label layer.
        changed = edited_model()
        events = changed.events
        removed = changed.nodes.remove('Tank', cascade=True)
        self.assertIn(target('labels', 'layer'), removed)
        self.assertFalse(changed.labels.entries)
        self.assertFalse(changed.profiles)
        self.assertEqual(tuple(changed.tags), (('swmm:raingages', 'Gauge'),))
        self.assertEqual(changed.events, events)
        self.assertEqual(state(portable(changed)), state(changed))

    def test_independent_codecs_promote_joint_source_once(self):
        full = default_schema()
        omitted = {'swmm:events', 'swmm:tags', 'swmm:labels', 'swmm:profiles', 'swmm:map'}
        schema = ModelSchema()
        for descriptor, codec in full.bindings:
            if descriptor.key not in omitted:
                schema.register(descriptor, codec)
        schema.register_json(*full.json_types.declarations)
        document = f_model().to_document()
        opaque = Model.from_document(document, schema=schema, strict=True)
        self.assertTrue(opaque.support.opaque_records)
        self.assertEqual(opaque.to_document().text, document.text)
        with self.assertRaises(ValidationError):
            opaque.nodes.rename('J', 'Tank')
        for descriptor, codec in full.bindings:
            if descriptor.key in omitted:
                schema.register(descriptor, codec)
        promoted = Model.from_document(document, schema=schema, strict=True)
        self.assertFalse(promoted.support.opaque_records)
        changed = ScenarioPatch.from_json_document(edit_patch(promoted).to_json_document()).apply(promoted).model
        for section, count in (('EVENTS', 3), ('TAGS', 4), ('LABELS', 3), ('PROFILES', 1)):
            self.assertEqual(len(changed.to_document().records(section)), count)
        self.assertEqual(state(changed), state(edited_model()))
        self.assertEqual(opaque.to_document().text, document.text)


if __name__ == '__main__':
    unittest.main()
