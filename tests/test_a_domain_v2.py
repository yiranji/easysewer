"""Independent A extension: public registration, references and persistence."""
from dataclasses import replace
import unittest

from easysewer.io.inp import InpDocument
from easysewer.io.json import JsonDocument
from easysewer.model import Model, Ref
from easysewer.model.geometry import Triangular
from easysewer.model.network import FunctionalRating
from easysewer.scenario import ScenarioPatch, RenameRecord, RemoveRecord, SetFields, FieldChange
from easysewer.validation import ValidationError
from a_extension_fixture import ORACLE, SlopeTriangle, SquareLaw, CurveLaw, extension_schema, model


def ref(collection, key):
    return Ref(collection='swmm:' + collection, key=key)


def imported():
    return Model.from_document(InpDocument.from_text(ORACLE, source='a-original.inp'),
                               schema=extension_schema(), strict=True)


def state(value):
    return tuple(tuple(getattr(value, name).items()) for name in ('nodes', 'links', 'curves'))


def portable(value):
    data = value.to_json_document().data
    data.pop('source', None)
    return Model.from_json_document(JsonDocument.from_data(data), schema=extension_schema(), strict=True)


def edit_patch(value):
    return ScenarioPatch(operations=(
        RenameRecord(target=ref('nodes', 'J'), new_id='J2'),
        RenameRecord(target=ref('curves', 'Rating'), new_id='Rate2'),
        SetFields(target=ref('links', 'P'), changes=(FieldChange(name='section',
            value=replace(value.links['P'].section, geometry=SlopeTriangle(depth=1.5, side_slope=1.5))),)),
        SetFields(target=ref('links', 'Q'), changes=(FieldChange(name='rating',
            value=SquareLaw(basis='HEAD', coefficient=.6)),)),
    ))


def edited(value=None):
    value = value or imported()
    schema = extension_schema()
    patch = ScenarioPatch.from_json_document(edit_patch(value).to_json_document(schema=schema), schema=schema)
    return patch.apply(value).model


# Native rows are edited literally, independently of the extension writer.
EDITED_ORACLE = ORACLE.replace('J .5 5 0', 'J2 .5 5 0').replace('P S J 50', 'P S J2 50').replace(
    'P TRIANGULAR 2 4 0 0', 'P TRIANGULAR 1.5 4.5 0 0').replace(
    'Q J O 0 FUNCTIONAL/HEAD .4 2', 'Q J2 O 0 FUNCTIONAL/HEAD .6 2').replace(
    'T J O2 0', 'T J2 O2 0').replace('Rating', 'Rate2')


