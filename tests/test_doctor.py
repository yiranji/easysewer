"""Doctor stays non-mutating by default and isolates every native ABI call."""
from contextlib import ExitStack
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from easysewer import _native_registry as registry
from easysewer import doctor
from test_native_runtime_registry import runtime, write_manifest


class DoctorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manifest = write_manifest(self.root)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(registry, '_package_roots', return_value=(self.root,)))
        self.stack.enter_context(runtime())

    def test_default_is_read_only_and_optional_absence_does_not_fail(self):
        del self.manifest['targets']['linux-x86_64']['libraries']['flexible_ponding']
        (self.root / 'libs/runtime-manifest.json').write_text(json.dumps(self.manifest))
        before = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        with patch.object(doctor, '_run_worker', return_value=dict(ok=True, evidence={})) as worker, \
                patch.object(doctor.tempfile, 'TemporaryDirectory', side_effect=AssertionError('read-only')):
            report = doctor.diagnose()
        self.assertTrue(report['ready'])
        self.assertEqual(report['exit_code'], 0)
        self.assertEqual(report['schema'], 'easysewer:doctor:1')
        self.assertEqual(report['smoke_test']['status'], 'not_requested')
        self.assertEqual(report['checks'][2]['status'], 'absent')
        self.assertIn('unvalidated', report['checks'][2]['note'])
        self.assertEqual([call.args[0] for call in worker.call_args_list],
                         [dict(operation='probe', name='swmm5'), dict(operation='probe', name='swmm-output')])
        self.assertEqual(before, {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob('*') if p.is_file()})

    def test_missing_and_invalid_manifest_have_stable_exit_without_launch(self):
        path = self.root / 'libs/runtime-manifest.json'
        for content, code in ((None, 'native_absent'), ('{', 'manifest_invalid')):
            if content is None:
                path.unlink()
            else:
                path.write_text(content)
            with patch.object(doctor, '_run_worker') as worker:
                report = doctor.diagnose(smoke_test=True)
            self.assertEqual(report['exit_code'], 11)
            self.assertEqual(report['checks'][0]['code'], code)
            self.assertEqual(report['smoke_test']['status'], 'not_run')
            worker.assert_not_called()

    def test_unsupported_abi_and_required_worker_failures_have_stable_exits(self):
        with runtime('Windows', 'ARM64', python_platform='win-arm64'), patch.object(doctor, '_run_worker') as worker:
            self.assertEqual(doctor.diagnose()['exit_code'], 10)
            worker.assert_not_called()
        for code in (12, 13):
            with patch.object(doctor, '_run_worker', return_value=dict(ok=False, exit_code=code,
                    code='load_failed', message='controlled failure')):
                report = doctor.diagnose()
            self.assertEqual(report['exit_code'], code)
            self.assertFalse(report['ready'])

    def test_optional_experimental_failure_does_not_mask_core_readiness(self):
        def outcome(request, timeout):
            if request['name'] == 'flexible_ponding':
                return dict(ok=False, exit_code=12, code='load_failed', message='optional')
            return dict(ok=True, evidence={})
        with patch.object(doctor, '_run_worker', side_effect=outcome):
            report = doctor.diagnose()
        self.assertEqual(report['exit_code'], 0)
        self.assertEqual(report['checks'][2]['status'], 'error')

    def test_smoke_is_explicit_and_parent_cleans_temporary_files_on_failure(self):
        directories = []
        def outcome(request, timeout):
            if request['operation'] == 'probe':
                return dict(ok=True, evidence={})
            root = Path(request['directory'])
            directories.append(root)
            (root / 'partial.out').write_bytes(b'partial')
            return dict(ok=False, exit_code=14, code='smoke_failed', message='controlled failure')
        with patch.object(doctor, '_run_worker', side_effect=outcome):
            report = doctor.diagnose(smoke_test=True)
        self.assertEqual(report['exit_code'], 14)
        self.assertEqual(report['smoke_test']['status'], 'error')
        self.assertEqual(len(directories), 1)
        self.assertFalse(directories[0].exists())

    def test_cli_json_has_same_exit_as_report_and_invalid_timeout_is_usage(self):
        with patch.object(doctor, '_run_worker', return_value=dict(ok=True, evidence={})), \
                patch('sys.stdout', new_callable=io.StringIO) as stream:
            code = doctor.main(['doctor', '--json'])
            data = json.loads(stream.getvalue())
        self.assertEqual(code, data['exit_code'])
        for value in ('0', '-1', 'nan', 'inf', '3601'):
            with patch('sys.stderr', new_callable=io.StringIO), self.assertRaises(SystemExit) as caught:
                doctor.main(['doctor', '--timeout', value])
            self.assertEqual(caught.exception.code, 2)

    def test_worker_crash_timeout_and_invalid_protocol_are_contained(self):
        from easysewer.runtime import _worker_launch
        for script, code in (("import time; time.sleep(30)", 'worker_timeout'),
                             ("raise SystemExit(23)", 'worker_exit'),
                             ("print('not json')", 'worker_protocol'),
                             ("print('{\"ok\": true}')", 'worker_protocol')):
            with self.subTest(code=code), patch.object(_worker_launch, 'command',
                    return_value=[sys.executable, '-I', '-B', '-c', script]):
                report = doctor._run_worker(dict(operation='probe', name='swmm5'), .2 if code == 'worker_timeout' else 10)
            self.assertEqual(report['exit_code'], 13)
            self.assertEqual(report['code'], code)

    def test_worker_load_and_smoke_errors_are_distinct(self):
        from easysewer.runtime import _worker_launch
        script = "print('{\"ok\":false,\"message\":\"controlled error\"}')"
        with patch.object(_worker_launch, 'command', return_value=[sys.executable, '-I', '-B', '-c', script]):
            for operation, code in (('probe', 12), ('smoke', 14)):
                report = doctor._run_worker(dict(operation=operation), 10)
                self.assertEqual(report['exit_code'], code)

    def test_pure_package_module_cli_reports_missing_native_without_runtime_modules(self):
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory) / 'easysewer'
            package.mkdir()
            source = Path(registry.__file__).parent
            for name in ('__init__.py', '__main__.py', 'doctor.py', '_native_registry.py'):
                shutil.copyfile(source / name, package / name)
            script = f"import sys, runpy; sys.path.insert(0, {directory!r}); sys.argv=['easysewer','doctor','--json']; runpy.run_module('easysewer',run_name='__main__')"
            completed = subprocess.run([sys.executable, '-I', '-B', '-c', script], text=True, capture_output=True)
            self.assertEqual(completed.returncode, 11, completed.stderr)
            self.assertEqual(json.loads(completed.stdout)['checks'][0]['code'], 'native_absent')
            self.assertFalse(list(package.rglob('__pycache__')))

    def test_corrupt_manifest_cli_remains_bounded_json(self):
        cases = ['deep_json', 'directory']
        if os.name == 'posix':
            cases.append('symlink_loop')
        if hasattr(os, 'mkfifo'):
            cases.append('fifo')
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                package = Path(directory) / 'easysewer'
                (package / 'libs').mkdir(parents=True)
                for name in ('__init__.py', '__main__.py', 'doctor.py', '_native_registry.py'):
                    shutil.copyfile(Path(registry.__file__).parent / name, package / name)
                manifest = package / 'libs/runtime-manifest.json'
                if case == 'deep_json':
                    manifest.write_text('[' * 20000 + '0' + ']' * 20000)
                elif case == 'directory':
                    manifest.mkdir()
                elif case == 'symlink_loop':
                    manifest.symlink_to(manifest.name)
                else:
                    os.mkfifo(manifest)
                script = f"import sys, runpy; sys.path.insert(0, {directory!r}); sys.argv=['easysewer','doctor','--json','--timeout','0.05']; runpy.run_module('easysewer',run_name='__main__')"
                completed = subprocess.run([sys.executable, '-I', '-B', '-c', script],
                    text=True, capture_output=True, timeout=5)
                self.assertEqual(completed.returncode, 11, completed.stderr)
                self.assertEqual(json.loads(completed.stdout)['checks'][0]['code'], 'manifest_invalid')
                self.assertNotIn('Traceback', completed.stderr)


class NativeDoctorIntegrationTests(unittest.TestCase):
    def test_real_isolated_load_and_smoke_output_read(self):
        try:
            registry.select_library('swmm5')
        except registry.NativeRegistryError as error:
            self.skipTest('Native integration requires an installed matching native wheel: ' + str(error))
        report = doctor.diagnose(smoke_test=True)
        self.assertEqual(report['exit_code'], 0, json.dumps(report, indent=2))
        self.assertTrue(report['smoke_test']['evidence']['output_values_match'])
        self.assertTrue(report['smoke_test']['evidence']['native_output_read'])
        self.assertTrue(report['smoke_test']['evidence']['pure_output_read'])


if __name__ == '__main__':
    unittest.main()
