"""Direct interpreter selection, isolated bootstrap and hostile identities."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import easysewer
from easysewer.runtime import SessionError
from easysewer.runtime import _process_session, _worker_launch as launch


PACKAGE = str(Path(easysewer.__file__).resolve().parent.parent)


def runtime(**overrides):
    values = {name: getattr(sys, name) for name in (
        'executable', '_base_executable', 'prefix', 'exec_prefix', 'base_prefix',
        'base_exec_prefix', 'platform', 'implementation', 'version', 'maxsize')}
    values['abiflags'] = getattr(sys, 'abiflags', '')
    values.update(overrides)
    return SimpleNamespace(**values)


class WorkerLaunchTests(unittest.TestCase):
    @contextmanager
    def windows_venv(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            prefix = root / 'venv'
            (prefix / 'Scripts').mkdir(parents=True)
            executable = prefix / 'Scripts' / 'python.exe'
            executable.write_bytes(b'fixture redirector')
            (prefix / 'pyvenv.cfg').write_text('home = ' + str(root), encoding='utf-8')
            base = root / 'python.exe'
            base.write_bytes(b'fixture interpreter')
            current = runtime(platform='win32', prefix=str(prefix), exec_prefix=str(prefix),
                              base_prefix=str(root), base_exec_prefix=str(root),
                              executable=str(executable), _base_executable=str(base))
            with patch.object(launch, 'sys', current):
                yield current

    def test_windows_venv_uses_direct_base_and_child_only_launcher_environment(self):
        with self.windows_venv() as current, patch.dict(os.environ, {
                '__PYVENV_LAUNCHER__': 'untrusted', 'PYTHONEXECUTABLE': 'other', 'UNCHANGED': 'yes'}):
            before = dict(os.environ)
            command = launch.command(PACKAGE)
            child = launch.environment()
            self.assertEqual(command[:5], [current._base_executable, '-I', '-B', '-u', '-c'])
            self.assertIn(repr(PACKAGE), command[-1])
            self.assertEqual(child['__PYVENV_LAUNCHER__'], current.executable)
            self.assertNotIn('PYTHONEXECUTABLE', child)
            self.assertEqual(child['UNCHANGED'], 'yes')
            self.assertEqual(dict(os.environ), before)

    def test_base_windows_and_nonwindows_do_not_select_private_base(self):
        for platform in ('linux', 'darwin', 'win32'):
            with self.subTest(platform=platform):
                current = runtime(platform=platform, prefix=sys.base_prefix,
                                  _base_executable='must not be selected')
                with patch.object(launch, 'sys', current), patch.dict(os.environ, {
                        '__PYVENV_LAUNCHER__': 'inherited', 'PYTHONEXECUTABLE': 'inherited'}):
                    self.assertEqual(launch.command(PACKAGE)[0], sys.executable)
                    environment = launch.environment()
                    if platform == 'win32':
                        self.assertNotIn('__PYVENV_LAUNCHER__', environment)
                        self.assertNotIn('PYTHONEXECUTABLE', environment)
                    else:
                        self.assertIsNone(environment)

    def test_unsupported_implementation_is_not_treated_as_cpython_redirector(self):
        with self.windows_venv() as current:
            current.implementation = SimpleNamespace(name='pypy')
            self.assertEqual(launch.command(PACKAGE)[0], current.executable)
            self.assertNotIn('__PYVENV_LAUNCHER__', launch.environment())

    def test_missing_relative_or_recursive_base_and_missing_config_fail_closed(self):
        for value in (None, '', 'python.exe', 'missing', 'launcher'):
            with self.subTest(value=value), self.windows_venv() as current:
                current._base_executable = (
                    current.executable if value == 'launcher' else
                    str(Path(current.base_prefix) / 'missing.exe') if value == 'missing' else value)
                with self.assertRaisesRegex(RuntimeError, 'Windows venv worker'):
                    launch.command(PACKAGE)
        with self.windows_venv() as current:
            Path(current.prefix, 'pyvenv.cfg').unlink()
            with self.assertRaisesRegex(RuntimeError, 'pyvenv.cfg'):
                launch.command(PACKAGE)

    def guarded(self, current, *, payload='print("verified", flush=True)'):
        with patch.object(launch, 'sys', current), patch.object(launch, '_windows_venv', return_value=True), \
                patch.object(launch, '_base_interpreter', return_value=sys.executable):
            command = launch.command(PACKAGE)
        command[-1] = command[-1].replace('from easysewer.runtime._solver_worker import main\nmain()', payload)
        return subprocess.run(command, capture_output=True, text=True, timeout=15)

    def test_bootstrap_checks_version_word_size_abi_prefix_and_executable_before_import(self):
        good = self.guarded(runtime())
        self.assertEqual(good.returncode, 0, good.stderr)
        self.assertEqual(good.stdout.strip(), 'verified')
        cases = (
            ('version', 'a different build', 'version, architecture, or ABI'),
            ('maxsize', 2**31 - 1 if sys.maxsize != 2**31 - 1 else 2**63 - 1, 'architecture'),
            ('abiflags', 'different-ABI', 'ABI'),
            ('prefix', str(Path(sys.prefix) / 'other'), 'prefixes'),
            ('executable', str(Path(sys.executable).parent / 'other.exe'), 'launcher path'),
        )
        for field, value, reason in cases:
            with self.subTest(field=field):
                child = self.guarded(runtime(**{field: value}), payload='raise AssertionError("must not import solver")')
                self.assertEqual(child.returncode, 78, child.stderr)
                failure = json.loads(child.stdout)
                self.assertEqual((failure['id'], failure['ok'], failure['failure']['stage']), (1, False, 'launch'))
                self.assertIn(reason, failure['failure']['message'])
                self.assertNotIn('must not import solver', child.stderr)

    def test_isolated_command_ignores_cwd_and_pythonpath_and_pins_package(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'easysewer.py').write_text('raise AssertionError("shadowed package")')
            command = launch.command(PACKAGE)
            payload = ('import easysewer, json; print(json.dumps(dict(package=easysewer.__file__, '
                       'paths=sys.path, isolated=sys.flags.isolated, bytecode=sys.dont_write_bytecode, '
                       'unbuffered=sys.stdout.write_through)))')
            command[-1] = command[-1].replace('from easysewer.runtime._solver_worker import main\nmain()', payload)
            environment = launch.environment() or dict(os.environ)
            environment['PYTHONPATH'] = str(root)
            child = subprocess.run(command, cwd=root, env=environment, capture_output=True, text=True, timeout=15)
            self.assertEqual(child.returncode, 0, child.stderr)
            value = json.loads(child.stdout)
            self.assertEqual(Path(value['package']).resolve(), Path(easysewer.__file__).resolve())
            self.assertNotIn(str(root), value['paths'])
            self.assertNotIn('', value['paths'])
            self.assertEqual((value['isolated'], value['bytecode'], value['unbuffered']), (1, True, True))

    def test_bootstrap_failure_is_an_actionable_session_error(self):
        with patch.object(launch, 'sys', runtime(version='incompatible build')), \
                patch.object(launch, '_windows_venv', return_value=True), \
                patch.object(launch, '_base_interpreter', return_value=sys.executable):
            command = launch.command(PACKAGE)
        backend = SimpleNamespace(worker_kind='standard', _selection=lambda: ('fixture', None))
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(_process_session, '_worker_command', return_value=command):
            with self.assertRaisesRegex(SessionError, 'version, architecture, or ABI differs') as caught:
                _process_session.ProcessSession(backend, 'fixture', working_directory=Path(directory).resolve(),
                                                call_timeout=10, cancel_event=None)
        self.assertEqual(caught.exception.failure.stage, 'launch')
        self.assertEqual(caught.exception.returncode, 78)

    def test_hostile_self_reported_pid_is_rejected_and_original_process_reaped(self):
        script = ('import json, os, sys, time; sys.path.insert(0, ' + repr(PACKAGE) + '); '
                  'from easysewer.runtime._worker_identity import capture; '
                  'request=json.loads(sys.stdin.buffer.readline()); identity=capture(); '
                  'identity["pid"]=os.getppid(); '
                  'print(json.dumps(dict(id=request["id"], ok=True, value=dict(worker_identity=identity))), flush=True); '
                  'time.sleep(30)')
        command = launch.command(PACKAGE)
        command[-1] = script
        backend = SimpleNamespace(worker_kind='standard', _selection=lambda: ('fixture', None),
                                  _loaded=lambda value: self.fail('Hostile identity reached backend admission'))
        connections = []
        factory = _process_session._WorkerConnection
        def capture(directory):
            result = factory(directory)
            connections.append(result)
            return result
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(_process_session, '_worker_command', return_value=command), \
                patch.object(_process_session, '_WorkerConnection', side_effect=capture):
            with self.assertRaisesRegex(SessionError, 'Invalid worker process identity'):
                _process_session.ProcessSession(backend, 'fixture', working_directory=Path(directory).resolve(),
                                                call_timeout=10, cancel_event=None)
        self.assertEqual(len(connections), 1)
        self.assertIsNotNone(connections[0].process.poll())
        self.assertTrue(all(not thread.is_alive() for thread in connections[0]._threads))


if __name__ == '__main__':
    unittest.main()
