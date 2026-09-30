"""Explicit token ownership and defaults are different observable contracts."""
from dataclasses import fields, replace
from datetime import date, time, timedelta
import unittest
from unittest.mock import patch

from easysewer.io.inp import InpDocument
from easysewer.io.inp.network import default_schema
from easysewer.io.json import JsonDocument
from easysewer.io.json.types import JsonField, JsonType
from easysewer.model import FieldFact, FieldSemantics, Model, Ref
from easysewer.model.options import Options
from easysewer.schema import FeatureDescriptor, RegistryError
from easysewer.schema.field_contracts import FieldRule
from easysewer.schema.option_profile import OPTION_DEFINITIONS
from easysewer.schema.structured import FieldBinding, FieldCoverage
from test_model_extensions import Sensor, SensorCodec
from test_options_v2 import ALL_OPTIONS
from test_scenario_v2 import portable, fields as changes
from easysewer.scenario import ScenarioPatch

OWNER = Ref(collection='swmm:options', key='settings')
PERIOD = '[OPTIONS]\nSTART_DATE 01/01/2020\nEND_DATE 01/02/2020\n'


def load(text=PERIOD):
    return Model.from_document(InpDocument.from_text(text, source='C:/fixtures/input.inp'))


def restore(model, schema=None):
    return Model.from_json_document(model.to_json_document(), schema=schema, strict=True)


def sensor_semantics(context):
    return FieldSemantics(unit=FieldFact(status='known', value='ft'),
        default=FieldFact(status='required'), effective=FieldFact(status='known', value=context.value))


class FieldSensorCodec(SensorCodec):
    field_rules = (FieldRule(value_type=Sensor, field='threshold', resolve=sensor_semantics),)

    def decode(self, document, profile):
        result = super().decode(document, profile)
        claims = tuple(FieldBinding(owner=Ref(collection='test:sensors', key=b.key[0]),
            path=('threshold',), line=b.line, tokens=(2,)) for b in result.value.bindings)
        coverage = tuple(FieldCoverage(owner=c.owner, path=c.path) for c in claims)
        return replace(result, value=replace(result.value, field_bindings=claims, field_coverage=coverage))


def sensor_schema(codec=None):
    schema = default_schema()
    schema.register(FeatureDescriptor(key='test:sensors', sections={'SENSORS'}), codec or FieldSensorCodec())
    schema.register_json(JsonType(key='test:sensor', value_type=Sensor, fields=(
        JsonField(name='id', attribute='id', shape=('string',)),
        JsonField(name='node', attribute='node', shape=('object', 'core:ref')),
        JsonField(name='limit', attribute='threshold', shape=('number',))), defaults=(('limit', 3.),)))
    return schema


