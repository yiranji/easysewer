"""Release gates must fail closed and preserve the authored archive inputs."""

import importlib.util
import io
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


def load_tool(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'tools' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


audit_sdist = load_tool('audit_sdist_tests')
qualify = load_tool('qualify_release')
pure = load_tool('build_pure')


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
        for kind in ('empty', 'failure', 'error', 'skip', 'unexpected success'):
            with self.subTest(kind=kind):
                result = unittest.TestResult()
                result.testsRun = 0 if kind == 'empty' else 1
                if kind == 'failure':
                    result.failures.append(('fixture', 'injected failure'))
                elif kind == 'error':
                    result.errors.append(('fixture', 'injected error'))
                elif kind == 'skip':
                    result.skipped.append(('fixture', 'injected skip'))
                elif kind == 'unexpected success':
                    result.unexpectedSuccesses.append('fixture')
                with self.assertRaisesRegex(RuntimeError, 'Release qualification failed'):
                    qualify.check_test_result(result, {'tests': result.testsRun})


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
