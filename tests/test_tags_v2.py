"""GUI tags are referenced annotations, not hydraulic inputs."""
from dataclasses import replace
import unittest

from easysewer.io.inp import InpDocument
from easysewer.model import Model, Ref
from easysewer.model.network import Junction
from easysewer.model.project import ObjectTag, TAG_TARGETS
from easysewer.scenario import ScenarioPatch, SetFields, FieldChange
from easysewer.validation import ValidationError
from test_hydrology_v2 import hydrology_model
from test_options_v2 import network
from test_project_v2 import load
from test_scenario_v2 import portable


class TagsTests(unittest.TestCase):
    def test_four_classes_can_share_an_id_and_rename_independently(self):
        model = hydrology_model()
        # Avoid an unrelated ambiguous node/subcatchment outlet in the fixture.
        model.subcatchments.update('S', outlet=Ref(collection='swmm:nodes', key='O'))
        for namespace, old in zip(TAG_TARGETS, ('R', 'S', 'J', 'P')):
            model.collection(namespace).rename(old, 'Shared')
        for namespace in TAG_TARGETS:
            model.tags.add(ObjectTag(target=Ref(collection=namespace, key='Shared'), text=namespace))
        self.assertEqual(len(model.tags), 4)
        source = model.to_document().text
        restored = load(source)
        self.assertEqual(tuple(restored.tags.items()), tuple(model.tags.items()))
        restored.nodes.rename('shared', 'Changed')
        self.assertEqual(restored.tags[('swmm:nodes', 'changed')].target.key, 'Changed')
        self.assertEqual(restored.tags[('swmm:links', 'shared')].target.key, 'Shared')
        self.assertEqual(tuple(load(restored.to_document().text).tags.items()), tuple(restored.tags.items()))
        self.assertEqual(tuple(portable(restored).tags.items()), tuple(restored.tags.items()))
        restored.tags.move(('swmm:raingages', 'Shared'))
        self.assertEqual(tuple(load(restored.to_document().text).tags), tuple(restored.tags))

    def test_last_assignment_quotes_empty_values_and_source_retention(self):
        source = network().to_document().text + '[tAgS]\nNode J first ; old\n[TAGS]\nnodes j "雨水 network" ignored\nLink P ""\n'
        model = load(source)
        self.assertEqual(model.to_document().text, source)
        self.assertEqual(model.tags[('swmm:nodes', 'J')].text, '雨水 network')
        self.assertEqual(model.tags[('swmm:links', 'P')].text, '')
        self.assertTrue({'tag.repeated_assignment', 'tag.gui_keyword', 'tag.ignored_columns'} <=
            {d.code for d in model.validate().diagnostics})
        model.tags.update(('swmm:nodes', 'j'), text='edited')
        output = model.to_document()
        self.assertEqual(len(output.records('TAGS')), 2)
        self.assertIn('; old', output.text)
        self.assertEqual(load(output.text).tags[('swmm:nodes', 'J')].text, 'edited')
        self.assertEqual(len(portable(model).to_document().records('TAGS')), 2)

    def test_reference_delete_protection_cascade_and_atomic_rollback(self):
        model = Model()
        model.nodes.add(Junction(id='A', elevation=0))
        model.nodes.add(Junction(id='B', elevation=0))
        tag = ObjectTag(target=Ref(collection='swmm:nodes', key='A'), text='kept')
        model.tags.add(tag)
        with self.assertRaises(ValidationError):
            model.nodes.remove('A')
        with self.assertRaises(ValueError):
            model.nodes.rename('A', 'B')
        self.assertEqual(model.tags[('swmm:nodes', 'A')], tag)
        removed = model.nodes.remove('A', cascade=True)
        self.assertEqual({v.collection for v in removed}, {'swmm:nodes', 'swmm:tags'})
        self.assertFalse(model.tags)
        self.assertFalse(model.to_document().records('TAGS'))

    def test_scenario_units_and_cache_conditions(self):
        from test_cache_reuse_v2 import snapshot, compare
        model = network()
        model.tags.add(ObjectTag(target=Ref(collection='swmm:nodes', key='J'), text='base'))
        patch = ScenarioPatch(operations=(SetFields(target=Ref(collection='swmm:tags', key=('swmm:nodes', 'J')),
            changes=(FieldChange(name='text', value='changed'),)),))
        changed = ScenarioPatch.from_json_document(patch.to_json_document()).apply(model).model
        self.assertEqual(changed.tags[('swmm:nodes', 'J')].text, 'changed')
        self.assertEqual(model.tags[('swmm:nodes', 'J')].text, 'base')
        assessment = compare(snapshot(model), snapshot(changed), intent='require_match')
        self.assertEqual(assessment.status, 'matched')
        self.assertTrue(assessment.allowed)
        before = tuple(changed.tags.items())
        changed.convert_units('CMS')
        self.assertEqual(tuple(changed.tags.items()), before)

    def test_invalid_and_unknown_data_remain_visible(self):
        base = network().to_document().text
        for row in ('Node J', 'Node J "bad; truncated"', 'Node Missing tag'):
            source = base + '[TAGS]\n' + row + '\n'
            model = Model.from_document(InpDocument.from_text(source, source='tags.inp'))
            self.assertEqual(model.document.text, source)
            self.assertFalse(model.validate().is_valid)
            with self.assertRaises(ValidationError):
                model.to_document()
        source = base + '[TAGS]\nFuture J value\n'
        model = load(source)
        self.assertTrue(model.support.opaque_records)
        self.assertEqual(model.to_document().text, source)
        with self.assertRaises(ValidationError):
            model.nodes.rename('J', 'K')
        model = network()
        valid = ObjectTag(target=Ref(collection='swmm:nodes', key='J'), text='ok')
        for row in (replace(valid, text='bad;value'), replace(valid, text='bad\nvalue'),
                    replace(valid, target=Ref(collection='swmm:curves', key='C')),
                    replace(valid, target=Ref(collection='swmm:nodes', key=('J', 'K')))):
            with self.assertRaises((ValidationError, ValueError)):
                with model.transaction():
                    model.tags.add(row)
        self.assertFalse(model.tags)


if __name__ == '__main__':
    unittest.main()
