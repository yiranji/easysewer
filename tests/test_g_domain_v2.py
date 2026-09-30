"""G acceptance: preservation, conservative edits and explicit feature upgrades."""
import codecs
from dataclasses import replace
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from easysewer.io.inp import InpDocument
from easysewer.io.json import JsonDocument
from easysewer.model import Model, Ref
from easysewer.model.network import Junction
from easysewer.scenario import ScenarioPatch, RenameRecord, SetFields, FieldChange
from easysewer.validation import ValidationError
from g_extension_fixture import COLLECTION, SECTION, SOURCE, FixedLimit, BandLimit, FutureProbe, schema


def load(generation=2):
    document = InpDocument.from_bytes(SOURCE.encode('gb18030'), encoding='gb18030', source='future-g.inp')
    return Model.from_document(document, schema=schema(generation), strict=True)


def rows(document):
    return next(block['records'] for block in document.data['collections'] if block['collection'] == COLLECTION)


class GDomainTests(unittest.TestCase):
    def assert_rejected_unchanged(self, model, operation):
        before = model.to_json_document().to_bytes()
        with self.assertRaises(ValidationError):
            with model.transaction():
                operation(model)
        self.assertEqual(model.to_json_document().to_bytes(), before)

    def test_typed_creation_and_source_free_json_follow_the_same_upgrade_contract(self):
        model = Model(schema=schema(2))
        for key in ('J', 'Spare'):
            model.nodes.add(Junction(id=key, elevation=0))
        records = (
            FutureProbe(id='A', node=Ref(collection='swmm:nodes', key='J'), limit=FixedLimit(depth=3),
                        backup=Ref(collection='swmm:nodes', key='Spare')),
            FutureProbe(id='B', node=Ref(collection='swmm:nodes', key='Spare'), limit=BandLimit(lower=1, upper=2)),
        )
        for record in records:
            model.collection(COLLECTION).add(record)
        self.assertNotIn('source', model.to_json_document().data)
        old = Model.from_json_document(model.to_json_document(), schema=schema(1), strict=True)
        self.assertEqual(old.to_json_document().data, model.to_json_document().data)
        upgraded = Model.from_json_document(old.to_json_document(), schema=schema(2), strict=True)
        rebuilt = Model.from_document(upgraded.to_document(), schema=schema(2), strict=True)
        self.assertEqual(tuple(rebuilt.collection(COLLECTION).values()), records)
        self.assertEqual(len(rebuilt.to_document().records(SECTION)), 2)

    def test_unknown_section_bytes_json_and_explicit_registration(self):
        original = InpDocument.from_bytes(SOURCE.encode('gb18030'), encoding='gb18030', source='future-g.inp')
        model = Model.from_document(original, strict=True)
        self.assertEqual(len(model.support.opaque_records), 3)
        self.assertEqual(model.to_document().to_bytes(), original.to_bytes())
        model = Model.from_json_document(model.to_json_document(), strict=True)
        self.assertEqual(model.to_document().to_bytes(), original.to_bytes())
        self.assert_rejected_unchanged(model, lambda m: m.nodes.rename('J', 'Renamed'))
        self.assert_rejected_unchanged(model, lambda m: m.convert_units('CMS'))
        upgraded = Model.from_json_document(model.to_json_document(), schema=schema(2), strict=True)
        self.assertEqual(tuple(upgraded.collection(COLLECTION)), ('FIRST', 'MIDDLE', 'LAST'))
        self.assertFalse(upgraded.support.opaque_records)
        self.assertEqual(upgraded.to_document().to_bytes(), original.to_bytes())

    def test_mixed_new_field_and_variant_survive_old_reader_safe_edits(self):
        raw = load().to_json_document().text.replace('  ', '\t').replace('\n', '\r\n')
        document = JsonDocument.from_bytes(codecs.BOM_UTF8 + raw.encode())
        old = Model.from_json_document(document, schema=schema(1), strict=True)
        self.assertEqual(old.to_json_document().to_bytes(), document.to_bytes())
        self.assertEqual(tuple(old.collection(COLLECTION)), ('FIRST', 'LAST'))
        old.collection(COLLECTION).update('FIRST', limit=FixedLimit(depth=9))
        old.nodes.update('J', elevation=12)
        serialized = old.to_json_document()
        self.assertEqual([row['key'] for row in rows(serialized)], ['FIRST', 'MIDDLE', 'LAST'])
        self.assertEqual(rows(serialized)[0]['value']['backup'], rows(document)[0]['value']['backup'])
        self.assertEqual(rows(serialized)[1], rows(document)[1])
        self.assertEqual(rows(serialized)[0]['value']['limit']['depth'], 9)
        for operation in (lambda m: m.nodes.rename('J', 'Renamed'),
                          lambda m: m.nodes.remove('Spare', cascade=True),
                          lambda m: m.collection(COLLECTION).move('LAST', before='FIRST'),
                          lambda m: m.convert_units('CMS')):
            self.assert_rejected_unchanged(old, operation)
        with self.assertRaises(ValidationError):
            old.to_document()
        self.assertIn('json.incomplete_model', {d.code for d in old.validate(for_run=True).errors})

    def test_upgrade_obeys_json_edits_references_units_and_no_duplicate_rows(self):
        old = Model.from_json_document(load().to_json_document(), schema=schema(1), strict=True)
        old.collection(COLLECTION).update('FIRST', limit=FixedLimit(depth=9))
        upgraded = Model.from_json_document(old.to_json_document(), schema=schema(2), strict=True)
        self.assertEqual(upgraded.collection(COLLECTION)['FIRST'].limit.depth, 9)
        # Source snapshot still declares 1.5; it cannot override the edited JSON.
        self.assertIn('FIRST J FIXED 1.5', upgraded.document.text)
        patch = ScenarioPatch(operations=(
            RenameRecord(target=Ref(collection='swmm:nodes', key='J'), new_id='Upstream'),
            RenameRecord(target=Ref(collection='swmm:nodes', key='Spare'), new_id='Backup'),
            SetFields(target=Ref(collection=COLLECTION, key='MIDDLE'),
                changes=(FieldChange(name='limit', value=BandLimit(lower=3, upper=7)),)),
        ))
        patch = ScenarioPatch.from_json_document(patch.to_json_document(schema=schema(2)), schema=schema(2))
        changed = patch.apply(upgraded).model
        changed.convert_units('CMS')
        self.assertAlmostEqual(changed.collection(COLLECTION)['FIRST'].limit.depth, 9 * .3048)
        self.assertAlmostEqual(changed.collection(COLLECTION)['MIDDLE'].limit.upper, 7 * .3048)
        self.assertEqual(changed.collection(COLLECTION)['FIRST'].node.key, 'Upstream')
        self.assertEqual(changed.collection(COLLECTION)['FIRST'].backup.key, 'Backup')
        document = changed.to_document()
        self.assertEqual(len(document.records(SECTION)), 3)
        restored = Model.from_document(document, schema=schema(2), strict=True)
        self.assertEqual(tuple(restored.collection(COLLECTION).items()), tuple(changed.collection(COLLECTION).items()))
        self.assertEqual(tuple(upgraded.nodes), ('J', 'Spare'))

    def test_disabling_codec_cannot_resurrect_deleted_records(self):
        model = load()
        model.collection(COLLECTION).remove('MIDDLE')
        model.collection(COLLECTION).rename('FIRST', 'Renamed')
        model.collection(COLLECTION).move('LAST', before='Renamed')
        for generation in (None, 1, None, 2):
            model = Model.from_json_document(model.to_json_document(),
                schema=None if generation is None else schema(generation), strict=True)
        self.assertEqual(tuple(model.collection(COLLECTION)), ('LAST', 'Renamed'))
        self.assertEqual([row.values[0] for row in model.to_document().records(SECTION)], ['LAST', 'Renamed'])
        model.collection(COLLECTION).add(replace(model.collection(COLLECTION)['Renamed'], id='MIDDLE'))
        self.assertEqual(len(model.to_document().records(SECTION)), 3)
        origin = model.provenance(Ref(collection=COLLECTION, key='MIDDLE'))
        self.assertFalse(origin.records)

    def test_unknown_required_root_content_stays_blocked_after_feature_upgrade(self):
        data = load().to_json_document().data
        data['schema_version'] = '1.9'
        data['required_capabilities'] = ['test:future-solver']
        data['extensions'] = {'test:future-settings': {'node': 'J', 'scale': 3}}
        model = Model.from_json_document(JsonDocument.from_data(data), schema=schema(1), strict=True)
        upgraded = Model.from_json_document(model.to_json_document(), schema=schema(2), strict=True)
        self.assertEqual(upgraded.to_json_document().data, data)
        with self.assertRaises(ValidationError):
            upgraded.to_document()
        self.assert_rejected_unchanged(upgraded, lambda m: m.nodes.rename('J', 'K'))

    def test_partial_source_upgrade_claims_new_rows_but_not_deleted_known_rows(self):
        old = load(1)
        self.assertEqual(tuple(old.collection(COLLECTION)), ('LAST',))
        self.assertEqual(len(old.support.opaque_records), 2)
        old.collection(COLLECTION).update('LAST', limit=FixedLimit(depth=8))
        upgraded = Model.from_json_document(old.to_json_document(), schema=schema(2), strict=True)
        self.assertEqual(tuple(upgraded.collection(COLLECTION)), ('FIRST', 'MIDDLE', 'LAST'))
        self.assertEqual(upgraded.collection(COLLECTION)['LAST'].limit.depth, 8)
        self.assertEqual(len(upgraded.to_document().records(SECTION)), 3)
        # Unknown source still protects deletions; only after promotion can the
        # user intentionally remove LAST and persist that decision.
        self.assert_rejected_unchanged(old, lambda m: m.collection(COLLECTION).remove('LAST'))
        upgraded.collection(COLLECTION).remove('LAST')
        dormant = Model.from_json_document(upgraded.to_json_document(), schema=schema(1), strict=True)
        restored = Model.from_json_document(dormant.to_json_document(), schema=schema(2), strict=True)
        self.assertEqual(tuple(restored.collection(COLLECTION)), ('FIRST', 'MIDDLE'))
        self.assertEqual(len(restored.to_document().records(SECTION)), 2)

    def test_fresh_process_can_explicitly_upgrade_without_loading_native_code(self):
        import easysewer
        old = Model.from_json_document(load().to_json_document(), schema=schema(1), strict=True)
        old.collection(COLLECTION).update('FIRST', limit=FixedLimit(depth=9))
        expected = Model.from_json_document(old.to_json_document(), schema=schema(2), strict=True)
        expected.nodes.rename('J', 'Upstream')
        expected.collection(COLLECTION).move('LAST', before='FIRST')
        expected.convert_units('CMS')
        child = '''import sys
from pathlib import Path
package, tests, source, destination = sys.argv[1:]
sys.path[:0] = [package, tests]
def deny_native(event, args):
    if event in ('ctypes.dlopen', 'subprocess.Popen', 'os.system'):
        raise AssertionError('Model upgrade attempted native loading or a child process: '+event)
sys.addaudithook(deny_native)
from easysewer.model import Model
from easysewer.io.json import JsonDocument
from g_extension_fixture import schema, COLLECTION
import easysewer
assert Path(easysewer.__file__).resolve().is_relative_to(Path(package).resolve())
document = JsonDocument.read(source)
unknown = Model.from_json_document(document, strict=True)
assert unknown.to_json_document().to_bytes() == document.to_bytes()
model = Model.from_json_document(unknown.to_json_document(), schema=schema(2), strict=True)
model.nodes.rename('J', 'Upstream')
model.collection(COLLECTION).move('LAST', before='FIRST')
model.convert_units('CMS')
assert len(model.to_document().records('G_PROBES')) == 3
model.to_json(destination)
'''
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old.to_json(root/'old.json')
            process = subprocess.run([sys.executable, '-I', '-B', '-c', child,
                str(Path(easysewer.__file__).resolve().parent.parent), str(Path(__file__).resolve().parent),
                str(root/'old.json'), str(root/'new.json')], capture_output=True, text=True,
                timeout=60, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            self.assertEqual(process.returncode, 0, process.stdout+process.stderr)
            restored = Model.from_json(root/'new.json', schema=schema(2), strict=True)
            self.assertEqual(restored.to_json_document().data, expected.to_json_document().data)


if __name__ == '__main__':
    unittest.main()
