"""Validation boundaries retain truthful sources without committing bad drafts."""
from dataclasses import dataclass, field, replace
from datetime import date, time, timedelta
import unittest
from unittest.mock import patch

from easysewer.io.inp import InpDocument
from easysewer.model import Model, Ref
from easysewer.model.diagnostics import DiagnosticResolver
from easysewer.model.options import DayTime
from easysewer.runtime._result_codec import Codec
from easysewer.validation import Diagnostic, DiagnosticSubject, ValidationError, ValidationReport
from test_options_v2 import network
from test_quality_v2 import quality_model

EVIDENCE = []


def parsed(model=None, extra=''):
    return Model.from_document(InpDocument.from_text((model or network()).to_document().text + extra,
        source='boundary-original.inp'), strict=False)


def subject(collection, key, *path):
    return DiagnosticSubject(collection='swmm:' + collection, key=key, path=path)


class BoundaryDiagnosticTests(unittest.TestCase):
    def check(self, issue):
        self.assertIsNotNone(issue.subject)
        self.assertEqual({v.subject for v in issue.locations}, {issue.subject, *issue.related})
        codec = Codec(None)
        self.assertEqual(codec.decode(codec.encode(issue)), issue)
        EVIDENCE.append(dict(code=issue.code, collection=issue.subject.collection,
            path=issue.subject.path, statuses=[v.status for v in issue.locations]))
        return issue

    def rejected(self, model, operation, code):
        before = model.to_json_document().to_bytes()
        revision = model._store.revision
        with self.assertRaises(ValidationError) as caught:
            operation()
        self.assertEqual(model._store.revision, revision)
        self.assertEqual(model.to_json_document().to_bytes(), before)
        return self.check(next(d for d in caught.exception.report.errors if d.code == code))

    def test_run_calendar_relations_and_original_vs_edited_sources(self):
        m = parsed()
        m.update_options(end_time=time())
        issue = self.check(next(d for d in m.validate(for_run=True).errors if d.code == 'options.invalid_period'))
        self.assertEqual(issue.subject, subject('options', 'settings', 'end_date'))
        self.assertEqual([v.status for v in issue.locations], ['current', 'current', 'omitted', 'changed'])
        self.assertIn(subject('options', 'settings', 'end_time'), issue.related)
        self.assertEqual(Model.from_json_document(m.to_json_document()).validate(for_run=True), m.validate(for_run=True))
        m = parsed()
        m.update_options(report_start_date=date(2020, 1, 2), report_start_time=time())
        issue = self.check(next(d for d in m.validate(for_run=True).errors if d.code == 'options.invalid_report_start'))
        self.assertEqual(issue.locations[0].status, 'changed')
        self.assertIsNone(issue.span)

    def test_report_step_calendar_and_adjustment_dependencies(self):
        m = parsed()
        m.update_options(report_step=timedelta(seconds=1))
        issue = self.check(next(d for d in m.validate(for_run=True).errors if d.code == 'options.report_step_too_small'))
        self.assertIn(subject('options', 'settings', 'routing_step'), issue.related)
        m = parsed()
        m.update_options(dry_step=timedelta(seconds=1), routing_step=timedelta(seconds=400),
            minimum_step=timedelta(seconds=500))
        warnings = [d for d in m.validate(for_run=True).diagnostics if d.code == 'options.effective_adjustment']
        self.assertEqual({d.subject.path[0] for d in warnings},
            {'report_step', 'dry_step', 'routing_step', 'minimum_step', 'min_surface_area', 'head_tolerance', 'max_trials'})
        for issue in warnings: self.check(issue)
        routing = next(d for d in warnings if d.subject.path == ('routing_step',))
        self.assertEqual(routing.related, (subject('options', 'settings', 'wet_step'),))
        # The bootstrap snapshot still serializes the untouched profile report.
        low = m.effective_options
        legacy = Codec(None, result_version='1.1')
        self.assertEqual(legacy.decode(legacy.encode(low)), low)
        self.assertTrue(all(d.subject is None and not d.locations for d in low.report.diagnostics))

    def test_calendar_overflow_model_and_effective_boundary(self):
        m = parsed()
        m.update_options(end_date=date.max, end_time=DayTime(day_offset=1))
        issue = self.check(next(d for d in m.validate(for_run=True).errors if d.code == 'options.calendar_overflow'))
        self.assertEqual(issue.subject, subject('options', 'settings', 'end_time'))
        self.assertEqual(issue.related, (subject('options', 'settings', 'end_date'),))
        with self.assertRaises(ValidationError) as caught: _ = m.effective_options
        self.assertEqual(caught.exception.report.errors[0], issue)

    def test_rejected_options_draft_does_not_blame_the_valid_old_token(self):
        m = parsed()
        issue = self.rejected(m, lambda: m.update_options(routing_step=timedelta(seconds=-1)), 'options.invalid_value')
        self.assertEqual(issue.subject, subject('options', 'settings', 'routing_step'))
        self.assertEqual(issue.locations[0].status, 'changed')
        self.assertTrue(issue.locations[0].spans)
        self.assertIsNone(issue.span)
        current = DiagnosticResolver(m).location(issue.subject)
        self.assertEqual(current.status, 'current')
        self.assertEqual(current.spans, issue.locations[0].spans)
        m = Model()
        issue = self.rejected(m, lambda: m.update_options(routing_step=timedelta(seconds=-1)), 'options.invalid_value')
        self.assertEqual(issue.locations[0].status, 'programmatic')
        self.assertNotIn('settings', m.collection('swmm:options'))

    def test_context_changes_are_candidate_bound_on_live_views_and_copies(self):
        m = parsed()
        for model in (m, m.copy(), Model.from_json_document(m.to_json_document())):
            view = model.collection('swmm:options')
            issue = self.rejected(model, lambda: view.update('settings', flow_units='CMS'),
                'options.context_change_requires_intent')
            self.assertEqual(issue.subject, subject('options', 'settings', 'flow_units'))
            self.assertEqual(issue.locations[0].status, 'changed')
            self.assertIsNone(issue.span)
        m.reinterpret_units('CMS')
        self.assertEqual(m.options.flow_units, 'CMS')

    def test_pollutant_guard_has_candidate_and_actual_consumers(self):
        m = parsed(quality_model())
        issue = self.rejected(m, lambda: m.pollutants.update('Q0', units='UG/L'), 'quality.units_context')
        self.assertEqual(issue.subject, subject('pollutants', 'Q0', 'units'))
        self.assertEqual(issue.locations[0].status, 'changed')
        self.assertTrue(issue.related)
        self.assertEqual(set(issue.related), {DiagnosticSubject(collection=u.owner.collection,
            key=u.owner.key, path=u.path) for u in m.referenced_by(Ref(collection='swmm:pollutants', key='Q0'))})
        self.assertIsNone(issue.span)

    def test_referenced_deletion_uses_typed_paths_and_never_display_id_guessing(self):
        m = parsed()
        m.nodes.rename('J', 'P')  # Same displayed ID as a link remains a different owner.
        issue = self.rejected(m, lambda: m.nodes.remove('P'), 'model.object_in_use')
        self.assertEqual(issue.subject, subject('nodes', 'P'))
        self.assertEqual(issue.related, (subject('links', 'P', 'inlet'),))
        self.assertEqual(issue.locations[0].original.key, 'J')
        m.nodes.remove('P', cascade=True)
        self.assertNotIn('P', m.nodes)
        self.assertNotIn('P', m.links)

    def test_opaque_guard_keeps_the_unparsed_source_and_target_separate(self):
        m = parsed(extra='\n[FUTURE]\nJ unknown-value\n')
        for model in (m, m.copy(), Model.from_json_document(m.to_json_document())):
            for operation in (lambda: model.nodes.rename('J', 'Changed'),
                              lambda: model.nodes.move('J'),
                              lambda: model.nodes.remove('J', cascade=True)):
                issue = self.rejected(model, operation, 'model.opaque_reference_risk')
                self.assertEqual(issue.subject, subject('nodes', 'J'))
                self.assertEqual(issue.span.source, 'boundary-original.inp')
                self.assertEqual(model.document.lines[issue.span.line - 1].content, 'J unknown-value')
                self.assertNotIn(issue.span, issue.locations[0].spans)

    def test_local_settings_and_transaction_rollback_keep_candidate_evidence(self):
        m = parsed()
        issue = self.rejected(m, lambda: m.update_report(continuity='wrong'), 'model.invalid_field')
        self.assertEqual(issue.subject, subject('report', 'settings', 'continuity'))
        # Explicit collection edits retain their established deferred validation.
        def transaction():
            with m.transaction():
                m.collection('swmm:options').update('settings', routing_step=timedelta(seconds=-1))
        issue = self.rejected(m, transaction, 'options.invalid_value')
        self.assertEqual(issue.locations[0].status, 'changed')
        self.assertIsNone(issue.span)

    def test_draft_inspection_does_not_render_or_read_files(self):
        m = parsed()
        with patch.object(Model, 'to_document', side_effect=AssertionError('no render')), \
             patch('builtins.open', side_effect=AssertionError('no IO')):
            with self.assertRaises(ValidationError) as caught:
                m.update_options(routing_step=timedelta(seconds=-1))
        self.check(caught.exception.report.errors[0])

    def test_control_conversion_keeps_composite_owner_and_transitive_references(self):
        from test_controls_v2 import controlled
        m = parsed(controlled('VARIABLE DepthValue = NODE J DEPTH\nEXPRESSION Offset = DepthValue + 2\n'
            'RULE Offset\nIF Offset > 1\nTHEN CONDUIT P STATUS = OPEN\n'))
        issue = self.rejected(m, lambda: m.convert_units('CMS'), 'units.control_dimensions')
        self.assertEqual(issue.subject, subject('controls', ('EXPRESSION', 'Offset'), 'expression'))
        self.assertIn(subject('controls', ('VARIABLE', 'DepthValue')), issue.related)
        self.assertIn(subject('nodes', 'J'), issue.related)
        self.assertNotIn(subject('controls', ('RULE', 'Offset')), issue.related)
        self.assertEqual(issue.locations[0].status, 'current')

    def test_conversion_commit_error_preserves_candidate_before_rollback(self):
        m = network()
        m.reinterpret_units('CMS')
        m.nodes.update('J', elevation=8e307)
        m = parsed(m)
        issue = self.rejected(m, lambda: m.convert_units('CFS'), 'model.invalid_field')
        self.assertEqual(issue.subject, subject('nodes', 'J', 'elevation'))
        self.assertEqual(issue.locations[0].status, 'changed')
        self.assertTrue(issue.locations[0].spans)
        self.assertIsNone(issue.span)
        self.assertEqual(DiagnosticResolver(m).location(issue.subject).status, 'current')

    def test_pollutant_conversion_local_candidate_overflow_is_not_an_old_value_error(self):
        m = quality_model()
        m.pollutants.update('Q0', rdii_concentration=1e308)
        m = parsed(m)
        issue = self.rejected(m, lambda: m.convert_pollutant_units('Q0', 'UG/L'), 'model.invalid_field')
        self.assertEqual(issue.subject, subject('pollutants', 'Q0', 'rdii_concentration'))
        self.assertEqual(issue.locations[0].status, 'changed')
        self.assertTrue(issue.locations[0].spans)
        self.assertIsNone(issue.span)

    def test_pollutant_conversion_shared_resource_includes_all_consumer_paths(self):
        from test_pollutant_units_v2 import SensorCodec, QualitySensor, sensor_model, add_shared_series
        codec = SensorCodec()
        m, schema = sensor_model(codec)
        series = add_shared_series(m)
        m.collection('test:quality_sensors').update('Sensor', pollutant=Ref(collection='swmm:pollutants', key='Q1'),
            series=series, backup=series)
        issue = self.rejected(m, lambda: m.convert_pollutant_units('Q0', 'UG/L'), 'quality.unit_conversion')
        self.assertEqual(issue.subject, subject('timeseries', 'CONCENTRATION'))
        self.assertIn(DiagnosticSubject(collection='test:quality_sensors', key='Sensor', path=('series',)), issue.related)
        self.assertIn(DiagnosticSubject(collection='test:quality_sensors', key='Sensor', path=('backup',)), issue.related)
        self.assertIn(subject('pollutants', 'Q0', 'units'), issue.related)

    def test_offset_incomplete_conversion_retains_option_source(self):
        m = parsed(extra='\n[FUTURE]\nJ 3\n')
        issue = self.rejected(m, lambda: m.convert_link_offsets('ELEVATION'), 'offsets.incomplete_coverage')
        self.assertEqual(issue.subject, subject('options', 'settings', 'link_offsets'))
        self.assertEqual(issue.locations[0].status, 'omitted')
        issue = self.rejected(m, lambda: m.convert_units('CMS'), 'units.incomplete_coverage')
        self.assertEqual(issue.subject, subject('options', 'settings', 'flow_units'))

    def test_extension_numeric_errors_have_typed_nested_paths_without_json_assumptions(self):
        from easysewer.model import CollectionSpec
        @dataclass(frozen=True)
        class Unknown:
            coefficient: float
        @dataclass(frozen=True)
        class Record:
            id: str
            layers: tuple[Unknown, ...]
        m = network()
        m._store.register(CollectionSpec(key='test:unknown', record_type=Record, key_of=lambda row: row.id))
        m.collection('test:unknown').add(Record('J', (Unknown(1.),)))
        before = m._store.clone()
        with self.assertRaises(ValidationError) as caught: m.convert_units('CMS')
        self.assertEqual(m._store.revision, before.revision)
        self.assertEqual(m._store._records, before._records)
        issue = self.check(caught.exception.report.errors[0])
        self.assertEqual(issue.code, 'units.missing_dimension')
        self.assertEqual(issue.subject, DiagnosticSubject(collection='test:unknown', key='J', path=('layers', 0, 'coefficient')))
        self.assertEqual(issue.field, 'layers[0].coefficient')
        self.assertEqual(issue.locations[0].status, 'programmatic')

    def test_custom_recursive_conversion_requires_explicit_child_addresses(self):
        from easysewer.io.inp.network import default_schema
        from easysewer.model import CollectionSpec
        from easysewer.model.units import UnitTransform
        from easysewer.schema import DecodedFeature, FeatureDescriptor
        from easysewer.schema.structured import FeatureData
        @dataclass(frozen=True)
        class Child:
            coefficient: float
        @dataclass(frozen=True)
        class Record:
            id: str
            children: tuple[Child, ...]
        for named in (False, True):
            def convert(value, context):
                return replace(value, children=context.convert_value(value.children,
                    **({'path': ('children',)} if named else {})))
            class Extension:
                collections = (CollectionSpec(key='test:recursive', record_type=Record, key_of=lambda row: row.id),)
                unit_transforms = (UnitTransform(value_type=Record, convert=convert),)
                def decode(self, document, profile): return DecodedFeature(value=FeatureData())
                def encode(self, store, profile): return ()
                def validate(self, store, profile): return ()
            schema = default_schema()
            schema.register(FeatureDescriptor(key='test:recursive', sections={'RECURSIVE'}), Extension())
            m = Model(schema=schema)
            m.collection('test:recursive').add(Record('J', (Child(1.),)))
            before = m._store.clone()
            with self.assertRaises(ValidationError) as caught: m.convert_units('CMS')
            self.assertEqual(m._store._records, before._records)
            self.assertEqual(m._store.revision, before.revision)
            issue = self.check(caught.exception.report.errors[0])
            self.assertEqual(issue.subject.path, ('children', 0, 'coefficient') if named else ())

    def test_custom_conversion_rejections_and_unsupported_numeric_fields(self):
        from easysewer.io.inp.network import default_schema
        from easysewer.model import CollectionSpec
        from easysewer.model.units import UnitTransform
        from easysewer.schema import DecodedFeature, FeatureDescriptor
        from easysewer.schema.structured import FeatureData
        @dataclass(frozen=True)
        class Record:
            id: str
            fixed_length: float = field(default=1., init=False, metadata={'dimension': 'length'})
        @dataclass(frozen=True)
        class Unsupported:
            id: str
            coefficient: float = field(default=1., metadata={'dimension': 'future_dimension'})
        def fails(value, context): raise ValueError('adapter rejects this formula')
        for kind, code, callback in ((Record, 'units.immutable_field', None),
                (Unsupported, 'units.unsupported_dimension', None),
                (Record, 'units.invalid_transform', lambda value, context: 2),
                (Record, 'units.transform_failed', fails)):
            class Extension:
                collections = (CollectionSpec(key='test:conversion', record_type=kind, key_of=lambda row: row.id),)
                unit_transforms = () if callback is None else (UnitTransform(value_type=kind, convert=callback),)
                def decode(self, document, profile): return DecodedFeature(value=FeatureData())
                def encode(self, store, profile): return ()
                def validate(self, store, profile): return ()
            schema = default_schema()
            schema.register(FeatureDescriptor(key='test:conversion', sections={'CONVERSION'}), Extension())
            m = Model(schema=schema)
            m.collection('test:conversion').add(kind('J'))
            before = m._store.clone()
            with self.assertRaises(ValidationError) as caught: m.convert_units('CMS')
            issue = self.check(caught.exception.report.errors[0])
            self.assertEqual(issue.code, code)
            self.assertEqual(issue.subject.collection, 'test:conversion')
            self.assertEqual(m._store._records, before._records)
            self.assertEqual(m._store.revision, before.revision)
        m = parsed()
        m._profile = replace(m.profile, unit_rules=None)
        self.rejected(m, lambda: m.convert_units('CMS'), 'units.missing_profile')


if __name__ == '__main__': unittest.main()
