"""Parser coordinates describe actual input, without inventing semantic owners."""
import codecs
from copy import deepcopy
from dataclasses import replace
import json
import unittest
from unittest.mock import patch

from easysewer.io.inp import InpDocument
from easysewer.io.inp.network import default_schema
from easysewer.io.json import JsonDocument, JsonField, JsonType
from easysewer.io.json.document import JsonLocations, json_path
from easysewer.model import Model, Point, Ref
from easysewer.schema import FeatureDescriptor, SupportLevel
from easysewer.runtime._result_codec import Codec
from easysewer.validation import Diagnostic, DiagnosticSubject, Severity, SourceSpan, ValidationError, ValidationReport
from test_json_v2 import entry
from test_model_extensions import Sensor, SensorCodec
from test_options_v2 import network

EVIDENCE = []


def text_at(document, span):
    return document.text.split('\n')[span.line - 1][span.column - 1:span.end_column - 1]


def document(data):
    return JsonDocument.from_data(data, source='parser-input.json')


class ParserDiagnosticTests(unittest.TestCase):
    def check(self, report, code):
        d = next(d for d in report.diagnostics if d.code == code)
        self.assertIsNotNone(d.span)
        codec = Codec(None)
        self.assertEqual(codec.decode(codec.encode(d)), d)
        EVIDENCE.append(dict(code=code, subject=d.subject.collection if d.subject else None,
            path=d.subject.path if d.subject else (), statuses=[v.status for v in d.locations]))
        return d

    def rejected(self, doc, code, **options):
        with self.assertRaises(ValidationError) as caught: Model.from_json_document(doc, **options)
        return self.check(caught.exception.report, code)

    def test_json_numeric_type_and_local_validation_have_exact_value_coordinates(self):
        for collection, key, name, bad, code in (
                ('swmm:nodes', 'J', 'elevation', True, 'json.field_type'),
                ('swmm:nodes', 'J', 'initial_depth', -9, 'model.invalid_field')):
            data = network().to_json_document().data
            entry(data, collection, key)['value'][name] = bad
            doc = document(data); d = self.rejected(doc, code)
            self.assertEqual(d.subject, DiagnosticSubject(collection=collection, key=key, path=(name,)))
            self.assertEqual(text_at(doc, d.span), json.dumps(bad))
            self.assertEqual(d.span.source, doc.source)
            self.assertTrue(d.field.endswith('.value.' + name))

    def test_nested_temporal_failure_stops_at_declared_model_field(self):
        data = network().to_json_document().data
        entry(data, 'swmm:options', 'settings')['value']['routing_step']['microseconds'] = -1
        doc = document(data); d = self.rejected(doc, 'json.temporal_value')
        self.assertEqual(d.subject.path, ('routing_step',))
        self.assertTrue(d.field.endswith('.value.routing_step'))
        self.assertEqual(text_at(doc, d.span), '{')

    def test_explicit_wire_aliases_drive_model_paths(self):
        schema = default_schema(); schema.register(FeatureDescriptor(key='test:sensors', sections={'SENSORS'}), SensorCodec())
        schema.register_json(JsonType(key='test:sensor', value_type=Sensor, fields=(
            JsonField(name='name', attribute='id', shape=('string',)),
            JsonField(name='at', attribute='node', shape=('object', 'core:ref')),
            JsonField(name='limit', attribute='threshold', shape=('number',)))))
        model = Model.from_document(InpDocument.from_text('[JUNCTIONS]\nJ 0\n[SENSORS]\nS J 1\n'), schema=schema)
        for bad, code in ((False, 'json.field_type'), (-3, 'model.invalid_field')):
            data = model.to_json_document().data; entry(data, 'test:sensors', 'S')['value']['limit'] = bad
            doc = document(data); d = self.rejected(doc, code, schema=schema)
            self.assertEqual(d.subject, DiagnosticSubject(collection='test:sensors', key='S', path=('threshold',)))
            self.assertTrue(d.field.endswith('.value.limit'))
            self.assertEqual(text_at(doc, d.span), json.dumps(bad))

    def test_unknown_member_with_punctuation_unicode_and_repeated_values(self):
        data = network().to_json_document().data
        name = 'odd.name["🔧"]'; entry(data, 'swmm:nodes', 'J')['value'][name] = 777
        data['same-value'] = 777
        doc = document(data); model = Model.from_json_document(doc)
        candidates = [d for d in model.validate().diagnostics if d.code == 'json.unknown_field' and d.subject]
        self.assertEqual(len(candidates), 1)
        d = self.check(ValidationReport(diagnostics=tuple(candidates)), 'json.unknown_field')
        self.assertEqual(d.subject, DiagnosticSubject(collection='swmm:nodes', key='J'))
        self.assertEqual(d.field.rsplit('.value', 1)[1], '[' + json.dumps(name, ensure_ascii=False) + ']')
        self.assertEqual(text_at(doc, d.span), '777')
        self.assertIn(json.dumps(name, ensure_ascii=False), doc.text.splitlines()[d.span.line - 1])
        self.assertEqual(model.to_json_document().to_bytes(), doc.to_bytes())

    def test_unknown_variant_and_collection_have_explicit_absent_identity(self):
        data = network().to_json_document().data
        entry(data, 'swmm:links', 'P')['value']['section']['geometry'] = {'type': 'future:geometry', 'value': 42}
        data['collections'].append({'collection': 'future:objects', 'records': [
            {'key': ['A', 'B'], 'value': {'type': 'future:object', 'x': 5}}]})
        doc = document(data); model = Model.from_json_document(doc)
        issues = [d for d in model.validate().diagnostics if d.code == 'json.unknown_value']
        self.assertEqual(len(issues), 2)
        for d in issues:
            self.check(ValidationReport(diagnostics=(d,)), d.code)
            self.assertEqual(d.locations[0].status, 'absent')
        self.assertEqual(issues[0].subject.path, ('section', 'geometry'))
        self.assertEqual(issues[1].subject.key, ('A', 'B'))
        self.assertEqual(model.to_json_document().to_bytes(), doc.to_bytes())

    def test_duplicate_and_mismatched_keys_use_supplied_identity(self):
        data = network().to_json_document().data
        entry(data, 'swmm:nodes', 'J')['key'] = 'Wrong'
        doc = document(data); d = self.rejected(doc, 'json.identity_mismatch')
        self.assertEqual(d.subject.key, 'Wrong'); self.assertEqual(d.related[0].key, 'J')
        self.assertEqual(text_at(doc, d.span), '"Wrong"')
        data = network().to_json_document().data
        block = next(b for b in data['collections'] if b['collection'] == 'swmm:nodes')
        block['records'].append(deepcopy(block['records'][0]))
        doc = document(data); d = self.rejected(doc, 'json.duplicate_record')
        self.assertEqual(d.subject.key, 'J')
        self.assertIn('.records[2]', d.field)

    def test_missing_identity_is_not_guessed_from_value_or_document(self):
        data = network().to_json_document().data; entry(data, 'swmm:nodes', 'J').pop('key')
        doc = document(data); d = self.rejected(doc, 'json.missing_field')
        self.assertIsNone(d.subject)
        self.assertTrue(d.field.endswith('.records[0]'))
        self.assertEqual(text_at(doc, d.span), '{')

    def test_unknown_array_fields_do_not_rebind_to_another_point(self):
        model = network(); model.links.update('P', vertices=(Point(x=1, y=2), Point(x=3, y=4)))
        data = model.to_json_document().data; entry(data, 'swmm:links', 'P')['value']['vertices'][0]['caption'] = 'protected'
        doc = document(data); model = Model.from_json_document(doc)
        d = self.check(model.validate(), 'json.unknown_field')
        self.assertEqual(d.subject.path, ('vertices', 0))
        self.assertEqual(d.locations[0].status, 'untracked')
        model.links.update('P', vertices=tuple(reversed(model.links['P'].vertices)))
        d = self.check(model.validate(), 'json.unknown_field_edit')
        self.assertEqual(d.subject.path, ('vertices', 0))
        self.assertEqual(d.locations[0].status, 'untracked')
        with self.assertRaises(ValidationError) as caught: model.to_json_document()
        self.assertEqual(self.check(caught.exception.report, 'json.unknown_field_edit'), d)
        self.assertEqual(doc.to_bytes(), document(data).to_bytes())

    def test_json_locations_handle_bom_crlf_escapes_and_index_once(self):
        text = '\r\n {"a.b": ["same", {"\\u0078": "é😀\\nvalue"}], "same":"same"}\r\n'
        doc = JsonDocument.from_bytes(codecs.BOM_UTF8 + text.encode(), source='unicode.json')
        locations = JsonLocations(doc)
        with patch.object(locations, '_index', wraps=locations._index) as index:
            for _ in range(50):
                self.assertEqual(text_at(doc, locations.span(('a.b', 1, 'x'))), '"é😀\\nvalue"')
                self.assertEqual(text_at(doc, locations.span(('same',))), '"same"')
            self.assertEqual(index.call_count, 1)
        self.assertEqual(json_path(('a.b', 1, 'x')), '$["a.b"][1].x')
        self.assertEqual(locations.span(('a.b', 1, 'missing')), locations.span(('a.b', 1)))

    def test_valid_known_json_does_not_build_diagnostic_index(self):
        doc = document(network().to_json_document().data)
        with patch.object(JsonLocations, '_index', side_effect=AssertionError('unnecessary diagnostic scan')):
            model = Model.from_json_document(doc, strict=True)
        self.assertEqual(model.to_json_document().to_bytes(), doc.to_bytes())

    def test_document_warnings_have_coordinates_without_invented_model_owner(self):
        data = network().to_json_document().data; data['schema_version'] = '1.9'
        doc = document(data); d = self.check(Model.from_json_document(doc).validate(), 'json.future_minor')
        self.assertIsNone(d.subject); self.assertEqual(text_at(doc, d.span), '"1.9"')

    def test_envelope_errors_use_actual_json_values(self):
        for name, value, code, expected in (
                ('schema_version', 'invalid', 'json.schema_version', '"invalid"'),
                ('schema_version', '2.0', 'json.schema_major', '"2.0"'),
                ('extensions', {'invalid namespace': 123}, 'json.extension', '123')):
            data = network().to_json_document().data; data[name] = value
            doc = document(data); d = self.rejected(doc, code)
            self.assertIsNone(d.subject)
            self.assertEqual(text_at(doc, d.span), expected)

    def test_source_identity_ledger_errors_do_not_claim_model_objects(self):
        model = Model.from_document(network().to_document())
        for duplicate in (True, False):
            data = model.to_json_document().data
            records = data['source']['known_records']
            if duplicate:
                records.append(deepcopy(records[0]))
                expected = '{'
                suffix = f'.known_records[{len(records) - 1}]'
            else:
                records[0]['future'] = 123
                expected, suffix = '123', '.known_records[0].future'
            doc = document(data); d = self.rejected(doc, 'json.source')
            self.assertIsNone(d.subject)
            self.assertEqual(text_at(doc, d.span), expected)
            self.assertTrue(d.field.endswith(suffix))

    def test_unhashable_discriminator_and_private_attribute_stop_semantic_mapping(self):
        types = default_schema().json_types
        self.assertEqual(types.model_path({'type': []}, ('field',)), ())
        types.register(JsonType(key='test:private', value_type=Sensor, fields=(
            JsonField(name='public', attribute='_private', shape=('number',)),)))
        self.assertEqual(types.model_path({'type': 'test:private', 'public': 1}, ('public',)), ())

    def test_invalid_inp_records_remain_unclaimed_and_absent(self):
        cases = [('[JUNCTIONS]\nJ bad\n', 'inp.invalid_record', 'swmm:nodes', 'J'),
                 ('[FILES]\nSAVE INFLOWS invalid.dat\n', 'files.invalid_input', 'swmm:files', ('INFLOWS', 'SAVE')),
                 ('[CURVES]\nC SHAPE 0\n', 'resource.invalid_input', 'swmm:curves', 'C')]
        for text, code, collection, key in cases:
            with self.subTest(code=code):
                doc = InpDocument.from_text(text, source='parser-original.inp'); model = Model.from_document(doc)
                for current in (model, model.copy(), Model.from_json_document(model.to_json_document())):
                    d = self.check(current.validate(), code)
                    self.assertEqual(d.subject.collection, collection); self.assertEqual(d.subject.key, key)
                    self.assertEqual(d.locations[0].status, 'absent')
                    self.assertEqual(d.span.line, 2)
                    self.assertEqual(current.document.to_bytes(), doc.to_bytes())
                self.assertEqual(model.support.support_for(2), SupportLevel.PRESERVED)

    def test_claimed_warning_uses_verified_binding_and_tracks_later_edits(self):
        doc = InpDocument.from_text('[JUNCTIONS]\nJ 0\n[COORDINATES]\nJ 1 2 ignored\n', source='parser-original.inp')
        model = Model.from_document(doc, strict=True)
        d = self.check(model.validate(), 'geometry.ignored_columns')
        self.assertEqual(d.subject, DiagnosticSubject(collection='swmm:nodes', key='J'))
        self.assertEqual(d.locations[0].status, 'context')
        self.assertEqual(model.support.features['swmm:network'].report.diagnostics[0].subject, d.subject)
        model.nodes.update('J', elevation=1)
        self.assertEqual(self.check(model.validate(), 'geometry.ignored_columns').locations[0].status, 'changed')

    def test_independent_codec_must_own_the_diagnostic_line(self):
        class Warnings(SensorCodec):
            def decode(self, document, profile):
                result = super().decode(document, profile)
                issues = tuple(Diagnostic(code=code, message=code, severity=Severity.WARNING,
                    span=SourceSpan(source=source, line=line, column=1, end_column=2))
                    for code, source, line in [('fixture.owned', document.source, 4),
                                               ('fixture.foreign_line', document.source, 2),
                                               ('fixture.foreign_source', 'external.dat', 4)])
                return replace(result, report=ValidationReport(diagnostics=issues))
        schema = default_schema(); schema.register(FeatureDescriptor(key='test:sensors', sections={'SENSORS'}), Warnings())
        doc = InpDocument.from_text('[JUNCTIONS]\nS 0\n[SENSORS]\nS S 1\n', source='parser-original.inp')
        m = Model.from_document(doc, schema=schema, strict=True)
        owned = self.check(m.validate(), 'fixture.owned')
        self.assertEqual(owned.subject.collection, 'test:sensors')
        for code in ('fixture.foreign_line', 'fixture.foreign_source'):
            self.assertIsNone(self.check(m.validate(), code).subject)


if __name__ == '__main__': unittest.main()
