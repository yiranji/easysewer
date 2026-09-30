"""GUI display records: unnamed label order, named profile paths and references."""
from dataclasses import dataclass, replace
import unittest

from easysewer.io.inp import InpDocument
from easysewer.io.inp.network import default_schema
from easysewer.model import Model, Point, Ref
from easysewer.model.network import Junction
from easysewer.model.project import MapLabel, MapLabels, ProfilePlot
from easysewer.scenario import ScenarioPatch, SetFields, FieldChange
from easysewer.schema.structured import ModelSchema
from easysewer.validation import ValidationError
from test_options_v2 import network
from test_project_v2 import load
from test_scenario_v2 import portable


def label(**changes):
    return replace(MapLabel(position=Point(x=1.25, y=-2), text='重复 label'), **changes)


class DisplayTests(unittest.TestCase):
    def test_unregistered_display_variants_cannot_silently_drop_extra_fields(self):
        @dataclass(frozen=True, kw_only=True)
        class StyledLabel(MapLabel):
            background: str = 'red'
        @dataclass(frozen=True, kw_only=True)
        class StyledLayer(MapLabels):
            visible: bool = False
        @dataclass(frozen=True, kw_only=True)
        class StyledProfile(ProfilePlot):
            depth_limit: float = 1
        records = (
            ('swmm:labels', MapLabels(entries=(StyledLabel(position=Point(x=0,y=0), text='extra'),))),
            ('swmm:labels', StyledLayer()),
            ('swmm:profiles', StyledProfile(name='extra', links=(Ref(collection='swmm:links',key='P'),))),
        )
        for namespace, value in records:
            with self.subTest(namespace=namespace, kind=type(value).__name__):
                model = network()
                model.collection(namespace).add(value)
                self.assertTrue(any(d.code.endswith('unsupported_variant') for d in model.validate().errors))
                with self.assertRaises(ValidationError):
                    model.to_document()

    def test_unnamed_labels_preserve_duplicates_order_comments_and_edits(self):
        source = network().to_document().text + '[LABELS]\n1.25 -2 "重复 label" ; first\n[LABELS]\n1.25 -2 "重复 label"\n'
        model = load(source)
        self.assertEqual(model.to_document().text, source)
        self.assertEqual(model.labels, MapLabels(entries=(label(), label())))
        self.assertEqual(portable(model).labels, model.labels)
        self.assertEqual(load(model.to_document(normalize=True).text).labels, model.labels)
        model.update_labels(entries=(label(text='second'), label(), label(text='first')))
        self.assertEqual(load(model.to_document().text).labels, model.labels)
        self.assertEqual(len(model.to_document().records('LABELS')), 3)
        self.assertIn('; first', model.to_document().text)
        model.update_labels(entries=())
        self.assertFalse(model.to_document().records('LABELS'))
        self.assertEqual(load(model.to_document().text).labels, MapLabels())
        model.collection('swmm:labels').remove('layer')
        self.assertEqual(model.labels, MapLabels())

    def test_label_optional_font_fields_and_manual_alias_are_explicit(self):
        endings = ('', ' ""', ' "" ""', ' "" "Times New Roman" 0',
                   ' "" Arial -10 2', ' "" Arial 10 YES NO', ' "" Arial +12 1 1 trailing')
        source = '[LABELS]\n' + ''.join('0 0 ""' + tail + '\n' for tail in endings)
        model = load(source)
        rows = model.labels.entries
        self.assertEqual([r.font_name for r in rows[:4]], ['Arial', 'Arial', '', 'Times New Roman'])
        self.assertEqual([r.font_size for r in rows[3:]], [0, -10, 10, 12])
        self.assertFalse(rows[4].bold)
        self.assertTrue(rows[5].bold)
        self.assertFalse(rows[5].italic)
        self.assertTrue(rows[6].bold and rows[6].italic)
        self.assertEqual(model.to_document().text, source)
        codes = {d.code for d in model.validate().diagnostics}
        self.assertTrue({'label.manual_boolean', 'label.gui_boolean', 'label.ignored_columns'} <= codes)
        normalized = model.to_document(normalize=True)
        self.assertNotIn('YES', normalized.text)
        self.assertEqual(load(normalized.text).labels, model.labels)

    def test_label_anchor_rename_delete_and_no_subcatchment_fallback(self):
        model = Model()
        model.nodes.add(Junction(id='J', elevation=0))
        model.update_labels(entries=(label(anchor=Ref(collection='swmm:nodes', key='J')), label()))
        model.nodes.rename('J', 'New')
        self.assertEqual(model.labels.entries[0].anchor.key, 'New')
        self.assertEqual(load(model.to_document().text).labels, model.labels)
        with self.assertRaises(ValidationError):
            model.nodes.remove('New')
        removed = model.nodes.remove('New', cascade=True)
        self.assertEqual({r.collection for r in removed}, {'swmm:nodes', 'swmm:labels'})
        self.assertEqual(model.labels, MapLabels())
        from test_hydrology_v2 import hydrology_model
        base = hydrology_model()
        source = base.to_document().text + '[LABELS]\n0 0 text S\n'
        parsed = Model.from_document(InpDocument.from_text(source))
        self.assertEqual(parsed.labels.entries[0].anchor.collection, 'swmm:nodes')
        self.assertIn('label.anchor_node_only', {d.code for d in parsed.validate().errors})
        # When both domains have the ID, the fixed GUI resolves the node.
        base.nodes.add(Junction(id='S', elevation=0))
        parsed = load(base.to_document().text + '[LABELS]\n0 0 text S\n')
        self.assertEqual(parsed.labels.entries[0].anchor, Ref(collection='swmm:nodes', key='S'))

    def test_profile_arbitrary_chunks_same_names_spaces_and_header_like_names(self):
        source = network().to_document().text + '[PROFILES]\n'
        source += '"North area" ' + ' '.join(['P'] * 12) + ' ; first chunk\n'
        source += '[PROFILES]\n' + ''.join('"north AREA" P\n' for _ in range(13))
        source += '"[Section]" P\n" padded " P\n'
        model = load(source)
        self.assertEqual(len(model.profiles['NORTH AREA'].links), 25)
        self.assertEqual(model.to_document().text, source)
        rebuilt = portable(model).to_document()
        self.assertEqual(len(rebuilt.records('PROFILES')), 7)
        self.assertFalse(rebuilt.find_sections('Section'))
        self.assertEqual(tuple(load(rebuilt.text).profiles.items()), tuple(model.profiles.items()))
        model.profiles.rename('North area', 'New name')
        model.links.rename('P', 'NewLink')
        model.profiles.move('[Section]', before='New name')
        output = model.to_document()
        self.assertIn('; first chunk', output.text)
        self.assertEqual(tuple(load(output.text).profiles.items()), tuple(model.profiles.items()))
        self.assertTrue(all(link.key == 'NewLink' for link in model.profiles['New name'].links))
        with self.assertRaises(ValidationError):
            model.links.remove('NewLink')
        removed = model.links.remove('NewLink', cascade=True)
        self.assertEqual(len([r for r in removed if r.collection == 'swmm:profiles']), 3)
        self.assertFalse(model.to_document().records('PROFILES'))

    def test_disconnected_profile_is_diagnostic_and_links_remain_ordered(self):
        model = network()
        for name in ('X', 'Y'):
            model.nodes.add(Junction(id=name, elevation=0))
        model.links.add(replace(model.links['P'], id='Q', inlet=Ref(collection='swmm:nodes', key='X'),
            outlet=Ref(collection='swmm:nodes', key='Y')))
        model.profiles.add(ProfilePlot(name='Disconnected', links=tuple(Ref(collection='swmm:links', key=k) for k in ('P', 'Q'))))
        report = model.validate()
        self.assertTrue(report.is_valid)
        self.assertIn('profile.disconnected', {d.code for d in report.diagnostics})
        self.assertEqual(load(model.to_document().text).profiles['Disconnected'], model.profiles['Disconnected'])

    def test_scenario_six_units_cache_and_promotion_from_source_only(self):
        from test_cache_reuse_v2 import snapshot, compare
        model = network()
        model.update_labels(entries=(label(),))
        model.profiles.add(ProfilePlot(name='North', links=(Ref(collection='swmm:links', key='P'),)))
        patch = ScenarioPatch(operations=(SetFields(target=Ref(collection='swmm:labels', key='layer'),
            changes=(FieldChange(name='entries', value=(label(text='new'), label())),)),))
        changed = ScenarioPatch.from_json_document(patch.to_json_document()).apply(model).model
        self.assertEqual(changed.labels.entries[0].text, 'new')
        self.assertEqual(model.labels.entries[0].text, label().text)
        changed.profiles.rename('North', '[Changed]')
        changed.profiles.update('[Changed]', links=changed.profiles['[Changed]'].links * 2)
        self.assertEqual(compare(snapshot(model), snapshot(changed), intent='require_match').status, 'matched')
        for unit in ('CFS', 'GPM', 'MGD', 'CMS', 'LPS', 'MLD'):
            converted = changed.copy()
            converted.convert_units(unit)
            self.assertEqual(converted.labels, changed.labels)
            self.assertEqual(tuple(converted.profiles.items()), tuple(changed.profiles.items()))
        schema = default_schema()
        legacy = ModelSchema()
        for descriptor, codec in schema.bindings:
            if descriptor.key not in ('swmm:labels', 'swmm:profiles'):
                legacy.register(descriptor, codec)
        legacy.register_json(*schema.json_types.declarations)
        original = changed.to_document()
        opaque = Model.from_document(original, schema=legacy, strict=True)
        self.assertEqual(opaque.to_document().text, original.text)
        self.assertEqual(len(opaque.support.opaque_records), 3)
        with self.assertRaises(ValidationError):
            opaque.links.rename('P', 'Q')
        promoted = load(opaque.to_document().text)
        promoted.links.rename('P', 'Q')
        self.assertEqual(len(promoted.to_document().records('LABELS')), 2)
        self.assertEqual(len(promoted.to_document().records('PROFILES')), 1)
        self.assertFalse(promoted.support.opaque_records)

    def test_invalid_inputs_keep_source_and_transactions_roll_back(self):
        for section, rows in (
            ('LABELS', ('nan 0 text', '0 0 "bad;quote"', '0 0 text "" Arial 1.5', '0 0 text "" Arial 2147483648', '0 0 text Missing')),
            ('PROFILES', ('"" P', 'North Missing', 'North "bad link"')),
        ):
            for row in rows:
                with self.subTest(section=section, row=row):
                    source = network().to_document().text + '[' + section + ']\n' + row + '\n'
                    model = Model.from_document(InpDocument.from_text(source, source='display.inp'))
                    self.assertEqual(model.document.text, source)
                    self.assertFalse(model.validate().is_valid)
                    with self.assertRaises(ValidationError):
                        model.to_document()
        source = network().to_document().text + '[PROFILES]\nOnlyName\n'
        ignored = load(source)
        self.assertEqual(len(ignored.support.opaque_records), 1)
        self.assertEqual(ignored.to_document().text, source)
        model = network()
        model.update_labels(entries=(label(),))
        before = model.labels
        for value in (label(text='bad;value'), label(font_size=True), label(position=Point(x=float('inf'), y=0)),
                      label(anchor=Ref(collection='swmm:subcatchments', key='S'))):
            with self.assertRaises(ValidationError):
                model.update_labels(entries=(value,))
            self.assertEqual(model.labels, before)
        with self.assertRaises(ValidationError):
            with model.transaction():
                model.profiles.add(ProfilePlot(name='Empty', links=()))
        self.assertFalse(model.profiles)


if __name__ == '__main__':
    unittest.main()
