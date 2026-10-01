"""Release gates must fail closed and preserve the authored archive inputs."""

import ast
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]

# The workflow selects these exact methods with --tests. Keep this inventory
# independent of .github: the source distribution ships tests, not workflows.
WINDOWS_FILE_TESTS = (
    'test_recovery_journal_readers_v2.WindowsJournalReaders.test_nonsharing_reader_preserves_destination_and_retry',
    'test_recovery_journal_readers_v2.WindowsJournalReaders.test_readonly_destination_preserved',
    'test_recovery_journal_readers_v2.WindowsJournalReaders.test_extended_path_and_unicode_replacement',
    'test_recovery_journal_readers_v2.WindowsJournalReaders.test_native_replace_missing_target_and_source_sharing_denial',
    'test_recovery_journal_readers_v2.WindowsPathCodeUnits.test_existing_windows_surrogate_path_retains_exact_name',
    'test_windows_directory_handles_v2.WindowsDirectoryHandleTests.test_archive_moves_fail_with_nonsharing_handle_and_succeed_after_release',
    'test_windows_directory_handles_v2.WindowsDirectoryHandleTests.test_existing_directory_lock_rolls_back_prior_output_and_allows_fresh_retry',
    'test_windows_directory_handles_v2.WindowsDirectoryHandleTests.test_prepared_directory_lock_retains_recovery_then_cleans_after_release',
)


