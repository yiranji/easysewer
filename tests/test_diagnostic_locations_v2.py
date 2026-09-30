"""Typed subjects, honest original locations and explicit archive migration."""
from dataclasses import replace
from datetime import timedelta
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from easysewer.io.inp import InpDocument
from easysewer.model import Model, Ref
from easysewer.model.diagnostics import DiagnosticResolver
from easysewer.validation import (Diagnostic, DiagnosticSubject, DiagnosticLocation,
    Severity, SourceSpan, ValidationError, ValidationReport)
from test_regulators_v2 import regulator_model

LEGACY = Path(__file__).parent/'fixtures/diagnostic_legacy'


def fixture(*, source='D:/models/input.inp', shape=False):
    model = regulator_model('SIDE', storage=False)
    model.update_options(flow_routing='KINWAVE')
    text = model.to_document().text
    if shape: text = text.replace('P CIRCULAR 1 0 0 0', 'P RECT_OPEN 1 2 0 0')
    return Model.from_document(InpDocument.from_text(text, source=source), strict=False)


def problem(model, code='regulator.requires_storage'):
    return next(d for d in model.validate(for_run=True).errors if d.code == code)


class DiagnosticLocationTests(unittest.TestCase):
    def test_topology_error_has_exact_inlet_and_related_sources(self):
        m = fixture(); issue = problem(m)
        self.assertEqual(issue.subject, DiagnosticSubject(collection='swmm:links', key='P', path=('inlet','key')))
        self.assertEqual(issue.span, SourceSpan(line=11, column=3, end_column=4, source='D:/models/input.inp'))
        self.assertEqual([v.status for v in issue.locations], ['current','context','current'])
        self.assertEqual([v.subject.collection for v in issue.locations], ['swmm:links','swmm:nodes','swmm:options'])
        digest = hashlib.sha256(m.document.text.encode()).hexdigest()
        self.assertTrue(all(v.source_sha256 == digest for v in issue.locations))
        self.assertEqual(issue.locations[-1].spans[0].line, 2)
        self.assertEqual(problem(Model.from_json_document(m.to_json_document())), issue)

    def test_aggregate_geometry_retains_all_declared_tokens(self):
        m = fixture(shape=True); issue = problem(m, 'regulator.invalid_shape')
        self.assertEqual(issue.subject.path, ('section','geometry'))
        self.assertIsNone(issue.span)
        self.assertEqual(issue.locations[0].status, 'current')
        self.assertTrue(issue.locations[0].spans)
        self.assertEqual({v.line for v in issue.locations[0].spans}, {13})

    def test_edits_rename_copy_and_rollback_preserve_truthful_history(self):
        m = fixture(); before = problem(m)
        copied = m.copy(); copied.links.rename('P', 'Renamed')
        renamed = problem(copied)
        self.assertEqual(renamed.subject.key, 'Renamed')
        self.assertEqual(renamed.locations[0].original.key, 'P')
        self.assertEqual(renamed.locations[0].status, 'changed'); self.assertIsNone(renamed.span)
        self.assertEqual(renamed.locations[0].spans, before.locations[0].spans)
        self.assertEqual(problem(Model.from_json_document(copied.to_json_document())), renamed)
        case_only = m.copy(); case_only.links.rename('P', 'p')
        self.assertEqual(problem(case_only).locations[0].status, 'changed')
        self.assertIsNone(problem(case_only).span)
        from easysewer.model.network import Junction
        m.nodes.add(Junction(id='Other', elevation=10))
        m.links.update('P', inlet=Ref(collection='swmm:nodes', key='Other'))
        edited = problem(m)
        self.assertEqual(edited.locations[0].status, 'changed'); self.assertIsNone(edited.span)
        self.assertEqual(edited.locations[1].status, 'programmatic')
        m.links.update('P', inlet=Ref(collection='swmm:nodes', key='J'))
        with self.assertRaises(ValidationError):
            with m.transaction(): m.links.update('P', section=None)
        self.assertEqual(problem(m), before)

    def test_same_named_namespaces_missing_targets_and_composite_keys(self):
        from easysewer.model.network import Junction
        from easysewer.model.inflows import FlowInflow
        m = fixture(); m.nodes.add(Junction(id='P', elevation=0))
        issue = problem(m)
        self.assertEqual(issue.subject.collection, 'swmm:links')
        self.assertEqual(issue.locations[0].spans[0].line, 11)
        m.inflows.add(FlowInflow(node=Ref(collection='swmm:nodes', key='Missing')))
        issue = next(d for d in m.validate().errors if d.code == 'model.unresolved_reference')
        self.assertEqual(issue.subject.key, ('Missing','FLOW'))
        self.assertEqual(issue.subject.path, ('node',))
        self.assertEqual([v.status for v in issue.locations], ['programmatic','absent'])

    def test_typed_local_paths_and_tuple_coordinates_are_not_invented(self):
        from easysewer.model.resources import InlineTimeSeries, SeriesPoint
        from easysewer.model.fields import validate_fields
        row = InlineTimeSeries(id='T', points=(SeriesPoint(time=timedelta(), value=float('nan')),))
        issue = next(iter(validate_fields(row)))
        self.assertEqual(issue.subject, DiagnosticSubject(path=('points',0,'value')))
        m = Model(); m.timeseries.add(row)
        issue = next(d for d in m.validate().errors if d.code == 'model.invalid_field')
        self.assertEqual(issue.subject.collection, 'swmm:timeseries')
        self.assertEqual(issue.subject.path, ('points',0,'value'))
        self.assertEqual(issue.locations[0].status, 'programmatic')
        parsed = Model.from_document(InpDocument.from_text('[TIMESERIES]\nT 0:00 1 1:00 2\n'))
        subject = DiagnosticSubject(collection='swmm:timeseries', key='T', path=('points',0,'value'))
        located = DiagnosticResolver(parsed).resolve(Diagnostic(code='example:tuple', message='position', subject=subject))
        self.assertEqual(located.locations[0].status, 'untracked'); self.assertFalse(located.locations[0].spans)

    def test_programmatic_omitted_unlocated_and_missing_paths(self):
        m = fixture(); resolver = DiagnosticResolver(m)
        subject = DiagnosticSubject(collection='swmm:options', key='settings', path=('threads',))
        self.assertEqual(resolver.location(subject).status, 'omitted')
        self.assertEqual(resolver.location(replace(subject, path=('future',))).status, 'untracked')
        issue = Diagnostic(code='extension:unbound', message='No inferred owner', object_id='P', field='inlet')
        self.assertIs(resolver.resolve(issue), issue)
        # A caller-supplied parser span retains its own source identity.
        supplied = replace(problem(m), span=SourceSpan(line=3,column=1,end_column=4,source='generated.inp'))
        self.assertEqual(resolver.resolve(supplied).span, supplied.span)
        self.assertIsNone(problem(fixture(source=None)).span.source)

    def test_duplicate_contributors_and_no_render_or_file_access(self):
        m = fixture()
        text = m.document.text + '[OPTIONS]\nFLOW_ROUTING STEADY\nFLOW_ROUTING KINWAVE\n'
        m = Model.from_document(InpDocument.from_text(text, source='/model/input.inp'))
        with patch.object(Model, 'to_document', side_effect=AssertionError('No rendering')), patch('builtins.open', side_effect=AssertionError('No IO')):
            issue = problem(m)
        self.assertEqual(len(issue.locations[-1].spans), 1)
        self.assertEqual(issue.locations[-1].spans[0].line, len(text.splitlines()))

    def test_subject_and_location_invariants(self):
        for values in ({'collection':'swmm:nodes'}, {'key':'J'}, {'collection':'bad','key':'J'},
                       {'collection':'swmm:nodes','key':('J',1)}, {'path':('x',True)}, {'path':['x']}, {'path':('_private',)}):
            with self.subTest(values=values), self.assertRaises((TypeError, ValueError)): DiagnosticSubject(**values)
        subject = DiagnosticSubject(collection='swmm:nodes', key='J')
        with self.assertRaises(ValueError): DiagnosticLocation(subject=subject, status='current')
        with self.assertRaises(ValueError): DiagnosticLocation(subject=subject, status='changed', spans=(SourceSpan(line=1,column=1,end_column=2),))
        location = DiagnosticLocation(subject=subject, status='programmatic')
        with self.assertRaises(ValueError): Diagnostic(code='x', message='x', locations=(location,))

    def test_independent_extension_local_validator_binds_its_own_namespace(self):
        from easysewer.io.inp.network import default_schema
        from easysewer.schema import FeatureDescriptor
        from test_model_extensions import Sensor, SensorCodec
        schema = default_schema(); schema.register(FeatureDescriptor(key='test:sensor', sections={'SENSORS'}), SensorCodec())
        model = Model(schema=schema)
        model.collection('test:sensors').add(Sensor(id='P', node=Ref(collection='swmm:nodes',key='P'), threshold=-1))
        issues = model.validate().errors
        local = next(d for d in issues if d.code == 'model.invalid_field')
        self.assertEqual(local.subject, DiagnosticSubject(collection='test:sensors',key='P',path=('threshold',)))
        self.assertEqual(local.locations[0].status, 'programmatic')
        missing = next(d for d in issues if d.code == 'model.unresolved_reference')
        self.assertEqual(missing.related[0].collection, 'swmm:nodes')
        self.assertEqual(missing.locations[-1].status, 'absent')

    def test_many_errors_reuse_shared_subjects_without_repeated_source_queries(self):
        import easysewer.model.diagnostics as implementation
        text = fixture().document.text
        original = 'P J O SIDE 0.2 0.65'
        shape = 'P CIRCULAR 1 0 0 0'
        text = text.replace(original, '\n'.join(original.replace('P ', f'P{i} ', 1) for i in range(200)))
        text = text.replace(shape, '\n'.join(shape.replace('P ', f'P{i} ', 1) for i in range(200)))
        model = Model.from_document(InpDocument.from_text(text,source='many.inp'))
        with patch.object(implementation, 'original_field', wraps=implementation.original_field) as query:
            issues = [d for d in model.validate(for_run=True).errors if d.code == 'regulator.requires_storage']
        self.assertEqual(len(issues), 200)
        queries = [(call.args[1], call.args[2]) for call in query.call_args_list]
        self.assertEqual(len(queries), len(set(queries)))
        self.assertLessEqual(sum(owner.collection != 'swmm:options' for owner, _ in queries), 200)
        self.assertEqual(len({d.locations[0].spans[0].line for d in issues}), 200)

    def test_actual_old_archives_and_checkpoint_decode_without_new_locations(self):
        from easysewer.runtime import RunResult, RunnerCheckpoint
        from test_result_archive_v2 import failure_result
        manifest = json.loads((LEGACY/'manifest.json').read_bytes())
        for name, digest in manifest['files'].items():
            self.assertEqual(hashlib.sha256((LEGACY/name).read_bytes()).hexdigest(), digest)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for version in ('1.0','1.1'):
                loaded = RunResult.load(LEGACY/('result-'+version))
                self.assertEqual(loaded, failure_result())
                self.assertIsNone(loaded.diagnostics.errors[0].subject)
                loaded.save(root/version)
                self.assertEqual(json.loads((root/version/'result.json').read_bytes())['schema_version'], '1.3')
                self.assertEqual(RunResult.load(root/version), loaded)
            old = RunnerCheckpoint.load(LEGACY/'runner-1.1')
            self.assertEqual(old.context.diagnostics.diagnostics[0].locations, ())
            restored = old.materialize(root/'rebuilt')
            self.assertEqual(restored.input_bytes, old.snapshot.input_bytes)

    def test_structured_results_cannot_be_silently_downgraded_or_smuggled(self):
        from easysewer.runtime import RunResult
        from easysewer.runtime._result_codec import Codec
        from test_result_archive_v2 import failure_result
        issue = problem(fixture())
        value = replace(failure_result(), diagnostics=ValidationReport(diagnostics=(issue,)))
        for version in ('1.0','1.1'):
            with self.assertRaisesRegex(ValueError, 'cannot retain'): Codec(None,result_version=version).encode(issue)
        encoded = Codec(None).encode(issue)
        for version in ('1.0','1.1'):
            with self.assertRaises(ValueError): Codec(None,result_version=version).decode(encoded)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); value.save(root/'archive')
            self.assertEqual(RunResult.load(root/'archive'), value)
            (root/'archive').rename(root/'moved')
            self.assertEqual(RunResult.load(root/'moved'), value)

    def test_table_json_and_nested_archive_preserve_locations_and_legacy(self):
        from easysewer.io.json import JsonDocument
        from easysewer.results.tables import ResultTable
        from easysewer.runtime._result_codec import Codec
        from easysewer.runtime.archive import _Blobs
        legacy = ResultTable.from_json_document(JsonDocument.from_bytes((LEGACY/'table-1.1.json').read_bytes()))
        self.assertIsNone(legacy.diagnostics.errors[0].subject)
        table = replace(legacy, diagnostics=ValidationReport(diagnostics=(problem(fixture()),)))
        self.assertEqual(ResultTable.from_json_document(table.to_json_document()), table)
        self.assertEqual(table.to_json_document().data['schema_version'], '1.2')
        with self.assertRaisesRegex(ValueError, 'cannot retain'): table.to_json_document(version='1.1')
        with tempfile.TemporaryDirectory() as directory:
            blobs = _Blobs(Path(directory),8*1024**3)
            encoded = Codec(blobs).encode(table)
            self.assertEqual(Codec(blobs).decode(encoded), table)
            for version in ('1.0','1.1'):
                with self.assertRaises(ValueError): Codec(blobs,result_version=version).decode(encoded)
                with self.assertRaises(ValueError): Codec(blobs,result_version=version).encode(table)
                old = Codec(blobs,result_version=version).encode(legacy)
                self.assertEqual(Codec(blobs,result_version=version).decode(old), legacy)

    def test_runner_rejection_preserves_structured_diagnostics_without_starting_solver(self):
        from easysewer.runtime import Runner, RunResult
        from test_runner_v2 import config
        with tempfile.TemporaryDirectory() as directory, patch('ctypes.CDLL', side_effect=AssertionError('No solver launch')):
            root = Path(directory); m = fixture(shape=True)
            result = Runner().run(m, config(root/'run', keep_failed_artifacts=False))
            self.assertEqual(result.status, 'rejected')
            found = next(d for d in result.diagnostics.errors if d.code == 'regulator.invalid_shape')
            self.assertTrue(found.locations)
            result.save(root/'saved')
            self.assertEqual(RunResult.load(root/'saved'), result)

    def test_new_runner_envelope_retains_warning_locations_and_old_format_is_strict(self):
        import test_runner_checkpoint_context_v2 as cp
        from easysewer.runtime import RunnerCheckpoint
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); snapshot = cp.fixture.snapshot(root)
            issue = replace(problem(fixture()), severity=Severity.WARNING)
            context = replace(cp.context(snapshot), diagnostics=ValidationReport(diagnostics=(issue,)))
            saved = cp.capture(cp.FakeSession(snapshot), context, root/'saved')
            self.assertEqual(json.loads(saved.manifest)['codec_version'], '1.2')
            self.assertEqual(RunnerCheckpoint.load(saved.directory).context, context)
            cp.rewrite(saved.directory, lambda data: data.update(codec_version='1.1'))
            with self.assertRaises(ValueError): RunnerCheckpoint.load(saved.directory)


if __name__ == '__main__': unittest.main()
