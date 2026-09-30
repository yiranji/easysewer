"""Project field facts and physical-line provenance without invented tokens."""
from dataclasses import dataclass, fields, is_dataclass, replace
from datetime import time, timedelta
import unittest
from unittest.mock import patch

from easysewer.io.inp import InpDocument
from easysewer.io.inp.project import ProjectCodec
from easysewer.model import Model, Ref
from easysewer.model.project import ProjectTitle, JsonAnnotation, ObjectTag, ProfilePlot
from easysewer.model.report import ReportSelection
from easysewer.model.resources import SeriesPoint
from easysewer.schema import RegistryError
from easysewer.schema.structured import FieldLineBinding, ModelSchema
from easysewer.validation import ValidationError
from test_hydrology_v2 import hydrology_model

UNITS = ('CFS', 'GPM', 'MGD', 'CMS', 'LPS', 'MLD')
COLLECTIONS = ('swmm:title', 'easysewer:metadata', 'swmm:tags', 'swmm:profiles')
TITLE = Ref(collection='swmm:title', key='text')
NOTE = Ref(collection='easysewer:metadata', key='test:note')


def load(text, *, strict=True):
    return Model.from_document(InpDocument.from_text(text, source='project-fields.inp'), strict=strict)


def fixture(units='CFS', *, variant='mixed'):
    m = hydrology_model(); m.reinterpret_units(units)
    m.update_options(end_date=m.options.start_date, end_time=time(0, 40),
        routing_step=timedelta(seconds=30), wet_step=timedelta(minutes=1), report_step=timedelta(minutes=1))
    m.nodes.update('J', initial_depth=1)
    m.timeseries.update('Rain', points=(SeriesPoint(time=timedelta(), value=.6),
        SeriesPoint(time=timedelta(minutes=40), value=0)))
    m.raingages.update('R', interval=timedelta(minutes=1))
    m.update_report(subcatchments=ReportSelection(mode='ALL'), nodes=ReportSelection(mode='ALL'), links=ReportSelection(mode='ALL'))
    text = m.to_document().text
    blocks = {
        'title': '[TITLE]\nFirst title ; retained inline comment\n\n; comment only\n[TITLE]\nSecond title\nThird title\nFourth title\n',
        'tags': '[TAGS]\nNode J old\nnodes j "new tag" ignored\nLink P ""\nGage R rain\nSubcatch S catchment\n',
        'profiles': '[PROFILES]\n"North area" P P P P P P\n[PROFILES]\n"north AREA" P\n',
    }
    if variant == 'mixed': text += ''.join(blocks.values())
    elif variant in blocks: text += blocks[variant]
    m = load(text)
    if variant in ('mixed', 'annotation'):
        m.set_annotation('test:note', {'text': 'informational name J ' * 80, 'numbers': [0, 2.5]})
    return m.to_document().text


def queries(model):
    result = []
    def walk(owner, value, path=()):
        if is_dataclass(value):
            for f in fields(value):
                p = (*path, f.name)
                result.extend((model.inspect_field(owner, p), model.field_provenance(owner, p)))
                walk(owner, getattr(value, f.name), p)
        elif isinstance(value, tuple):
            for i, child in enumerate(value):
                p = (*path, i)
                result.extend((model.inspect_field(owner, p), model.field_provenance(owner, p)))
                walk(owner, child, p)
    for collection in COLLECTIONS:
        for key, value in model.collection(collection).items():
            walk(Ref(collection=collection, key=key), value)
    return tuple(result)


def materialize(model):
    for collection in COLLECTIONS:
        for key, value in tuple(model.collection(collection).items()):
            owner = Ref(collection=collection, key=key)
            updates = {}
            for f in fields(value):
                fact = model.inspect_field(owner, f.name).semantics.effective
                if fact.status != 'known': raise AssertionError(fact)
                updates[f.name] = fact.value
            model.collection(collection).replace(key, replace(value, **updates))