def load_tool(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'tools' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


audit_sdist = load_tool('audit_sdist_tests')
qualify = load_tool('qualify_release')
pure = load_tool('build_pure')
extractor = load_tool('extract_sdist')


class SourceArchiveExtractionTests(unittest.TestCase):
    filter_modes = (False, True) if hasattr(tarfile, 'data_filter') else (True,)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.archive = self.root / 'release.tar.gz'

    def write_archive(self, entries):
        with tarfile.open(self.archive, 'w:gz') as stream:
            for name, kind, raw in entries:
                member = tarfile.TarInfo(name)
                member.type = kind
                member.mode = 0o6777
                member.linkname = '../outside'
                member.size = len(raw) if kind == tarfile.REGTYPE else 0
                stream.addfile(member, io.BytesIO(raw) if kind == tarfile.REGTYPE else None)

    def entries(self):
        return [('release', tarfile.DIRTYPE, b''),
                ('release/tests/input.bin', tarfile.REGTYPE, b'\x00\xff\r\n'),
                ('release/empty', tarfile.DIRTYPE, b'')]

    def test_current_and_legacy_paths_preserve_files_and_directories(self):
        self.write_archive(self.entries())
        original_extractall = tarfile.TarFile.extractall
        for legacy in self.filter_modes:
            with self.subTest(legacy=legacy):
                output = self.root / str(legacy)
                data_filter = None if legacy else tarfile.data_filter
                with mock.patch.object(tarfile, 'data_filter', data_filter, create=True), \
                        mock.patch.object(tarfile.TarFile, 'extractall', autospec=True,
                                          side_effect=original_extractall) as extractall:
                    extractor.extract_sdist(self.archive, output)
                if legacy:
                    extractall.assert_not_called()
                    if os.name != 'nt':
                        self.assertEqual((output / 'release/tests/input.bin').stat().st_mode & 0o6000, 0)
                else:
                    self.assertEqual(extractall.call_count, 1)
                    self.assertEqual(extractall.call_args.kwargs['filter'], 'data')
                self.assertEqual((output / 'release/tests/input.bin').read_bytes(), b'\x00\xff\r\n')
                self.assertTrue((output / 'release/empty').is_dir())

    def assert_rejected_before_extraction(self, entries):
        self.write_archive(entries)
        for legacy in self.filter_modes:
            with self.subTest(legacy=legacy):
                output = self.root / 'must-not-exist'
                with mock.patch.object(tarfile, 'data_filter', None if legacy else tarfile.data_filter, create=True):
                    with self.assertRaises(ValueError):
                        extractor.extract_sdist(self.archive, output)
                self.assertFalse(output.exists())

    def test_traversal_absolute_and_windows_alias_paths_are_rejected(self):
        names = ('../outside', '/outside', 'release/../../outside', 'release/./file',
                 'release//file', 'C:/outside', 'C:outside', '\\\\server\\share\\file',
                 'release\\..\\outside', 'release/file:stream', 'release/NUL',
                 'release/CON.txt', 'release/file.', 'release/file ', 'release/a\nfile')
        for name in names:
            with self.subTest(name=name):
                self.assert_rejected_before_extraction(self.entries() + [(name, tarfile.REGTYPE, b'bad')])

    def test_links_devices_fifos_and_unknown_member_types_are_rejected(self):
        for kind in (tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.CHRTYPE,
                     tarfile.BLKTYPE, tarfile.FIFOTYPE, b'Z'):
            with self.subTest(kind=kind):
                self.assert_rejected_before_extraction(self.entries() + [('release/bad', kind, b'')])

    def test_empty_duplicate_case_alias_and_file_parent_archives_are_rejected(self):
        cases = [[], self.entries() + [self.entries()[1]],
                 self.entries() + [('Release/extra', tarfile.REGTYPE, b'')],
                 self.entries() + [('release/tests/input.bin/child', tarfile.REGTYPE, b'')],
                 [('release/file/child', tarfile.REGTYPE, b''), ('release/file', tarfile.REGTYPE, b'')]]
        for entries in cases:
            with self.subTest(entries=entries):
                self.assert_rejected_before_extraction(entries)

    def test_existing_destination_is_never_reused(self):
        self.write_archive(self.entries())
        for kind in ('file', 'directory', 'dangling-symlink'):
            with self.subTest(kind=kind):
                output = self.root / kind
                if kind == 'file':
                    output.write_bytes(b'keep')
                elif kind == 'directory':
                    output.mkdir()
                    (output / 'keep').write_bytes(b'keep')
                with mock.patch.object(Path, 'is_symlink', return_value=kind == 'dangling-symlink'):
                    with self.assertRaises(FileExistsError):
                        extractor.extract_sdist(self.archive, output)
                if kind == 'file':
                    self.assertEqual(output.read_bytes(), b'keep')
                elif kind == 'directory':
                    self.assertEqual(list(output.iterdir()), [output / 'keep'])

    def test_current_filter_errors_are_not_retried_without_a_filter(self):
        self.write_archive(self.entries())
        with mock.patch.object(tarfile, 'data_filter', lambda member, path: member, create=True), \
                mock.patch.object(tarfile.TarFile, 'extractall', side_effect=TypeError('filter failure')) as extractall:
            with self.assertRaisesRegex(TypeError, 'filter failure'):
                extractor.extract_sdist(self.archive, self.root / 'output')
        self.assertEqual(extractall.call_count, 1)
        self.assertEqual(list((self.root / 'output').iterdir()), [])

    def test_standalone_cli_works_without_checkout_or_installed_package(self):
        self.write_archive(self.entries())
        helper = self.root / 'extract_sdist.py'
        helper.write_bytes((ROOT / 'tools/extract_sdist.py').read_bytes())
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                output = self.root / ('cli-' + str(legacy))
                command = [sys.executable, '-I', '-B']
                if legacy:
                    command += ['-c', 'import runpy, sys, tarfile; tarfile.data_filter = None; '
                                'sys.argv = sys.argv[1:]; runpy.run_path(sys.argv[0], run_name="__main__")']
                command += [str(helper), '--archive', str(self.archive), '--output', str(output)]
                result = subprocess.run(command, cwd=self.root, capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual((output / 'release/tests/input.bin').read_bytes(), b'\x00\xff\r\n')


class QualificationSelectionTests(unittest.TestCase):
    def test_windows_file_gate_has_exactly_eight_existing_platform_guarded_methods(self):
        self.assertEqual(len(WINDOWS_FILE_TESTS), 8)
        self.assertEqual(len(set(WINDOWS_FILE_TESTS)), 8)
        selected = {}
        for name in WINDOWS_FILE_TESTS:
            module, cls, method = name.split('.')
            selected.setdefault((module, cls), set()).add(method)
        self.assertEqual({cls: len(methods) for (_, cls), methods in selected.items()},
                         {'WindowsJournalReaders': 4, 'WindowsPathCodeUnits': 1,
                          'WindowsDirectoryHandleTests': 3})
        expected_guard = ast.dump(ast.parse("os.name == 'nt'", mode='eval').body)
        for (module, cls), methods in selected.items():
            with self.subTest(module=module, cls=cls):
                tree = ast.parse((ROOT / 'tests' / (module + '.py')).read_text(encoding='utf-8'))
                definition, = (node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == cls)
                actual = {node.name for node in definition.body
                          if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith('test')}
                self.assertEqual(actual, methods)
                self.assertTrue(any(isinstance(decorator, ast.Call) and
                                    ast.unparse(decorator.func) == 'unittest.skipUnless' and
                                    decorator.args and ast.dump(decorator.args[0]) == expected_guard
                                    for decorator in definition.decorator_list))

    def test_windows_file_gate_remains_separate_from_default_profiles(self):
        modules = {name.split('.')[0] for name in WINDOWS_FILE_TESTS}
        for platform in ('linux', 'win32'):
            for pure in (False, True):
                with self.subTest(platform=platform, pure=pure):
                    self.assertFalse(modules.intersection(qualify.default_test_names(pure=pure, platform=platform)))

    def test_native_defaults_include_directory_publication_and_checkpoint_regressions(self):
        required = ('test_native_output_containment_v2',
                    'test_native_public_directory_checkpoint_v2',
                    'test_native_output_directory_checkpoint_v2')
        for platform in ('linux', 'win32'):
            with self.subTest(platform=platform):
                names = qualify.default_test_names(platform=platform)
                for name in required:
                    self.assertEqual(names.count(name), 1, name)
                self.assertEqual(len(names), len(set(names)))

    def test_pure_defaults_remain_unchanged_on_windows_and_linux(self):
        expected = ['test_public_api_v2', 'test_edit_workflow_v2', 'test_scenario_v2',
                    'test_project_v2', 'test_json_v2', 'test_runner_v2', 'test_output_v2', 'test_report_v2']
        for platform in ('linux', 'win32'):
            with self.subTest(platform=platform):
                self.assertEqual(qualify.default_test_names(pure=True, platform=platform), expected)

    def test_windows_error_mode_is_only_selected_for_native_windows(self):
        for platform in ('linux', 'win32'):
            for pure in (False, True):
                with self.subTest(platform=platform, pure=pure):
                    names = qualify.default_test_names(pure=pure, platform=platform)
                    self.assertEqual('test_native_windows_error_mode' in names,
                                     platform == 'win32' and not pure)

    def test_default_test_selections_do_not_share_mutable_state(self):
        first = qualify.default_test_names(platform='linux')
        expected = list(first)
        first.clear()
        self.assertEqual(qualify.default_test_names(platform='linux'), expected)


class ReleaseGateTests(unittest.TestCase):
    def assert_optimized_cli_is_rejected(self, options=(), **environment):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'must-not-exist'
            for script in ('build_release.py', 'qualify_release.py'):
                with self.subTest(script=script):
                    command = [sys.executable, *options, '-B', str(ROOT / 'tools' / script),
                               '--output', str(output)]
                    if script == 'qualify_release.py':
                        command += ['--package', str(Path(directory) / 'absent-package')]
                    result = subprocess.run(command, capture_output=True, text=True, timeout=30,
                                            env=dict(os.environ, **environment))
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertIn('run without -O, -OO or PYTHONOPTIMIZE', result.stderr)
                    self.assertFalse(output.exists())

    def test_optimized_cli_is_rejected_before_creating_output(self):
        for flag in ('-O', '-OO'):
            with self.subTest(flag=flag):
                self.assert_optimized_cli_is_rejected((flag,))

    def test_environment_optimization_is_rejected_before_creating_output(self):
        self.assert_optimized_cli_is_rejected(PYTHONOPTIMIZE='1')

    def test_successful_nonempty_suite_is_accepted(self):
        result = unittest.TestResult()
        result.testsRun = 1
        qualify.check_test_result(result, {'tests': 1})

    def test_empty_cli_selection_writes_failure_evidence_without_running_example(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'qualification'
            code = (
                'import runpy, sys, unittest; '
                'unittest.defaultTestLoader.loadTestsFromNames = lambda names: unittest.TestSuite(); '
                'sys.argv = sys.argv[1:]; runpy.run_path(sys.argv[0], run_name="__main__")'
            )
            result = subprocess.run(
                [sys.executable, '-B', '-c', code, str(ROOT / 'tools/qualify_release.py'),
                 '--package', str(ROOT / 'src'), '--output', str(output)],
                capture_output=True, text=True, timeout=30,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('Release qualification failed', result.stderr)
            self.assertTrue((output / 'tests.json').is_file())
            self.assertFalse((output / 'first-run').exists())
            self.assertFalse((output / 'result.json').exists())

    def test_empty_failure_error_skip_and_unexpected_success_are_rejected(self):
        for kind in ('empty', 'failure', 'error', 'skip', 'expected failure', 'unexpected success'):
            with self.subTest(kind=kind):
                result = unittest.TestResult()
                result.testsRun = 0 if kind == 'empty' else 1
                if kind == 'failure':
                    result.failures.append(('fixture', 'injected failure'))
                elif kind == 'error':
                    result.errors.append(('fixture', 'injected error'))
                elif kind == 'skip':
                    result.skipped.append(('fixture', 'injected skip'))
                elif kind == 'expected failure':
                    result.expectedFailures.append(('fixture', 'known failure'))
                elif kind == 'unexpected success':
                    result.unexpectedSuccesses.append('fixture')
                with self.assertRaisesRegex(RuntimeError, 'Release qualification failed'):
                    qualify.check_test_result(result, {'tests': result.testsRun})

    def test_nonpassing_cli_outcomes_retain_serializable_evidence(self):
        for outcome in ('skip', 'expected failure', 'unexpected success'):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / 'qualification'
                decorator = ("@unittest.skip('injected skip')" if outcome == 'skip' else
                             '@unittest.expectedFailure')
                body = "self.fail('injected failure')" if outcome == 'expected failure' else 'pass'
                code = (
                    'import runpy, sys, unittest\n'
                    'class SelectedCase(unittest.TestCase):\n'
                    f'    {decorator}\n'
                    '    def test_selected(self):\n'
                    f'        {body}\n'
                    'unittest.defaultTestLoader.loadTestsFromNames = lambda names: '
                    'unittest.defaultTestLoader.loadTestsFromTestCase(SelectedCase)\n'
                    'sys.argv = sys.argv[1:]\n'
                    'runpy.run_path(sys.argv[0], run_name="__main__")\n'
                )
                result = subprocess.run(
                    [sys.executable, '-B', '-c', code, str(ROOT / 'tools/qualify_release.py'),
                     '--package', str(ROOT / 'src'), '--output', str(output)],
                    capture_output=True, text=True, timeout=30,
                )
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn('Release qualification failed', result.stderr)
                summary = json.loads((output / 'tests.json').read_text(encoding='utf-8'))
                self.assertEqual(summary['tests'], 1)
                field = {'skip': 'skips', 'expected failure': 'expected_failures',
                         'unexpected success': 'unexpected_successes'}[outcome]
                self.assertEqual(len(summary[field]), 1)
                self.assertIn('test_selected', str(summary[field][0]))
                self.assertFalse((output / 'first-run').exists())
                self.assertFalse((output / 'result.json').exists())


class SourceArchiveAuditTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / 'tests').mkdir()
        (self.root / 'tests/test_example.py').write_bytes(b'authored test\n')
        (self.root / 'tests/fixtures').mkdir()
        (self.root / 'tests/fixtures/input.bin').write_bytes(b'\x00\xff\r\n')

    def archive(self, entries):
        path = self.root / 'source.tar.gz'
        with tarfile.open(path, 'w:gz') as stream:
            for name, raw in entries:
                member = tarfile.TarInfo(name)
                if raw is None:
                    member.type = tarfile.SYMTYPE
                    member.linkname = 'test_example.py'
                    stream.addfile(member)
                else:
                    member.size = len(raw)
                    stream.addfile(member, io.BytesIO(raw))
        return path

    def entries(self):
        return [('easysewer/tests/test_example.py', b'authored test\n'),
                ('easysewer/tests/fixtures/input.bin', b'\x00\xff\r\n')]

    def test_exact_authored_bytes_are_required_and_bytecode_is_ignored(self):
        (self.root / 'tests/__pycache__').mkdir()
        (self.root / 'tests/__pycache__/cached.pyc').write_bytes(b'cache')
        result = audit_sdist.audit(self.root, self.archive(self.entries()))
        self.assertTrue(result['passed'])
        self.assertEqual(result['expected_files'], 2)
        self.assertEqual(result['archived_files'], 2)

    def test_missing_changed_and_unexpected_files_fail_the_audit(self):
        cases = [
            ('missing', self.entries()[:1], 'tests/fixtures/input.bin'),
            ('different', [(self.entries()[0][0], b'changed')] + self.entries()[1:],
             'tests/test_example.py'),
            ('unexpected', self.entries() + [('easysewer/tests/extra.py', b'extra')],
             'tests/extra.py'),
        ]
        for field, entries, name in cases:
            with self.subTest(field=field):
                result = audit_sdist.audit(self.root, self.archive(entries))
                self.assertFalse(result['passed'])
                self.assertEqual(result[field], [name])

    def test_unsafe_duplicate_nonregular_and_multiple_roots_are_rejected(self):
        cases = [
            ('../escape', b'unsafe'),
            ('/absolute', b'unsafe'),
            ('easysewer\\tests\\escape.py', b'unsafe'),
            self.entries()[0],
            ('easysewer/tests/link.py', None),
            ('other/README.md', b'other root'),
        ]
        for entry in cases:
            with self.subTest(entry=entry[0]):
                with self.assertRaises(ValueError):
                    audit_sdist.audit(self.root, self.archive(self.entries() + [entry]))


class PureProfileValidationTests(unittest.TestCase):
    def test_relative_paths_reject_escapes_and_nonportable_names(self):
        for value in ('', '.', '..', '../outside.py', '/absolute.py', 'a/../b.py',
                      'a//b.py', 'a/./b.py', 'C:/drive.py', 'a\\b.py', 123):
            with self.subTest(value=value), self.assertRaises(ValueError):
                pure.relative(value)
        self.assertEqual(pure.relative('src/easysewer/model.py'), Path('src/easysewer/model.py'))


if __name__ == '__main__':
    unittest.main()
