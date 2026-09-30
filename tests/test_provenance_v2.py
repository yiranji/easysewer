"""Original source associations survive graph lifecycle and source-bearing JSON."""
import base64
from copy import deepcopy
from dataclasses import replace
import hashlib
import unittest

from easysewer.io.inp import InpDocument
from easysewer.io.json import JsonDocument
from easysewer.model import Model, Ref
from easysewer.model.network import Junction
from easysewer.validation import ValidationError
from test_scenario_v2 import portable, fields
from easysewer.scenario import ScenarioPatch, RenameRecord

SOURCE = ('; project\n[JUNCTIONS]\nj 1 2 ; imported\nK 0\n'
          '[COORDINATES]\nj 1 2\n[COORDINATES]\nj 3 4\n'
          '[TAGS]\nNode j district\n[MAP]\nUNITS FEET\n')


def load():
    return Model.from_document(InpDocument.from_text(SOURCE, source='original.inp'), strict=True)


def ref(key='j', collection='swmm:nodes'):
    return Ref(collection=collection, key=key)


def restore(model):
    return Model.from_json_document(model.to_json_document(), strict=True)


class ProvenanceTests(unittest.TestCase):
    def test_original_records_positions_duplicates_and_immutable_values(self):
        model = load()
        info = model.provenance(ref())
        self.assertEqual(info.kind, 'inp')
        self.assertEqual(info.owner.key, 'j')
        self.assertEqual(info.original, ref('J'))
        self.assertEqual(info.source, 'original.inp')
        self.assertEqual(info.source_sha256, hashlib.sha256(SOURCE.encode()).hexdigest())
        self.assertEqual([row.span.line for row in info.records], [3, 6, 8])
        self.assertEqual([row.section for row in info.records], ['JUNCTIONS', 'COORDINATES', 'COORDINATES'])
        self.assertIn('; imported', info.records[0].text)
        self.assertEqual(info.records[1].values, ('j', '1', '2'))
        self.assertEqual(info.original_value.position.x, 3)
        self.assertFalse(info.changed)
        model.nodes.update('J', elevation=5)
        changed = model.provenance(ref())
        self.assertTrue(changed.changed)
        self.assertEqual(changed.records, info.records)
        self.assertEqual(changed.original_value.elevation, 1)
        self.assertFalse(info.changed)
        with self.assertRaises(KeyError): model.provenance(ref('absent'))
        model.nodes.update('j', elevation=True)
        self.assertTrue(model.provenance(ref()).changed)  # True == 1 is not a typed no-op.
        with self.assertRaises(TypeError): model.provenance('j')

    def test_rename_moves_indirect_composite_keys_and_preserves_original_owner(self):
        model = load()
        before = model.provenance(ref(('swmm:nodes', 'j'), 'swmm:tags'))
        model.nodes.rename('j', 'New')
        model.nodes.move('New')
        for copy in (model, model.copy(), restore(model)):
            info = copy.provenance(ref('New'))
            self.assertEqual(info.original, ref('J'))
            self.assertEqual(info.original_value.id, 'j')
            self.assertTrue(info.changed)
            tag = copy.provenance(ref(('swmm:nodes', 'New'), 'swmm:tags'))
            self.assertEqual(tag.original, before.original)
            self.assertEqual(tag.records, before.records)
            self.assertTrue(tag.changed)
            self.assertEqual(list(copy.nodes), ['K', 'New'])
        self.assertEqual(restore(model).to_document().text, model.to_document().text)

    def test_delete_recreate_same_id_has_no_borrowed_origin_including_json(self):
        model = load()
        old = model.nodes['j']
        removed = model.nodes.remove('j', cascade=True)
        self.assertEqual(len(removed), 2)
        model.nodes.add(old)
        for copy in (model, model.copy(), restore(model)):
            info = copy.provenance(ref())
            self.assertEqual(info.kind, 'created')
            self.assertIsNone(info.original)
            self.assertFalse(info.records)
            self.assertIsNone(info.changed)
            self.assertFalse(copy.tags)

    def test_nested_rollback_copy_and_scenario_keep_lineage_isolated(self):
        model = load()
        before = model.to_json_document().data
        with self.assertRaises(RuntimeError):
            with model.transaction():
                model.nodes.rename('j', 'Changed')
                with model.transaction():
                    model.nodes.remove('Changed', cascade=True)
                    model.nodes.add(Junction(id='j', elevation=99))
                raise RuntimeError('abort whole operation')
        self.assertEqual(model.to_json_document().data, before)
        with model.transaction():
            try:
                with model.transaction():
                    model.nodes.rename('j', 'Failed')
                    raise RuntimeError('nested abort')
            except RuntimeError:
                pass
            model.nodes.rename('j', 'Committed')
        self.assertEqual(model.provenance(ref('Committed')).original, ref('J'))
        patch = ScenarioPatch(operations=(RenameRecord(target=ref('Committed'), new_id='Scenario'),
            fields('nodes', 'Scenario', elevation=8)))
        changed = ScenarioPatch.from_json_document(patch.to_json_document()).apply(model).model
        self.assertEqual(changed.provenance(ref('Scenario')).original, ref('J'))
        self.assertEqual(model.provenance(ref('Committed')).original, ref('J'))
        changed.nodes.remove('Scenario', cascade=True)
        self.assertEqual(model.provenance(ref('Committed')).kind, 'inp')

    def test_old_json_has_unknown_history_and_portable_json_has_no_inp_claim(self):
        model = load()
        data = model.to_json_document().data
        data['source'].pop('origins')
        doc = JsonDocument.from_data(data)
        older = Model.from_json_document(doc, strict=True)
        self.assertEqual(older.provenance(ref()).kind, 'untracked')
        self.assertFalse(older.provenance(ref()).records)
        self.assertEqual(older.to_json_document().text, doc.text)
        older.nodes.rename('j', 'Renamed')
        self.assertEqual(restore(older).provenance(ref('Renamed')).kind, 'untracked')
        older.nodes.add(Junction(id='Added', elevation=0))
        self.assertEqual(restore(older).provenance(ref('Added')).kind, 'created')
        self.assertEqual(portable(model).provenance(ref()).kind, 'untracked')
        rebuilt = Model.from_document(model.to_document(), strict=True)
        self.assertEqual(rebuilt.provenance(ref()).kind, 'inp')
        self.assertFalse(rebuilt.provenance(ref()).changed)

    def test_invalid_ledger_associations_and_source_hash_are_rejected(self):
        data = load().to_json_document().data
        def ledger(value): return value['source']['origins']
        mutations = (
            lambda d: ledger(d).update(source_sha256='0'*64),
            lambda d: ledger(d)['records'].append(deepcopy(ledger(d)['records'][0])),
            lambda d: ledger(d)['records'][0]['owner'].update(key='missing'),
            lambda d: ledger(d)['records'][0]['original'].update(key='missing'),
            lambda d: ledger(d)['records'][0]['original'].update(collection='swmm:links'),
            lambda d: ledger(d)['records'][0].update(kind='created'),
            lambda d: ledger(d)['records'][0].update(kind='unknown'),
            lambda d: ledger(d)['records'][1].update(original=deepcopy(ledger(d)['records'][0]['original'])),
            lambda d: d['source'].update(bytes_base64=base64.b64encode((SOURCE+'; changed\n').encode()).decode()),
        )
        for mutation in mutations:
            changed = deepcopy(data)
            mutation(changed)
            with self.subTest(mutation=mutation), self.assertRaises(ValidationError):
                Model.from_json_document(JsonDocument.from_data(changed))

    def test_future_origin_version_is_preserved_and_guards_identity_mutation(self):
        data = load().to_json_document().data
        data['source']['origins']['schema_version'] = '9.0'
        data['source']['origins']['future'] = {'opaque': True}
        model = Model.from_json_document(JsonDocument.from_data(data))
        self.assertEqual(model.to_json_document().data, data)
        self.assertEqual(model.provenance(ref()).kind, 'untracked')
        self.assertIn('json.origins_version', {d.code for d in model.validate().diagnostics})
        with self.assertRaises(ValidationError): model.nodes.rename('j', 'new')
        with self.assertRaises(ValidationError): model.to_document()

    def test_unit_conversion_keeps_original_declarations_and_inp_reload_establishes_baseline(self):
        model = load()
        model.nodes.rename('j', 'New')
        model.convert_units('CMS')
        info = model.provenance(ref('New'))
        self.assertEqual(info.original_value.elevation, 1)
        self.assertAlmostEqual(model.nodes['New'].elevation, .3048)
        self.assertTrue(info.changed)
        self.assertEqual(restore(model).provenance(ref('New')), info)
        reloaded = Model.from_document(model.to_document(), strict=True)
        self.assertEqual(reloaded.provenance(ref('New')).original, ref('NEW'))
        self.assertFalse(reloaded.provenance(ref('New')).changed)
        self.assertNotEqual(reloaded.provenance(ref('New')).source_sha256, info.source_sha256)


if __name__ == '__main__':
    unittest.main()