class ProjectFieldTests(unittest.TestCase):
    def test_six_units_all_project_fields_json_and_no_io(self):
        for units in UNITS:
            with self.subTest(units=units):
                m = load(fixture(units)); before = queries(m)
                for info in before[::2]:
                    self.assertEqual(info.semantics.effective.status, 'known', info)
                    self.assertEqual(info.semantics.effective.value, info.value)
                    self.assertEqual(info.semantics.unit.status, 'not_applicable')
                restored = Model.from_json_document(m.to_json_document(), strict=True)
                self.assertEqual(queries(restored), before)
                self.assertEqual(restored.to_document().to_bytes(), m.to_document().to_bytes())
                with patch('builtins.open', side_effect=AssertionError('Unexpected I/O')), \
                     patch.object(Model, 'to_document', side_effect=AssertionError('Unexpected rendering')):
                    self.assertEqual(queries(m), before)
                materialize(restored)
                self.assertEqual(restored.to_document().to_bytes(), m.to_document().to_bytes())
                m.convert_units('CMS' if units != 'CMS' else 'CFS')
                self.assertEqual(queries(m), before)

    def test_raw_lines_include_empty_unicode_comments_and_exact_spans(self):
        source = '[TITLE]\r\n标题 ; 行内注释\r\n\r\n; 独立注释\r\n[TITLE]\r\n末行'
        document = InpDocument.from_bytes(source.encode('utf-8-sig'), encoding='utf-8-sig', source='标题.inp')
        m = Model.from_document(document, strict=True)
        declarations = m.field_provenance(TITLE, 'lines').declarations
        self.assertEqual([d.raw_text for d in declarations], ['标题 ; 行内注释', '', '; 独立注释', '末行'])
        self.assertEqual([d.line for d in declarations], [2, 3, 4, 6])
        for i, d in enumerate(declarations):
            self.assertEqual(d.tokens, ())
            self.assertEqual((d.span.source, d.span.line, d.span.column, d.span.end_column),
                             ('标题.inp', d.line, 1, len(d.raw_text) + 1))
            self.assertEqual(d.source_owners, (TITLE.canonical,))
            self.assertTrue(d.contributes)
            self.assertEqual(m.field_provenance(TITLE, ('lines', i)).status, 'explicit')
            self.assertEqual(m.inspect_field(TITLE, ('lines', i)).provenance.status, 'untracked_path')
        self.assertEqual(m.to_document().to_bytes(), document.to_bytes())
        self.assertEqual(queries(Model.from_json_document(m.to_json_document())), queries(m))
        original = m.field_provenance(TITLE, ('lines', 0))
        m.collection('swmm:title').update('text', lines=tuple(reversed(m.title.lines)))
        self.assertEqual(m.field_provenance(TITLE, ('lines', 0)), original)
        self.assertTrue(m.inspect_field(TITLE, 'lines').changed)

    def test_annotation_blocks_are_derived_original_lines_not_decoded_tokens(self):
        m = load(fixture(variant='annotation'))
        for name in ('key', 'json_text'):
            provenance = m.field_provenance(NOTE, name)
            self.assertEqual(provenance.status, 'derived')
            self.assertGreater(len(provenance.declarations), 2)
            for d in provenance.declarations:
                self.assertEqual(d.tokens, ())
                self.assertTrue(d.raw_text.startswith(';@easysewer.annotation/1 '))
                self.assertTrue(d.contributes)
        self.assertEqual(m.inspect_field(NOTE, 'json_text').semantics.default.status, 'required')
        source = '[TITLE]\n;@easysewer.annotation/9 future\n;@easysewer.annotation/1 bad\n'
        other = load(source, strict=False)
        self.assertFalse(other.collection('easysewer:metadata'))
        self.assertEqual(other.field_provenance(TITLE, 'lines').status, 'explicit')
        self.assertEqual(other.document.text, source)
        with self.assertRaises(ValidationError): other.to_document()
        invalid = Model(); invalid.collection('swmm:title').add(ProjectTitle(lines=(m.document.find_sections('TITLE')[0].lines[0].content,)))
        self.assertEqual(invalid.inspect_field(TITLE, 'lines').semantics.effective.status, 'invalid')

    def test_tags_last_assignment_and_profiles_original_chunk_indexes(self):
        m = load(fixture())
        tag = Ref(collection='swmm:tags', key=('swmm:nodes', 'J'))
        d = m.field_provenance(tag, 'text').declarations
        self.assertEqual([v.tokens[0].raw for v in d], ['old', '"new tag"'])
        self.assertEqual([v.contributes for v in d], [False, True])
        self.assertTrue(all(v.raw_text is None and v.span is None for v in d))
        self.assertEqual(m.field_provenance(tag, ('target', 'collection')).status, 'derived')
        profile = Ref(collection='swmm:profiles', key='North area')
        names = m.field_provenance(profile, 'name').declarations
        self.assertEqual([v.contributes for v in names], [True, False])
        self.assertEqual([v.role for v in names], ['value', 'retained'])
        links = m.field_provenance(profile, 'links').declarations
        self.assertEqual([len(d.tokens) for d in links], [6, 1])
        for i in range(7):
            d, = m.field_provenance(profile, ('links', i, 'key')).declarations
            self.assertEqual(d.line, links[0 if i < 6 else 1].line)
            self.assertEqual(d.tokens[0].raw, 'P')
        self.assertEqual(m.inspect_field(profile, ('links', 0)).provenance.status, 'untracked_path')

    def test_failed_known_rows_block_completeness_without_hiding_declarations(self):
        for tail, collection, key, path in (
            ('[TAGS]\nNode J\n', 'swmm:tags', ('swmm:nodes', 'J'), ('text',)),
            ('[PROFILES]\n"North area"\n', 'swmm:profiles', 'North area', ('links',)),
            ('[PROFILES]\n"North area" "bad\n', 'swmm:profiles', 'North area', ('links',)),
        ):
            with self.subTest(tail=tail):
                m = load(fixture() + tail, strict=False)
                p = m.field_provenance(Ref(collection=collection, key=key), path)
                self.assertEqual(p.status, 'unknown')
                self.assertTrue(p.declarations)

    def test_lifecycle_rename_rollback_and_missing_references(self):
        m = load(fixture()); before = queries(m)
        with self.assertRaises(ValidationError): m.links.remove('P')
        self.assertEqual(queries(m), before)
        profile = Ref(collection='swmm:profiles', key='North area')
        original = m.field_provenance(profile, 'links')
        m.profiles.rename('North area', 'Renamed'); m.links.rename('P', 'Changed')
        renamed = Ref(collection='swmm:profiles', key='Renamed')
        self.assertEqual(m.field_provenance(renamed, 'links').declarations, original.declarations)
        self.assertEqual(m.field_provenance(renamed, 'links').original, original.original)
        self.assertTrue(m.inspect_field(renamed, 'links').changed)
        self.assertEqual(queries(m.copy()), queries(m))
        self.assertEqual(queries(Model.from_json_document(m.to_json_document())), queries(m))
        missing = load('[TAGS]\nNode Missing tag\n[PROFILES]\nProfile Missing\n', strict=False)
        self.assertEqual(missing.inspect_field(Ref(collection='swmm:tags', key=('swmm:nodes', 'Missing')), 'text').semantics.effective.status, 'invalid')
        self.assertEqual(missing.inspect_field(Ref(collection='swmm:profiles', key='Profile'), 'links').semantics.effective.status, 'invalid')

    def test_programmatic_empty_and_extension_facts_do_not_invent_source(self):
        m = Model(); m.collection('swmm:title').add(ProjectTitle())
        info = m.inspect_field(TITLE, 'lines')
        self.assertEqual(info.semantics.effective.value, ())
        self.assertEqual(info.semantics.default.value, ())
        self.assertEqual(info.provenance.status, 'created')
        @dataclass(frozen=True, kw_only=True)
        class Extended(JsonAnnotation):
            extra: str = 'custom'
        m.collection('easysewer:metadata').add(Extended(key='test:note', json_text='{}'))
        self.assertEqual(m.inspect_field(NOTE, 'json_text').semantics.effective.status, 'unknown')

    def test_raw_binding_contract_rejects_foreign_unclaimed_and_duplicate_lines(self):
        source = InpDocument.from_text('[TITLE]\nTitle\n')
        for mutate in (
            lambda bindings: tuple(replace(b, line=999) for b in bindings),
            lambda bindings: tuple(replace(b, owner=NOTE) for b in bindings),
            lambda bindings: tuple(replace(b, path=('absent',)) for b in bindings),
            lambda bindings: (*bindings, bindings[0]),
        ):
            class Broken(ProjectCodec):
                def decode(self, document, profile):
                    result = super().decode(document, profile)
                    return replace(result, value=replace(result.value, field_bindings=mutate(result.value.field_bindings)))
            schema = ModelSchema(); schema.register(ProjectCodec.descriptor, Broken())
            with self.subTest(mutate=mutate), self.assertRaises(RegistryError):
                Model.from_document(source, schema=schema)
        from test_field_inspection_v2 import FieldSensorCodec, sensor_schema
        class UndeclaredRaw(FieldSensorCodec):
            def decode(self, document, profile):
                result = super().decode(document, profile)
                return replace(result, value=replace(result.value, field_bindings=tuple(
                    FieldLineBinding(owner=b.owner, path=b.path, line=b.line) for b in result.value.field_bindings)))
        with self.assertRaisesRegex(RegistryError, 'raw section'):
            Model.from_document(InpDocument.from_text('[JUNCTIONS]\nJ 0\n[SENSORS]\nS J 3\n'), schema=sensor_schema(UndeclaredRaw()))
        for changes in ({'line': 0}, {'line': True}, {'role': 'guessed'}, {'contributes': 1}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                FieldLineBinding(**(dict(owner=TITLE, path=('lines',), line=2) | changes))

    def test_raw_cross_owner_requires_derived_role_and_reports_real_owner(self):
        m = load('[TITLE]\nTitle\n'); m.set_annotation('test:note', {'value': 1})
        document = m.to_document()
        for role in ('value', 'derived'):
            class CrossOwner(ProjectCodec):
                def decode(self, document, profile):
                    result = super().decode(document, profile)
                    line = next(b.line for b in result.value.field_bindings if b.owner == NOTE)
                    bindings = tuple(replace(b, line=line, role=role) if b.owner == TITLE else b
                                     for b in result.value.field_bindings)
                    return replace(result, value=replace(result.value, field_bindings=bindings))
            schema = ModelSchema(); schema.register(ProjectCodec.descriptor, CrossOwner())
            if role == 'value':
                with self.assertRaises(RegistryError): Model.from_document(document, schema=schema)
            else:
                derived = Model.from_document(document, schema=schema)
                declaration, = derived.field_provenance(TITLE, 'lines').declarations
                self.assertEqual(declaration.source_owners, (NOTE.canonical,))
                self.assertTrue(declaration.raw_text.startswith(';@easysewer.annotation/1'))

    def test_invalid_raw_title_retains_document_and_diagnostics_without_writer_failure(self):
        for bad in ('plain\x00text', '; comment\x00text', ';@easysewer.annotation/1 bad\x00'):
            for prefix in ('', 'Valid title\n'):
                with self.subTest(bad=bad, prefix=prefix):
                    text = '[TITLE]\n' + prefix + bad + '\n'
                    model = load(text, strict=False)
                    self.assertEqual(model.document.text, text)
                    self.assertTrue(model.validate().errors)
                    self.assertTrue(any(d.span and d.span.line == (3 if prefix else 2)
                                        for d in model.validate().errors))
                    self.assertFalse(model.metadata)
                    if prefix:
                        self.assertEqual(model.title.lines, ('Valid title',))
                        self.assertEqual(model.field_provenance(TITLE, 'lines').status, 'unknown')
                        self.assertEqual(model.field_provenance(TITLE, ('lines', 0)).status, 'explicit')
                    else:
                        self.assertFalse(model.collection('swmm:title'))
                    with self.assertRaises(ValidationError): model.to_document()
                    with self.assertRaises(ValidationError): load(text, strict=True)


if __name__ == '__main__': unittest.main()