class FieldInspectionTests(unittest.TestCase):
    def test_all_options_have_explicit_exact_tokens_and_separate_defaults(self):
        model = load(ALL_OPTIONS)
        self.assertEqual(len(OPTION_DEFINITIONS), 43)
        for definition in OPTION_DEFINITIONS:
            with self.subTest(field=definition.field):
                info = model.inspect_field(OWNER, definition.field)
                self.assertEqual(info.path, (definition.field,))
                self.assertEqual(info.provenance.status, 'explicit')
                self.assertFalse(info.changed)
                self.assertEqual(info.constructor_default, FieldFact(status='known', value=None))
                self.assertEqual(info.semantics.default.value, model.profile.option_default(definition.field))
                self.assertEqual(info.semantics.effective.status, 'known')
                declaration, = info.provenance.declarations
                token, = declaration.tokens
                self.assertEqual(model.document.text[token.start:token.end], token.raw)
                self.assertEqual(token.span.source, 'C:/fixtures/input.inp')
        directory = model.inspect_field(OWNER, 'temp_directory')
        self.assertEqual(directory.provenance.declarations[0].tokens[0].raw, '"temporary files"')
        self.assertTrue(directory.provenance.declarations[0].tokens[0].quoted)

    def test_omission_zero_and_equal_explicit_default_have_different_provenance(self):
        absent = load().inspect_field(OWNER, 'rule_step')
        zero = load(PERIOD+'RULE_STEP 0\n').inspect_field(OWNER, 'rule_step')
        self.assertEqual(absent.provenance.status, 'omitted')
        self.assertEqual(zero.provenance.status, 'explicit')
        self.assertIsNone(absent.value)
        self.assertEqual(zero.value, timedelta())
        self.assertEqual(absent.semantics.effective.value, zero.semantics.effective.value)
        self.assertEqual(zero.semantics.unit.value, 's')
        explicit = load(PERIOD+'FLOW_UNITS CFS\n').inspect_field(OWNER, 'flow_units')
        self.assertEqual(explicit.value, explicit.semantics.default.value)
        self.assertEqual(explicit.provenance.status, 'explicit')

    def test_repeated_assignments_retain_original_tokens_and_effective_contributors(self):
        model = load(PERIOD+'RULE_STEP .5\nRULE_STEP 0 ; final\nFLOW_ROUTING KW\nFLOW_ROUTING NONE\n')
        rows = model.field_provenance(OWNER, 'rule_step').declarations
        self.assertEqual([d.tokens[0].raw for d in rows], ['.5', '0'])
        self.assertEqual([d.contributes for d in rows], [False, True])
        routing = model.inspect_field(OWNER, 'flow_routing')
        self.assertEqual(routing.value, 'KINWAVE')
        self.assertEqual([d.role for d in routing.provenance.declarations], ['value', 'retained'])
        self.assertEqual([d.contributes for d in routing.provenance.declarations], [True, False])
        ignore = model.inspect_field(OWNER, 'ignore_routing')
        self.assertTrue(ignore.value)
        self.assertEqual(ignore.provenance.status, 'derived')
        self.assertEqual(ignore.provenance.declarations[0].tokens[0].raw, 'NONE')
        overridden = load(PERIOD+'FLOW_ROUTING NONE\nIGNORE_ROUTING NO\n')
        self.assertEqual([d.contributes for d in overridden.field_provenance(OWNER, 'ignore_routing').declarations], [False, True])
        self.assertFalse(overridden.options.ignore_routing)

    def test_effective_adjustments_calendar_fallback_and_current_unit_context(self):
        model = load(PERIOD+'HEAD_TOLERANCE 0\nMAX_TRIALS 0\n')
        info = model.inspect_field(OWNER, 'head_tolerance')
        self.assertEqual(info.value, 0)
        self.assertEqual(info.semantics.default.value, 0)
        self.assertEqual(info.semantics.effective.value, .005)
        self.assertIn('convergence', info.semantics.effective.reason)
        self.assertEqual(info.semantics.unit.value, 'ft')
        self.assertEqual(model.inspect_field(OWNER, 'max_trials').semantics.effective.value, 8)
        report = model.inspect_field(OWNER, 'report_start_date')
        self.assertIsNone(report.semantics.default.value)
        self.assertEqual(report.semantics.effective.value, date(2020, 1, 1))
        model.convert_units('CMS')
        converted = model.inspect_field(OWNER, 'head_tolerance')
        self.assertEqual(converted.semantics.unit.value, 'm')
        self.assertAlmostEqual(converted.semantics.effective.value, .005 * .3048)
        self.assertEqual(converted.provenance, info.provenance)

    def test_source_lifecycle_json_scenario_and_queries_do_not_render(self):
        model = load(PERIOD+'RULE_STEP 0\n')
        original = model.inspect_field(OWNER, 'rule_step')
        scenario = ScenarioPatch(operations=(changes('options', 'settings', rule_step=timedelta(seconds=10)),))
        changed = scenario.apply(model).model
        for variant in (changed, changed.copy(), restore(changed)):
            with patch.object(InpDocument, 'to_bytes', side_effect=AssertionError('query rendered source')):
                info = variant.inspect_field(OWNER, 'rule_step')
                self.assertEqual(info.provenance, original.provenance)
                self.assertTrue(info.changed)
        with self.assertRaises(RuntimeError):
            with model.transaction():
                model.update_options(rule_step=timedelta(seconds=5))
                raise RuntimeError('rollback')
        self.assertEqual(model.inspect_field(OWNER, 'rule_step'), original)
        collection = model.collection('swmm:options')
        old = collection['settings']; collection.remove('settings'); collection.add(old)
        self.assertEqual(restore(model).inspect_field(OWNER, 'rule_step').provenance.status, 'created')

    def test_old_portable_and_malformed_input_never_claim_an_omitted_token(self):
        model = load(PERIOD+'RULE_STEP bad\n')
        info = model.inspect_field(OWNER, 'rule_step')
        self.assertEqual(info.provenance.status, 'unknown')
        self.assertFalse(info.provenance.declarations)
        valid = load()
        data = valid.to_json_document().data; data['source'].pop('origins')
        old = Model.from_json_document(JsonDocument.from_data(data))
        for variant in (old, portable(valid)):
            self.assertEqual(variant.inspect_field(OWNER, 'rule_step').provenance.status, 'untracked')
        malformed = load(PERIOD+'RULE_STEP "unterminated\n')
        self.assertEqual(malformed.field_provenance(OWNER, 'rule_step').status, 'unknown')

    def test_invalid_drafts_and_invalid_query_paths_have_explicit_outcomes(self):
        model = load()
        model.collection('swmm:options').update('settings', max_trials=True)
        info = model.inspect_field(OWNER, 'max_trials')
        self.assertEqual(info.semantics.effective.status, 'invalid')
        self.assertTrue(info.semantics.diagnostics)
        for path in ('absent', ('rule_step', 'days')):
            with self.assertRaises(KeyError): model.inspect_field(OWNER, path)
        for path in ((), ('_store',), (True,), (-1,), ['rule_step']):
            with self.assertRaises(ValueError): model.inspect_field(OWNER, path)
        with self.assertRaises(TypeError): model.inspect_field('settings', 'rule_step')
        with self.assertRaises(KeyError): model.inspect_field(Ref(collection='swmm:options', key='absent'), 'rule_step')

    def test_independent_codec_registered_defaults_rename_and_temporary_absence(self):
        schema = sensor_schema()
        model = Model.from_document(InpDocument.from_text('[JUNCTIONS]\nJ 0\n[SENSORS]\nS J 3\n'), schema=schema, strict=True)
        owner = Ref(collection='test:sensors', key='S')
        before = model.inspect_field(owner, 'threshold')
        self.assertEqual(before.constructor_default.status, 'required')
        self.assertEqual(before.json_default, FieldFact(status='known', value=3.))
        self.assertEqual(before.semantics.default.status, 'required')
        self.assertEqual(before.provenance.status, 'explicit')
        model.collection('test:sensors').rename('S', 'Renamed')
        owner = Ref(collection='test:sensors', key='Renamed')
        model.nodes.rename('J', 'NewNode')
        opaque = Model.from_json_document(model.to_json_document())
        restored = restore(opaque, schema)
        result = restored.inspect_field(owner, 'threshold')
        self.assertEqual(result.provenance.original.key, 'S')
        self.assertEqual(result.provenance.declarations, before.provenance.declarations)
        self.assertFalse(result.changed)

    def test_malformed_extension_bindings_are_rejected_before_a_source_snapshot_exists(self):
        source = InpDocument.from_text('[JUNCTIONS]\nJ 0\n[SENSORS]\nS J 3\n')
        for mutation in (
            lambda b: replace(b, tokens=(30,)),
            lambda b: replace(b, line=2),
            lambda b: replace(b, owner=Ref(collection='swmm:nodes', key='J')),
            lambda b: replace(b, path=('missing',)),
        ):
            class Broken(FieldSensorCodec):
                def decode(self, document, profile):
                    result = super().decode(document, profile)
                    return replace(result, value=replace(result.value,
                        field_bindings=tuple(mutation(b) for b in result.value.field_bindings)))
            with self.subTest(mutation=mutation), self.assertRaises(RegistryError):
                Model.from_document(source, schema=sensor_schema(Broken()))
        class Duplicate(FieldSensorCodec):
            field_rules = FieldSensorCodec.field_rules * 2
        with self.assertRaises(RegistryError): sensor_schema(Duplicate())
        with self.assertRaises(ValueError): FieldBinding(owner=OWNER, path=('rule_step',), line=1, tokens=(True,))

    def test_registry_conflicts_are_atomic_and_profile_rules_are_explicit(self):
        schema = sensor_schema()
        before = schema.bindings
        with self.assertRaises(RegistryError):
            schema.register(FeatureDescriptor(key='test:conflict', sections={'SENSORS'}), FieldSensorCodec())
        self.assertEqual(schema.bindings, before)
        snapshot = schema.snapshot()
        self.assertIsNotNone(snapshot.field_rule(Sensor, 'threshold', load().profile))
        future = replace(load().profile, key='test:future')
        self.assertIsNone(snapshot.field_rule(Sensor, 'threshold', future))
        with self.assertRaises(TypeError): FieldRule(value_type=Sensor, field='missing', resolve=sensor_semantics)
        with self.assertRaises(TypeError): FieldSemantics(diagnostics=('not a diagnostic',))
        with self.assertRaises(ValueError): FieldFact(status='unknown', value=0)

    def test_known_malformed_override_keeps_partial_declarations_but_no_completeness_claim(self):
        model = load(PERIOD+'RULE_STEP 0\nRULE_STEP bad\n')
        source = model.field_provenance(OWNER, 'rule_step')
        self.assertEqual(source.status, 'unknown')
        self.assertEqual(source.declarations[0].tokens[0].raw, '0')
        self.assertEqual(source.value.value, timedelta())
        malformed = load(PERIOD+'FLOW_ROUTING UNKNOWN\n')
        for name in ('flow_routing', 'ignore_routing'):
            self.assertEqual(malformed.field_provenance(OWNER, name).status, 'unknown')

    def test_unnamed_nested_items_are_not_given_invented_current_identity(self):
        model = load('[LABELS]\n1 2 "same"\n3 4 "same"\n')
        owner = Ref(collection='swmm:labels', key='layer')
        info = model.inspect_field(owner, ('entries', 0, 'text'))
        self.assertEqual(info.provenance.status, 'untracked_path')
        original = model.field_provenance(owner, ('entries', 0, 'position', 'x'))
        self.assertEqual(original.value.value, 1)
        self.assertEqual(original.status, 'explicit')  # Source indexes are now declared; current identity remains unknown.
        model.collection('swmm:labels').update('layer', entries=tuple(reversed(model.collection('swmm:labels')['layer'].entries)))
        self.assertEqual(model.inspect_field(owner, ('entries', 0, 'position', 'x')).value, 3)
        self.assertEqual(model.field_provenance(owner, ('entries', 0, 'position', 'x')), original)


if __name__ == '__main__':
    unittest.main()
