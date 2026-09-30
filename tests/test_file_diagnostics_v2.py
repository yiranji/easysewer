"""File declarations, external content and staging failures keep distinct evidence."""
from dataclasses import replace
import errno
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from easysewer.io.inp import InpDocument
from easysewer.io.interface_inspection import InterfaceInspection
from easysewer.model import Model
from easysewer.model.resources import FileTimeSeries
from easysewer.model.values import FileReference
from easysewer.model.file_resources import file_subject
from easysewer.runtime import check_files
from easysewer.runtime._preparation import inventory, stage, verify_resources
from easysewer.runtime._result_codec import Codec
from easysewer.validation import Diagnostic, DiagnosticSubject, SourceSpan, ValidationError, ValidationReport
from test_files_v2 import bind
from test_options_v2 import network

EVIDENCE = []


def parsed(root, *, output=False):
    model = network()
    bind(model, 'HOTSTART', 'USE', root / 'state.hsf')
    if output: bind(model, 'HOTSTART', 'SAVE', root / 'state.hsf')
    return Model.from_document(InpDocument.from_text(model.to_document().text,
        source=str(root / 'original.inp')), strict=True)


def noop(): pass


class FileDiagnosticTests(unittest.TestCase):
    def issue(self, report, code):
        value = next(d for d in report.diagnostics if d.code == code)
        self.assertIsNotNone(value.subject)
        self.assertEqual({v.subject for v in value.locations}, {value.subject, *value.related})
        codec = Codec(None)
        self.assertEqual(codec.decode(codec.encode(value)), value)
        EVIDENCE.append(dict(code=code, subject=value.subject.collection, path=value.subject.path,
            statuses=[v.status for v in value.locations]))
        return value

    def test_guard_sources_survive_copy_json_and_rejected_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            m = parsed(Path(directory))
            for model in (m, m.copy(), Model.from_json_document(m.to_json_document())):
                for operation, code in ((lambda: model.nodes.rename('J', 'Moved'), 'files.external_identity_dependency'),
                                        (lambda: model.reinterpret_units('CMS'), 'files.external_unit_dependency')):
                    before = model.to_json_document().to_bytes()
                    with self.assertRaises(ValidationError) as caught: operation()
                    d = self.issue(caught.exception.report, code)
                    self.assertEqual(d.subject.path, ('file', 'path'))
                    self.assertEqual(d.locations[0].status, 'current')
                    self.assertTrue(d.related)
                    self.assertEqual(model.to_json_document().to_bytes(), before)

    def test_missing_edited_and_programmatic_paths_are_not_current_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); m = parsed(root)
            d = self.issue(check_files(m).report, 'files.unavailable')
            self.assertEqual(d.span.source, str(root / 'original.inp'))
            value = m.files[('HOTSTART', 'USE')]
            m.files.replace(value.key, replace(value, file=replace(value.file, path=str(root / 'other.hsf'))))
            d = self.issue(check_files(m).report, 'files.unavailable')
            self.assertIsNone(d.span)
            self.assertEqual(d.locations[0].status, 'changed')
            m = network(); bind(m, 'HOTSTART', 'USE', root / 'none')
            self.assertEqual(self.issue(check_files(m).report, 'files.unavailable').locations[0].status, 'programmatic')

    def test_external_content_span_is_not_the_inp_declaration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / 'state.hsf').write_bytes(b'bad')
            m = parsed(root)
            external = Diagnostic(code='fixture.external', message='Bad external sample', field='samples[2]',
                subject=DiagnosticSubject(path=('samples', 2)), span=SourceSpan(line=7, column=2, end_column=4))
            def inspect(*args, **kwargs):
                return InterfaceInspection(format=kwargs['use'].format, status='invalid',
                    report=ValidationReport(diagnostics=(external,)))
            result = check_files(m, inspectors={'swmm:hotstart.interface': inspect})
            d = self.issue(result.report, 'fixture.external')
            self.assertEqual(d.span, replace(external.span, source=str(root / 'state.hsf')))
            self.assertEqual(d.subject.path, ('file', 'path'))
            self.assertEqual(d.locations[0].spans[0].source, str(root / 'original.inp'))
            self.assertEqual(result.checks[0].inspection.report.diagnostics, (d,))
            self.assertEqual(external.subject.path, ('samples', 2))

    def test_explicit_inspector_model_subject_and_candidate_evidence_survive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / 'state.hsf').write_bytes(b'data'); m = parsed(root)
            owner = DiagnosticSubject(collection='swmm:nodes', key='J', path=('elevation',))
            original = m._resolve_diagnostics(ValidationReport(diagnostics=(Diagnostic(code='fixture.bound',
                message='An explicit model diagnostic', subject=owner),))).diagnostics[0]
            m.nodes.update('J', elevation=1)
            def inspect(*args, **kwargs):
                return InterfaceInspection(format=kwargs['use'].format, status='invalid',
                    report=ValidationReport(diagnostics=(original,)))
            d = self.issue(check_files(m, inspectors={'swmm:hotstart.interface': inspect}).report, 'fixture.bound')
            self.assertEqual(d.subject, owner)
            self.assertEqual(d.locations[0], original.locations[0])
            self.assertEqual(d.related, (file_subject(m.file_uses()[0]),))

    def test_builtin_data_parser_retains_external_line_and_declaration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); data = root / 'series.dat'; data.write_bytes(b'; comment\n0:00 not-a-number\n')
            m = network(); m.timeseries.add(FileTimeSeries(id='T', file=FileReference(path=str(data))))
            m = Model.from_document(InpDocument.from_text(m.to_document().text, source=str(root / 'original.inp')))
            report = check_files(m).report
            d = self.issue(report, 'data.invalid_text')
            self.assertEqual(d.span.source, str(data))
            self.assertEqual(d.span.line, 2)
            self.assertEqual(d.subject.key, 'T')
            self.assertEqual(d.locations[0].spans[0].source, str(root / 'original.inp'))

    def test_shared_input_is_captured_once_and_both_real_consumers_are_related(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); data = root / 'series.dat'; data.write_bytes(b'0:00 1\n1:00 2\n')
            m = network()
            for name in ('T1', 'T2'): m.timeseries.add(FileTimeSeries(id=name, file=FileReference(path=str(data))))
            m = Model.from_document(InpDocument.from_text(m.to_document().text, source=str(root / 'original.inp')))
            plans = inventory(m, input_directory=root, working_directory=root)
            records, _, _ = stage(m, plans, root / 'work', 'assets', checkpoint=noop)
            self.assertEqual(len(records), 2)
            self.assertEqual(records[0].relative_path, records[1].relative_path)
            (root / 'work' / records[0].relative_path).write_bytes(b'changed')
            with self.assertRaises(ValidationError) as caught:
                verify_resources(records, root / 'work', checkpoint=noop, diagnostics=m._resolve_diagnostics)
            d = self.issue(caught.exception.report, 'run.resource_changed')
            self.assertEqual({d.subject.key, *(v.key for v in d.related)}, {'T1', 'T2'})
            self.assertEqual([v.status for v in d.locations], ['changed', 'changed'])

    def test_alias_collision_and_inspection_limit_address_real_consumers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / 'state.hsf').write_bytes(b'large')
            m = parsed(root, output=True)
            report = check_files(m, overwrite=True, max_bytes=1).report
            d = self.issue(report, 'files.path_collision')
            self.assertEqual(d.subject.key, ('HOTSTART', 'SAVE'))
            self.assertEqual(d.related[0].key, ('HOTSTART', 'USE'))
            self.assertEqual(self.issue(report, 'files.inspection_limit').locations[0].status, 'current')

    def test_export_and_resolution_failures_preserve_model(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); m = network(); bind(m, 'RDII', 'USE', 'unbound.rdii')
            m = Model.from_document(InpDocument.from_text(m.to_document().text, source=str(root / 'original.inp')))
            before = m.to_json_document().to_bytes()
            with self.assertRaises(ValidationError) as caught: m.to_inp(root / 'export.inp')
            self.assertEqual(self.issue(caught.exception.report, 'files.unbound_working_directory').subject.path,
                             ('file', 'base_directory'))
            with self.assertRaises(ValidationError) as caught: m.resolve_files()
            self.issue(caught.exception.report, 'files.resolve_path')
            self.assertEqual(m.to_json_document().to_bytes(), before)
            self.assertFalse((root / 'export.inp').exists())
            m = parsed(root)
            with self.assertRaises(ValidationError) as caught: m.to_inp(root / 'bad.inp', path_policy='bad')
            self.issue(caught.exception.report, 'files.rebase_path')
            self.assertFalse((root / 'bad.inp').exists())

    def test_inventory_rejections_and_operational_path_error_keep_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); m = parsed(root); use = m.file_uses()[0]
            for uses, code in (((use, use), 'run.duplicate_consumer'), ((), 'run.undeclared_consumer'),
                               ((replace(use, kind='directory'),), 'run.directory_adapter')):
                with patch.object(Model, 'file_uses', return_value=iter(uses)):
                    with self.assertRaises(ValidationError) as caught:
                        inventory(m, input_directory=root, working_directory=root)
                self.issue(caught.exception.report, code)
            failure = ValueError('Host mapping required')
            with patch('easysewer.runtime._preparation.host_path', side_effect=failure):
                with self.assertRaises(ValueError) as caught: inventory(m, input_directory=root, working_directory=root)
            self.assertIs(caught.exception, failure)
            self.issue(failure._easysewer_resource_diagnostics, 'run.resource_path')

    def test_capture_failures_keep_exception_errno_and_original_declaration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); m = parsed(root)
            plans = inventory(m, input_directory=root, working_directory=root)
            with self.assertRaises(ValidationError) as caught: stage(m.copy(), plans, root / 'missing', 'assets', checkpoint=noop)
            d = self.issue(caught.exception.report, 'run.missing_resource')
            self.assertEqual(d.locations[0].status, 'current')
            (root / 'state.hsf').write_bytes(b'original')
            for failure in (OSError(errno.ENOSPC, 'Disk full'), ValueError('Input changed while being captured')):
                with patch('easysewer.runtime._preparation.copy_input', side_effect=failure):
                    with self.assertRaises(type(failure)) as caught:
                        stage(m.copy(), plans, root / 'failed', 'assets', checkpoint=noop)
                self.assertIs(caught.exception, failure)
                d = self.issue(failure._easysewer_resource_diagnostics, 'run.resource_capture')
                self.assertEqual(d.span.source, str(root / 'original.inp'))
                if isinstance(failure, OSError): self.assertEqual(failure.errno, errno.ENOSPC)

    def test_optional_missing_source_is_resolved_before_staging_edit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); m = parsed(root)
            plans = inventory(m, input_directory=root, working_directory=root)
            plans = tuple(replace(p, use=replace(p.use, required=False)) for p in plans)
            _, _, issues = stage(m, plans, root / 'work', 'assets', checkpoint=noop)
            d = self.issue(ValidationReport(diagnostics=issues), 'run.optional_resource_missing')
            self.assertEqual(d.locations[0].status, 'current')
            self.assertEqual(d.span.source, str(root / 'original.inp'))
            self.assertTrue(m.files[('HOTSTART', 'USE')].file.path.startswith('assets/'))

    def test_staged_verification_keeps_history_and_all_shared_consumers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / 'state.hsf').write_bytes(b'original')
            m = parsed(root); plans = inventory(m, input_directory=root, working_directory=root)
            records, _, _ = stage(m, plans, root / 'work', 'assets', checkpoint=noop)
            # Two declarations can share a captured resource; all known owners
            # remain related even though its bytes need hashing only once.
            other = replace(records[0], owner=replace(records[0].owner, key=('RUNOFF', 'USE')))
            target = root / 'work' / records[0].relative_path
            target.write_bytes(b'modified')
            with self.assertRaises(ValidationError) as caught:
                verify_resources((*records, other), root / 'work', checkpoint=noop, diagnostics=m._resolve_diagnostics)
            d = self.issue(caught.exception.report, 'run.resource_changed')
            self.assertIsNone(d.span)
            self.assertEqual([v.status for v in d.locations], ['changed', 'absent'])
            self.assertEqual(d.locations[0].spans[0].source, str(root / 'original.inp'))
            target.unlink()
            with self.assertRaises(FileNotFoundError) as caught:
                verify_resources(records, root / 'work', checkpoint=noop, diagnostics=m._resolve_diagnostics)
            self.issue(caught.exception._easysewer_resource_diagnostics, 'run.resource_unavailable')


if __name__ == '__main__': unittest.main()
