"""Display input facts, root-specific units and explicit cross-record sources."""
from dataclasses import dataclass, replace
import unittest

from easysewer.io.inp import InpDocument
from easysewer.io.inp.network import default_schema
from easysewer.io.json import JsonDocument
from easysewer.model import FieldFact, FieldSemantics, Model, Point, Ref
from easysewer.model.project import Backdrop, MapLabel, MapLabels, MapSettings
from easysewer.schema import FeatureDescriptor, RegistryError
from easysewer.schema.field_contracts import FieldRule
from easysewer.schema.structured import FieldBinding, FieldCoverage
from test_field_inspection_v2 import FieldSensorCodec, sensor_schema, sensor_semantics
from test_model_extensions import Sensor
from test_scenario_v2 import portable

MAP = Ref(collection='swmm:map', key='settings')
IMAGE = Ref(collection='swmm:backdrop', key='image')
LABELS = Ref(collection='swmm:labels', key='layer')
SOURCE = ('[JUNCTIONS]\nJ 0\n[MAP]\nUNITS FEET\nDIMENSIONS 0 0 100 200\n'
          '[BACKDROP]\nUNITS METERS\nFILE "image one.png"\nDIMENSIONS 10 20 -3 -4\n'
          'OFFSET 1 2\nSCALING 3 4\n[MAP]\nUNITS DEGREES\n'
          '[LABELS]\n1 2 "label" J\n3 4 "explicit" "" Arial 10 0 0\n')


def load(source=SOURCE):
    return Model.from_document(InpDocument.from_text(source, source='C:/fixtures/map.inp'))