class ADomainTests(unittest.TestCase):
    def test_derived_geometry_and_rating_tokens_preserve_their_origins(self):
        value = imported()
        slope = value.inspect_field(ref('links', 'P'), ('section', 'geometry', 'side_slope'))
        self.assertEqual(slope.provenance.status, 'derived')
        self.assertEqual([t.raw for t in slope.provenance.declarations[0].tokens], ['2', '4'])
        self.assertEqual(slope.value, 1)
        # The extension declares source and conversion rules; it does not
        # borrow the built-in geometry's exact-type effective-value rules.
        self.assertEqual(slope.semantics.effective.status, 'unknown')
        coefficient = value.inspect_field(ref('links', 'Q'), ('rating', 'coefficient'))
        self.assertEqual(coefficient.provenance.status, 'explicit')
        self.assertEqual([t.raw for t in coefficient.provenance.declarations[0].tokens], ['.4'])
        changed = edited(value)
        restored = Model.from_json_document(changed.to_json_document(), schema=extension_schema(), strict=True)
        for candidate in (changed, restored):
            info = candidate.inspect_field(ref('links', 'P'), ('section', 'geometry', 'side_slope'))
            self.assertEqual(info.value, 1.5)
            self.assertTrue(info.changed)
            self.assertEqual(info.provenance, slope.provenance)
            self.assertEqual(candidate.provenance(ref('nodes', 'J2')).original, ref('nodes', 'J'))
            self.assertEqual(candidate.provenance(ref('curves', 'Rate2')).original, ref('curves', 'RATING'))

    def test_invalid_extension_values_and_wrong_curve_purpose_are_diagnosed(self):
        for kind in ('depth', 'slope', 'coefficient', 'curve'):
            value = model()
            if kind in ('depth', 'slope'):
                shape = SlopeTriangle(depth=-1 if kind == 'depth' else 2, side_slope=-1 if kind == 'slope' else 1)
                value.links.update('P', section=replace(value.links['P'].section, geometry=shape))
            elif kind == 'coefficient':
                value.links.update('Q', rating=SquareLaw(basis='HEAD', coefficient=-1))
            else:
                value.curves.update('Rating', kind='PUMP1')
            with self.subTest(kind=kind):
                errors = value.validate().errors
                self.assertTrue(errors)
                self.assertIn('resource.wrong_purpose' if kind == 'curve' else 'model.invalid_field',
                              {d.code for d in errors})

    def test_creation_import_edit_and_all_roundtrips(self):
        created, loaded = model(), imported()
        self.assertEqual(state(created), state(loaded))
        for value in (created, loaded):
            original = value.to_json_document().to_bytes()
            changed = edited(value)
            self.assertEqual(value.to_json_document().to_bytes(), original)
            self.assertTrue(changed.validate(for_run=True).is_valid)
            self.assertEqual(changed.links['T'].rating.curve, ref('curves', 'Rate2'))
            self.assertEqual(changed.links['P'].outlet, ref('nodes', 'J2'))
            self.assertEqual(changed.links['Q'].inlet, ref('nodes', 'J2'))
            for restored in (portable(changed),
                Model.from_json_document(changed.to_json_document(), schema=extension_schema(), strict=True),
                Model.from_document(changed.to_document(), schema=extension_schema(), strict=True),
                Model.from_document(changed.to_document(normalize=True), schema=extension_schema(), strict=True),
                Model.from_document(InpDocument.from_text(EDITED_ORACLE), schema=extension_schema(), strict=True)):
                self.assertEqual(state(restored), state(changed))
                self.assertTrue(restored.validate(for_run=True).is_valid)

    def test_registration_is_local_and_unknown_json_can_be_upgraded(self):
        source = imported()
        ordinary = Model.from_document(source.to_document(), strict=True)
        self.assertIs(type(ordinary.links['P'].section.geometry), Triangular)
        self.assertIs(type(ordinary.links['Q'].rating), FunctionalRating)
        self.assertIs(type(source.links['T'].rating), CurveLaw)
        for value in (source, portable(source)):
            document = value.to_json_document()
            old = Model.from_json_document(document, strict=True)
            self.assertEqual(old.to_json_document().data, document.data)
            with self.assertRaises(ValidationError):
                old.to_document()
            upgraded = Model.from_json_document(old.to_json_document(), schema=extension_schema(), strict=True)
            self.assertEqual(state(upgraded), state(value))

    def test_references_failed_patch_and_cascade_keep_formula_types(self):
        value = imported()
        original = value.to_json_document().to_bytes()
        operations = edit_patch(value).operations + (RemoveRecord(target=ref('curves', 'Rate2')),)
        with self.assertRaises(ValidationError):
            ScenarioPatch(operations=operations).apply(value)
        self.assertEqual(value.to_json_document().to_bytes(), original)
        changed = edited(value)
        with self.assertRaises(ValidationError):
            changed.curves.remove('Rate2')
        removed = changed.curves.remove('Rate2', cascade=True)
        self.assertIn(ref('links', 'T'), removed)
        self.assertEqual(tuple(changed.links), ('P', 'Q'))
        self.assertIs(type(changed.links['Q'].rating), SquareLaw)
        self.assertEqual(state(portable(changed)), state(changed))

    def test_extension_units_follow_the_physical_formula_and_curve_dimensions(self):
        value = model()
        value.convert_units('CFS', basis='physical')
        shape = value.links['P'].section.geometry
        self.assertAlmostEqual(shape.depth, 2/.3048)
        self.assertEqual(shape.side_slope, 1)
        self.assertAlmostEqual(value.links['Q'].rating.coefficient, .4/.3048)
        self.assertAlmostEqual(value.curves['Rating'].points[2].x, 1/.3048)
        self.assertAlmostEqual(value.curves['Rating'].points[2].y, .3/.028316846592)
        # At an equivalent head, q_US * ft^3-to-m^3 equals q_SI.
        self.assertAlmostEqual(value.links['Q'].rating.coefficient*(1.7/.3048)**2*.028316846592, .4*1.7**2)
        restored = portable(value)
        restored.convert_units('CMS', basis='physical')
        self.assertAlmostEqual(restored.links['Q'].rating.coefficient, .4)
        self.assertAlmostEqual(restored.links['P'].section.geometry.depth, 2)
        self.assertAlmostEqual(restored.curves['Rating'].points[2].y, .3)


if __name__ == '__main__':
    unittest.main()
