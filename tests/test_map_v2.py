"""Map/background GUI directives, source ordering and legacy preservation."""
from dataclasses import dataclass, replace
from pathlib import Path
import tempfile
import unittest

from easysewer.io.inp import InpDocument
from easysewer.io.json import JsonDocument
from easysewer.model import Model, Point, Ref, FileReference
from easysewer.model.project import MapExtent, MapSettings, Backdrop
from easysewer.scenario import ScenarioPatch, SetFields, FieldChange
from easysewer.validation import ValidationError
from test_options_v2 import network
from test_project_v2 import load
from test_scenario_v2 import portable


class MapTests(unittest.TestCase):
    def assert_roundtrip(self, model):
        for other in (load(model.to_document().text), load(model.to_document(normalize=True).text),
                      portable(model), load(portable(model).to_document().text)):
            self.assertEqual(other.map, model.map)
            self.assertEqual(other.backdrop, model.backdrop)
            self.assertEqual(other.effective_map, model.effective_map)

    def test_cross_section_units_last_assignment_and_priority_only_edit(self):
        cases = (
            ('[MAP]\nUNITS FEET\n[BACKDROP]\nUNITS METERS\n', 'METERS', 'BACKDROP'),
            ('[BACKDROP]\nUNITS METERS\n[MAP]\nUNITS FEET\n', 'FEET', 'MAP'),
            ('[MAP]\nUNITS FEET\n[BACKDROP]\nUNITS METERS\n[MAP]\nUNITS DEGREES\n', 'DEGREES', 'MAP'),
            ('[BACKDROP]\nUNITS FEET\n[MAP]\nUNITS DEGREES\n[BACKDROP]\nUNITS NONE\n', 'NONE', 'BACKDROP'),
        )
        for source, units, origin in cases:
            with self.subTest(source=source):
                model = load(source)
                self.assertEqual(model.to_document().text, source)
                self.assertEqual((model.effective_map.units, model.effective_map.units_source), (units, origin))
                self.assertEqual(model.map.units_precedence, origin)
                self.assert_roundtrip(model)
                flipped = 'MAP' if origin == 'BACKDROP' else 'BACKDROP'
                model.update_map(units_precedence=flipped)
                self.assertEqual(model.effective_map.units_source, flipped)
                self.assert_roundtrip(model)
        empty = Model()
        self.assertIsNone(empty.effective_map.units)
        empty.update_backdrop(units='NONE')
        self.assertEqual(empty.effective_map.units_source, 'BACKDROP')
        self.assertEqual(empty.map, MapSettings())
        self.assert_roundtrip(empty)

    def test_keyword_unit_prefixes_extra_columns_and_legacy_fields(self):
        source = ('[MAP]\nDime 0 1 5 8 unused\nUnit meter-more tail\n'
                  '[BACKDROP]\nDime 8 7 2 1\nOffs -1.25 0 trailing\nScal 0 -2\nUnit deg\nFILE picture.png ignored\n')
        model = load(source)
        self.assertEqual(model.to_document().text, source)
        self.assertEqual(model.map.units, 'METERS')
        self.assertEqual(model.effective_map.units, 'DEGREES')
        self.assertEqual(model.backdrop.legacy_offset, Point(x=-1.25, y=0))
        self.assertEqual(model.backdrop.legacy_scaling, Point(x=0, y=-2))
        self.assertEqual(model.backdrop.file.path, 'picture.png')
        self.assertEqual(model.backdrop.extent, MapExtent(lower_left=Point(x=8,y=7), upper_right=Point(x=2,y=1)))
        codes = {d.code for d in model.validate().diagnostics}
        self.assertTrue({'map.gui_keyword', 'map.gui_unit', 'map.ignored_columns', 'map.legacy_directive'} <= codes)
        self.assert_roundtrip(model)
        for word, unit in (('f', 'FEET'), ('meters', 'METERS'), ('D', 'DEGREES'), ('none', 'NONE')):
            self.assertEqual(load('[MAP]\nUNIT ' + word + '\n').map.units, unit)

    def test_background_bounds_allow_reversed_or_degenerate_map_bounds_do_not(self):
        for coordinates in ('4 3 1 0', '0 0 0 0', '0 0 1 -1'):
            background = load('[BACKDROP]\nDIMENSIONS ' + coordinates + '\n')
            self.assert_roundtrip(background)
            map_model = Model.from_document(InpDocument.from_text('[MAP]\nDIMENSIONS ' + coordinates + '\n'))
            self.assertFalse(map_model.validate().is_valid)
        model = Model()
        with self.assertRaises(ValidationError):
            model.update_map(extent=MapExtent(lower_left=Point(x=1,y=1), upper_right=Point(x=0,y=0)))
        model.update_backdrop(extent=MapExtent(lower_left=Point(x=1,y=1), upper_right=Point(x=0,y=0)))
        self.assert_roundtrip(model)

    def test_explicit_empty_file_is_distinct_from_omission_and_rebases_paths(self):
        source = '[BACKDROP]\nFILE old.png ; original\nFILE ""\n'
        model = load(source)
        self.assertTrue(model.backdrop.clear_file)
        self.assertIsNone(model.backdrop.file)
        self.assertFalse(model.file_uses())
        self.assertEqual(model.to_document().text, source)
        self.assert_roundtrip(model)
        self.assertEqual(model.to_document(normalize=True).records('BACKDROP')[0].values, ('FILE', ''))
        model.update_backdrop(clear_file=False)
        self.assertFalse(model.to_document().records('BACKDROP'))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'old' / 'model.inp'
            path.parent.mkdir()
            path.write_text('[BACKDROP]\nFILE "图片 path.png"\n', encoding='utf-8')
            model = Model.from_inp(path, strict=True)
            model.to_inp(root / 'new' / 'model.inp')
            reloaded = Model.from_inp(root / 'new' / 'model.inp', strict=True)
            self.assertEqual(reloaded.backdrop.file.resolve(), path.parent / '图片 path.png')
            with self.assertRaises(ValidationError):
                model.update_backdrop(clear_file=True)
            model.update_backdrop(file=None, clear_file=True)
            self.assertFalse(model.file_uses())
            self.assert_roundtrip(model)

    def test_scenario_and_all_flow_units_preserve_maps_and_cache_conditions(self):
        from test_cache_reuse_v2 import snapshot, compare
        model = network()
        model.update_map(units='FEET')
        model.update_backdrop(units='METERS', legacy_offset=Point(x=5,y=6), legacy_scaling=Point(x=2,y=-3), clear_file=True)
        patch = ScenarioPatch(operations=(SetFields(target=Ref(collection='swmm:map',key='settings'),
            changes=(FieldChange(name='units_precedence',value='BACKDROP'),)),))
        changed = ScenarioPatch.from_json_document(patch.to_json_document()).apply(model).model
        self.assertEqual(changed.effective_map.units, 'METERS')
        self.assertEqual(model.effective_map.units, 'FEET')
        self.assertEqual(compare(snapshot(model), snapshot(changed), intent='require_match').status, 'matched')
        for unit in ('CFS', 'GPM', 'MGD', 'CMS', 'LPS', 'MLD'):
            converted = changed.copy()
            converted.convert_units(unit)
            self.assertEqual(converted.map, changed.map)
            self.assertEqual(converted.backdrop, changed.backdrop)
            self.assertEqual(converted.effective_map, changed.effective_map)

    def test_ignored_and_unknown_lines_remain_source_owned_and_invalid_changes_roll_back(self):
        source = '[MAP]\nUNITS\nFUTURE state\n[BACKDROP]\nFILE\n'
        model = load(source)
        self.assertEqual(len(model.support.opaque_records), 3)
        self.assertEqual(model.to_document().text, source)
        with self.assertRaises(ValidationError):
            model.convert_units('CMS')
        for section, row in (('MAP','UNIT unknown'),('MAP','DIME 0 0 nan 1'),('BACKDROP','OFFS 1'),
                             ('BACKDROP','SCAL inf 1'),('BACKDROP','FILE "bad;quote"')):
            bad = Model.from_document(InpDocument.from_text('[' + section + ']\n' + row + '\n', source='map.inp'))
            self.assertFalse(bad.validate().is_valid)
            self.assertTrue(any(d.span and d.span.source == 'map.inp' for d in bad.validate().errors))
        valid = load('[MAP]\nUNITS FEET\n[BACKDROP]\nUNITS METERS\n')
        before = valid.to_document().text
        with self.assertRaises(ValidationError):
            with valid.transaction():
                valid.update_backdrop(units=None)
        self.assertEqual(valid.to_document().text, before)

    def test_new_fields_default_when_reading_old_json_and_variants_are_rejected(self):
        model = Model()
        model.update_map(units='FEET')
        model.update_backdrop(file=FileReference(path='old.png'))
        data = portable(model).to_json_document().data
        def strip(value):
            if isinstance(value, dict):
                if value.get('type') == 'swmm:map.settings':
                    value.pop('units_precedence', None)
                if value.get('type') == 'swmm:map.backdrop':
                    for field in ('units','clear_file','legacy_offset','legacy_scaling'):
                        value.pop(field, None)
                for child in value.values(): strip(child)
            elif isinstance(value, list):
                for child in value: strip(child)
        strip(data)
        restored = Model.from_json_document(JsonDocument.from_data(data), strict=True)
        self.assertEqual(restored.map, model.map)
        self.assertEqual(restored.backdrop, model.backdrop)
        @dataclass(frozen=True, kw_only=True)
        class FutureMap(MapSettings):
            projection: str = 'future'
        other = Model()
        other.collection('swmm:map').add(FutureMap())
        self.assertIn('map.unsupported_variant', {d.code for d in other.validate().errors})
        with self.assertRaises(ValidationError):
            other.to_document()


if __name__ == '__main__':
    unittest.main()
