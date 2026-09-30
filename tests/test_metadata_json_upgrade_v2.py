"""Actual previous development packages' JSON must retain formerly opaque fields."""
from dataclasses import dataclass
from pathlib import Path
import unittest

from easysewer.io.json import JsonDocument
from easysewer.io.json.types import JsonField, JsonType, JsonTypes
from easysewer.model import Model, Point
from easysewer.validation import ValidationError
from test_scenario_v2 import portable

FIXTURES = Path(__file__).parent / 'fixtures' / 'metadata_legacy'


def document(name):
    return JsonDocument.read(FIXTURES / (name + '.json'))


def wire(data, tag):
    for collection in data['collections']:
        for record in collection['records']:
            if record['value'].get('type') == tag:
                return record['value']
    raise AssertionError(tag)


class MetadataJsonUpgradeTests(unittest.TestCase):
    def test_actual_old_storage_json_recovers_polygon_and_materializes_portable_field(self):
        original = document('storage')
        self.assertNotIn('polygon', wire(original.data, 'swmm:network.storage'))
        model = Model.from_json_document(original, strict=True)
        expected = (Point(x=-1, y=2), Point(x=3, y=4), Point(x=-1, y=2))
        self.assertEqual(model.nodes['J'].polygon, expected)
        self.assertIn('json.source_field_promoted', {d.code for d in model.validate().diagnostics})
        self.assertIn('polygon', wire(model.to_json_document().data, 'swmm:network.storage'))
        self.assertEqual(portable(model).nodes['J'].polygon, expected)
        model.nodes.rename('J', 'Tank')
        rebuilt = Model.from_document(model.to_document(), strict=True)
        self.assertEqual(rebuilt.nodes['Tank'].polygon, expected)
        self.assertEqual(len(model.to_document().records('POLYGONS')), 7)
        again = Model.from_json_document(model.to_json_document(), strict=True)
        self.assertFalse(any(d.code == 'json.source_field_promoted' for d in again.validate().diagnostics))

    def test_actual_old_backdrop_json_retains_legacy_fields_and_cross_section_units(self):
        model = Model.from_json_document(document('backdrop'), strict=True)
        self.assertEqual(model.effective_map.units, 'METERS')
        self.assertEqual(model.map.units_precedence, 'BACKDROP')
        self.assertEqual(model.backdrop.legacy_offset, Point(x=1, y=2))
        self.assertEqual(model.backdrop.legacy_scaling, Point(x=3, y=4))
        self.assertEqual(portable(model).backdrop, model.backdrop)
        rebuilt = Model.from_document(model.to_document(), strict=True)
        self.assertEqual(rebuilt.effective_map, model.effective_map)
        self.assertEqual(rebuilt.backdrop, model.backdrop)

    def test_explicit_empty_fields_win_and_source_free_old_json_uses_defaults(self):
        for source_present in (True, False):
            data = document('storage').data
            if source_present:
                wire(data, 'swmm:network.storage')['polygon'] = []
            else:
                data.pop('source')
            model = Model.from_json_document(JsonDocument.from_data(data), strict=True)
            self.assertFalse(model.nodes['J'].polygon)
            self.assertFalse(any(d.code == 'json.source_field_promoted' for d in model.validate().diagnostics))
        data = document('backdrop').data
        wire(data, 'swmm:map.settings')['units_precedence'] = 'MAP'
        wire(data, 'swmm:map.backdrop').update(units=None, legacy_offset=None, legacy_scaling=None, clear_file=False)
        model = Model.from_json_document(JsonDocument.from_data(data), strict=True)
        self.assertEqual(model.effective_map.units, 'FEET')
        self.assertIsNone(model.backdrop.legacy_offset)
        self.assertIsNone(portable(model).backdrop.units)

    def test_new_source_clear_does_not_silently_overwrite_existing_json_file(self):
        with self.assertRaises(ValidationError):
            Model.from_json_document(document('clear-conflict'), strict=True)
        model = Model.from_json_document(document('clear-conflict'))
        self.assertEqual(model.backdrop.file.path, 'old.png')
        self.assertTrue(model.backdrop.clear_file)
        self.assertIn('map.backdrop_file_conflict', {d.code for d in model.validate().errors})
        model.update_backdrop(file=None)
        self.assertTrue(portable(model).backdrop.clear_file)
        self.assertIsNone(Model.from_document(model.to_document(), strict=True).backdrop.file)

    def test_source_defaults_are_explicit_extensible_and_preserve_wire_names(self):
        @dataclass(frozen=True, kw_only=True)
        class Value:
            amount: int = 0
        field = JsonField(name='v', attribute='amount', shape=('integer',))
        declaration = JsonType(key='test:source_default', value_type=Value, fields=(field,),
            defaults=(('v', 0),), source_defaults=('v',))
        types = JsonTypes((declaration,))
        promoted, names = types.promote_source_defaults(Value(), {'type': declaration.key}, Value(amount=3))
        self.assertEqual((promoted.amount, names), (3, ('v',)))
        self.assertEqual(types.promote_source_defaults(Value(), {'v': 0}, Value(amount=3)), (Value(), ()))
        self.assertEqual(types.promote_source_defaults(Value(), {}, object()), (Value(), ()))
        with self.assertRaises(ValueError):
            JsonType(key='test:bad', value_type=Value, fields=(field,), source_defaults=('v',))
        with self.assertRaises(ValueError):
            JsonType(key='test:bad', value_type=Value, fields=(field,), defaults=(('v', 0),), source_defaults=('v', 'v'))


if __name__ == '__main__':
    unittest.main()