class DisplayFieldTests(unittest.TestCase):
    def test_cross_record_unit_order_keeps_exact_sources_and_contributions(self):
        model = load()
        field = model.inspect_field(MAP, 'units_precedence')
        self.assertEqual(field.value, 'MAP')
        self.assertEqual(field.provenance.status, 'derived')
        rows = field.provenance.declarations
        self.assertEqual([r.tokens[0].raw for r in rows], ['FEET', 'METERS', 'DEGREES'])
        self.assertEqual([r.contributes for r in rows], [False, True, True])
        self.assertEqual(rows[1].source_owners, (IMAGE.canonical,))
        self.assertEqual(rows[2].source_owners, (MAP.canonical,))
        self.assertEqual(field.semantics.default.status, 'not_applicable')
        before = field.provenance
        model.update_map(units_precedence='BACKDROP')
        changed = model.inspect_field(MAP, 'units_precedence')
        self.assertTrue(changed.changed)
        self.assertEqual(changed.provenance, before)
        self.assertEqual(model.inspect_field(MAP, 'units').semantics.effective.value, 'METERS')
        rebuilt = Model.from_json_document(model.to_json_document(), strict=True)
        self.assertEqual(rebuilt.inspect_field(MAP, 'units_precedence'), changed)
        self.assertEqual(len(rebuilt.to_document().records('MAP')), 2)

    def test_extent_components_and_label_coordinates_use_shared_explicit_map_units(self):
        model = load()
        for owner, path in ((MAP, ('extent', 'lower_left', 'x')),
                            (IMAGE, ('extent', 'upper_right', 'y')),
                            (LABELS, ('entries', 0, 'position', 'x'))):
            info = model.inspect_field(owner, path)
            self.assertEqual(info.semantics.unit.value, 'degree')
            source = model.field_provenance(owner, path)
            self.assertEqual(source.status, 'explicit')
            self.assertEqual(len(source.declarations[0].tokens), 1)
        model.update_map(units_precedence='BACKDROP')
        for flow in ('CFS', 'GPM', 'MGD', 'CMS', 'LPS', 'MLD'):
            model.convert_units(flow)
            self.assertEqual(model.inspect_field(MAP, ('extent', 'lower_left', 'x')).semantics.unit.value, 'm')
            self.assertEqual(model.inspect_field(LABELS, ('entries', 0, 'font_size')).semantics.unit.value, 'pt')
        # Network points now have an independent, explicitly scoped contract.
        model.nodes.update('J', position=Point(x=8, y=9))
        self.assertEqual(model.inspect_field(Ref(collection='swmm:nodes', key='J'), ('position', 'x')).semantics.unit.value, 'm')

    def test_file_clear_omission_and_nested_lexical_context_are_distinct(self):
        model = load()
        path = model.field_provenance(IMAGE, ('file', 'path'))
        self.assertEqual(path.declarations[0].tokens[0].raw, '"image one.png"')
        self.assertEqual(model.field_provenance(IMAGE, ('file', 'base_directory')).status, 'derived')
        cleared = load(SOURCE.replace('FILE "image one.png"', 'FILE "image one.png"\nFILE ""'))
        self.assertEqual(cleared.field_provenance(IMAGE, 'file').status, 'explicit')
        self.assertEqual([v.contributes for v in cleared.field_provenance(IMAGE, 'file').declarations], [False, True])
        self.assertEqual(cleared.field_provenance(IMAGE, ('file', 'path')).status, 'absent_path')
        info = cleared.inspect_field(IMAGE, 'file')
        self.assertEqual(info.semantics.effective, FieldFact(status='known', value=None, reason='Explicit FILE empty-string clearing command'))
        omitted = load('[BACKDROP]\nUNITS FEET\n').inspect_field(IMAGE, 'file')
        self.assertEqual(omitted.provenance.status, 'omitted')
        self.assertEqual(omitted.semantics.effective.status, 'unknown')

    def test_absent_gui_preferences_and_deprecated_values_are_not_invented_defaults(self):
        model = load('[MAP]\nDIMENSIONS 0 0 3 4\n[BACKDROP]\nOFFSET 1 2\nSCALING 3 4\n')
        units = model.inspect_field(MAP, 'units')
        self.assertEqual(units.semantics.default.status, 'unknown')
        self.assertEqual(units.semantics.effective.status, 'unknown')
        self.assertEqual(model.inspect_field(MAP, ('extent', 'lower_left', 'x')).semantics.unit.status, 'unknown')
        model.update_map(units='NONE')
        self.assertEqual(model.inspect_field(MAP, ('extent', 'lower_left', 'x')).semantics.unit.status, 'unknown')
        for field in ('legacy_offset', 'legacy_scaling'):
            info = model.inspect_field(IMAGE, (field, 'x'))
            self.assertEqual(info.provenance.status, 'explicit')
            self.assertEqual(info.semantics.unit.status, 'unknown')
            self.assertEqual(info.semantics.effective.status, 'not_applicable')

    def test_label_omitted_defaults_and_explicit_equal_values_keep_separate_sources(self):
        model = load()
        for name, value in (('font_name', 'Arial'), ('font_size', 10), ('bold', False), ('italic', False)):
            for index, status in ((0, 'omitted'), (1, 'explicit')):
                path = ('entries', index, name)
                info = model.inspect_field(LABELS, path)
                self.assertEqual(info.value, value)
                self.assertEqual(info.semantics.default.value, value)
                self.assertEqual(info.provenance.status, 'untracked_path')
                self.assertEqual(model.field_provenance(LABELS, path).status, status)
        self.assertEqual(model.field_provenance(LABELS, ('entries', 1, 'anchor')).declarations[0].role, 'marker')
        anchor = model.field_provenance(LABELS, ('entries', 0, 'anchor', 'key'))
        model.nodes.rename('J', 'Renamed')
        self.assertEqual(model.inspect_field(LABELS, ('entries', 0, 'anchor', 'key')).value, 'Renamed')
        self.assertEqual(model.field_provenance(LABELS, ('entries', 0, 'anchor', 'key')), anchor)

    def test_invalid_known_map_line_blocks_a_false_complete_source_claim(self):
        model = load(SOURCE+'[BACKDROP]\nUNITS bad\n')
        self.assertEqual(model.field_provenance(IMAGE, 'units').status, 'unknown')
        self.assertEqual(model.field_provenance(MAP, 'units_precedence').status, 'unknown')
        self.assertEqual(model.field_provenance(MAP, ('extent', 'lower_left', 'x')).status, 'explicit')
        labels = load('[LABELS]\n1 2 "ok"\nbad 2 "not decoded"\n')
        self.assertEqual(labels.field_provenance(LABELS, 'entries').status, 'unknown')
        self.assertEqual(labels.field_provenance(LABELS, ('entries', 0, 'text')).status, 'explicit')

    def test_invalid_drafts_and_missing_anchors_return_diagnostics(self):
        model = load()
        model.collection('swmm:map').update('settings', units='invalid')
        info = model.inspect_field(MAP, ('extent', 'lower_left', 'x'))
        self.assertEqual(info.semantics.unit.status, 'invalid')
        self.assertTrue(info.semantics.diagnostics)
        labels = load('[LABELS]\n1 2 "missing" MissingNode\n')
        self.assertEqual(labels.inspect_field(LABELS, ('entries', 0, 'anchor')).semantics.effective.status, 'invalid')
        labels.collection('swmm:labels').update('layer', entries=False)
        self.assertEqual(labels.inspect_field(LABELS, 'entries').semantics.effective.status, 'invalid')
        self.assertEqual(labels.inspect_field(LABELS, 'entries').semantics.default.value, ())

    def test_portable_recreate_rollback_and_sequence_reorder_preserve_scope(self):
        model = load()
        self.assertEqual(portable(model).inspect_field(MAP, 'units').provenance.status, 'untracked')
        before = model.inspect_field(MAP, 'units')
        with self.assertRaises(RuntimeError):
            with model.transaction():
                model.update_map(units='METERS')
                raise RuntimeError('rollback')
        self.assertEqual(model.inspect_field(MAP, 'units'), before)
        old = model.collection('swmm:map')['settings']
        model.collection('swmm:map').remove('settings'); model.collection('swmm:map').add(old)
        self.assertEqual(model.inspect_field(MAP, 'units').provenance.status, 'created')
        original = model.field_provenance(LABELS, ('entries', 0, 'text'))
        model.update_labels(entries=tuple(reversed(model.labels.entries)))
        self.assertEqual(model.field_provenance(LABELS, ('entries', 0, 'text')), original)
        self.assertEqual(model.inspect_field(LABELS, ('entries', 0, 'text')).value, 'explicit')

    def test_scoped_rules_override_only_their_exact_root_and_snapshot(self):
        schema = default_schema()
        root = Model().profile
        self.assertIsNone(schema.field_rule(Point, 'x', root))
        self.assertIsNotNone(schema.field_rule(Point, 'x', root, root_type=MapSettings))
        self.assertIsNotNone(schema.snapshot().field_rule(Point, 'x', root, root_type=MapLabels))
        @dataclass(frozen=True)
        class OtherRoot:
            point: Point
        class OtherCodec(FieldSensorCodec):
            field_rules = (FieldRule(value_type=Point, field='x', root_type=OtherRoot, resolve=sensor_semantics),)
        schema.register(FeatureDescriptor(key='test:other', sections={'SENSORS'}), OtherCodec())
        self.assertIsNotNone(schema.field_rule(Point, 'x', root, root_type=OtherRoot))
        class Duplicate(OtherCodec): pass
        with self.assertRaises(RegistryError): schema.register(FeatureDescriptor(key='test:duplicate', sections={'SENSORS'}), Duplicate())

    def test_independent_derived_cross_record_binding_and_foreign_line_rejection(self):
        class CopiedThreshold(FieldSensorCodec):
            role = 'derived'
            source_line = 4
            def decode(self, document, profile):
                result = super().decode(document, profile)
                records = list(result.value.records)
                records[1] = replace(records[1], value=replace(records[1].value, threshold=records[0].value.threshold))
                original = result.value.field_bindings[0]
                second = replace(result.value.field_bindings[1], line=self.source_line, role=self.role)
                return replace(result, value=replace(result.value, records=tuple(records), field_bindings=(original, second)))
        source = InpDocument.from_text('[JUNCTIONS]\nJ 0\n[SENSORS]\nA J 3\nB J 8\n')
        schema = sensor_schema(CopiedThreshold())
        model = Model.from_document(source, schema=schema, strict=True)
        owner = Ref(collection='test:sensors', key='B')
        info = model.inspect_field(owner, 'threshold')
        self.assertEqual(info.value, 3)
        self.assertEqual(info.provenance.status, 'derived')
        self.assertEqual(info.provenance.declarations[0].source_owners, (Ref(collection='test:sensors', key='A'),))
        model.collection('test:sensors').rename('B', 'NewB')
        rebuilt = Model.from_json_document(model.to_json_document(), schema=schema, strict=True)
        self.assertEqual(rebuilt.field_provenance(Ref(collection='test:sensors', key='NewB'), 'threshold').declarations,
                         info.provenance.declarations)
        for role, source_line in (('value', 4), ('derived', 2)):
            broken = CopiedThreshold(); broken.role = role; broken.source_line = source_line
            with self.subTest(role=role, line=source_line), self.assertRaises(RegistryError):
                Model.from_document(source, schema=sensor_schema(broken))


if __name__ == '__main__':
    unittest.main()
