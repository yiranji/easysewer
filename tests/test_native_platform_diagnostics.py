"""Packaged-library discovery is conservative and does not load native code."""

from contextlib import ExitStack, contextmanager
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from easysewer import get_native_capabilities
from easysewer.runtime import (FlexiblePondingBackend, RunError, Runner, SessionCancelled, SessionError,
                              SessionTimeout, StandardBackend)
from easysewer.runtime.backend import NativeFailure
from easysewer.utils import find_library_path, probe_library_path


@contextmanager
def host(system='Linux', machine='x86_64', bits=64, python_platform=None):
    if python_platform is None:
        python_platform = ('win-amd64' if machine.lower() in ('amd64', 'x86_64') and bits == 64 else
                           'win-arm64' if machine.lower() == 'arm64' and bits == 64 else 'win32')
    with ExitStack() as stack:
        stack.enter_context(patch('platform.system', return_value=system))
        stack.enter_context(patch('platform.machine', return_value=machine))
        stack.enter_context(patch('struct.calcsize', return_value=bits // 8))
        stack.enter_context(patch('sysconfig.get_platform', return_value=python_platform))
        yield


class NativePlatformDiagnosticsTests(unittest.TestCase):
    def run_path_failure(self, *, system='Windows', capable=True, stage='open', code=303,
                         message='ERROR 303: input path cannot be resolved or exceeds native path buffer.'):
        from test_backend_v2 import BackendContractTests
        from test_options_v2 import network
        from test_runner_v2 import config
        loaded=StandardBackend._loaded
        def metadata(backend,value):
            info=loaded(backend,value)
            return replace(info,platform=system,capabilities=info.capabilities+
                (('easysewer:path-io:1',) if capable else ()))
        failure=NativeFailure(stage=stage,code=code,message=message)
        cleanup=(NativeFailure(stage='close',code=301,message='original cleanup failure'),)
        error=SessionError(failure,cleanup=cleanup,stderr='original stderr',returncode=7)
        with BackendContractTests().fixture() as (root,backend):
            destination=root/'published';destination.mkdir()
            (destination/'model.out').write_bytes(b'previous-success')
            with patch.object(StandardBackend,'_loaded',metadata), \
                    patch('easysewer.runtime._process_session.ProcessSession.open',side_effect=error):
                with self.assertRaises(RunError) as caught:
                    Runner(backends={backend.key:backend}).run(network(),
                        config(destination,overwrite=True,keep_failed_artifacts=False),raise_on_error=True)
            self.assertIs(caught.exception.__cause__,error)
            result=caught.exception.result
            self.assertEqual(result.status,'failed')
            self.assertFalse(result.native_completed)
            self.assertEqual(result.failure.native,failure)
            self.assertEqual(result.failure.message,str(error))
            self.assertEqual(result.failure.cleanup,cleanup)
            self.assertEqual(result.failure.stderr,'original stderr')
            self.assertEqual(result.failure.worker_returncode,7)
            self.assertEqual((destination/'model.out').read_bytes(),b'previous-success')
            self.assertIsNone(result.retained_directory)
            return result

    def test_windows_native_path_hint_preserves_primary_failure_and_cause(self):
        result=self.run_path_failure()
        hint,=[d for d in result.diagnostics.diagnostics if d.code=='run.native_path']
        self.assertEqual(hint.severity.value,'warning')
        self.assertIn('ANSI code page',hint.message)
        self.assertIn('4095-byte',hint.message)
        self.assertIn('Options.temp_directory (TEMPDIR)',hint.message)
        self.assertIn('output_directory can remain Unicode',hint.message)
        self.assertIn('never relocated',hint.message)

    def test_other_native_failures_do_not_inherit_windows_path_hint(self):
        for changes in ({'system':'Linux'},{'capable':False},{'stage':'start'},
                        {'code':302},{'message':'ERROR 303: unrelated input failure'}):
            with self.subTest(**changes):
                result=self.run_path_failure(**changes)
                self.assertNotIn('run.native_path',{d.code for d in result.diagnostics.diagnostics})

    def test_unsupported_architecture_cannot_advertise_bundled_libraries(self):
        for system, machine, bits in (
            ('Linux', 'aarch64', 64), ('Windows', 'ARM64', 64),
            ('Linux', 'x86_64', 32), ('Windows', 'x86', 32),
        ):
            with self.subTest(system=system, machine=machine, bits=bits), host(system, machine, bits):
                self.assertEqual(get_native_capabilities(), {
                    'swmm_solver': False, 'swmm_output': False, 'flexible_ponding': False,
                })
                with self.assertRaisesRegex(OSError, 'x86-64.*64-bit'):
                    find_library_path('swmm5')

    def test_supported_architecture_aliases_still_discover_files(self):
        for system, machine in (('Linux', 'x86_64'), ('Linux', 'amd64'), ('Windows', 'AMD64')):
            with self.subTest(system=system, machine=machine), host(system, machine), \
                    patch('easysewer.utils.os.path.isfile', return_value=True):
                self.assertIsNotNone(probe_library_path('swmm5'))

    def test_windows_x64_python_on_arm_host_reaches_real_loader_check(self):
        with tempfile.TemporaryDirectory() as directory:
            library = Path(directory) / 'solver.dll'
            library.write_bytes(b'fixture')
            for backend in (StandardBackend(), FlexiblePondingBackend()):
                with self.subTest(backend=backend.key), host('Windows', 'ARM64', python_platform='win-amd64'), \
                        patch('easysewer.utils.os.path.isfile', return_value=True), \
                        patch('easysewer.runtime.native.probe_library_path', return_value=str(library)), \
                        patch('easysewer.runtime.flexible.probe_library_path', return_value=str(library)), \
                        patch('easysewer.runtime._process_session.ProcessSession') as launch:
                    self.assertTrue(all(get_native_capabilities().values()))
                    backend.session(working_directory=Path(directory))
                    launch.assert_called_once()

    def test_windows_native_arm_and_32_bit_interpreters_remain_unavailable(self):
        for machine, bits, python_platform in (
            ('ARM64', 64, 'win-arm64'), ('AMD64', 64, 'win-arm64'),
            ('ARM64', 32, 'win32'), ('ARM64', 32, 'win-amd64'),
        ):
            with self.subTest(machine=machine, bits=bits, python_platform=python_platform), \
                    host('Windows', machine, bits, python_platform), \
                    patch('easysewer.runtime._process_session.ProcessSession') as launch:
                self.assertFalse(any(get_native_capabilities().values()))
                info = StandardBackend().probe()
                self.assertFalse(info.available)
                self.assertIn(python_platform, info.reason)
                launch.assert_not_called()

    def test_directory_named_like_library_is_not_a_capability(self):
        with tempfile.TemporaryDirectory() as directory, host():
            with patch('easysewer.utils._get_library_path_candidates', return_value=('.so.esso', [directory])):
                self.assertIsNone(probe_library_path('swmm5'))
                self.assertFalse(any(get_native_capabilities().values()))

    def test_packaged_probes_explain_unsupported_hosts_without_launching(self):
        for backend in (StandardBackend(), FlexiblePondingBackend()):
            for system, machine, text in (
                ('Darwin', 'arm64', 'Windows and Linux'),
                ('Linux', 'aarch64', 'x86-64'),
            ):
                with self.subTest(backend=backend.key, system=system), host(system, machine), \
                        patch('easysewer.runtime._process_session.ProcessSession') as launch:
                    info = backend.probe()
                    self.assertFalse(info.available)
                    self.assertIn(text, info.reason)
                    self.assertIn(machine, info.reason)
                    launch.assert_not_called()

    def test_explicit_library_does_not_inherit_bundled_platform_restrictions(self):
        with tempfile.TemporaryDirectory() as directory:
            library = Path(directory) / 'user.dylib'
            library.write_bytes(b'fixture')
            with host('Darwin', 'arm64'):
                self.assertEqual(StandardBackend(library=str(library))._check_runtime(), str(library))

    def test_missing_packaged_library_explains_pure_package_boundary(self):
        with host(), patch('easysewer.runtime.native.probe_library_path', return_value=None):
            info = StandardBackend().probe()
        self.assertFalse(info.available)
        self.assertIn('pure', info.reason)

    def test_missing_explicit_library_names_the_selected_path(self):
        with tempfile.TemporaryDirectory() as directory:
            library = str(Path(directory) / 'missing.so')
            info = StandardBackend(library=library).probe()
        self.assertFalse(info.available)
        self.assertIn(library, info.reason)

    def test_packaged_linux_loader_hints_preserve_failure_evidence(self):
        cases = (
            ('libgomp.so.1: cannot open shared object file: No such file or directory', 'libgomp1'),
            ("libc.so.6: version `GLIBC_2.33' not found", 'glibc 2.33'),
        )
        with tempfile.TemporaryDirectory() as directory:
            library = Path(directory) / 'solver.so'
            library.write_bytes(b'fixture')
            for backend in (StandardBackend(), FlexiblePondingBackend()):
                for message, hint in cases:
                    with self.subTest(backend=backend.key, message=message), host(), \
                            patch('easysewer.runtime.native.probe_library_path', return_value=str(library)), \
                            patch('easysewer.runtime.flexible.probe_library_path', return_value=str(library)):
                        failure = NativeFailure(stage='load', code=None, message='OSError: ' + message)
                        cleanup = (NativeFailure(stage='close', code=1, message='cleanup'),)
                        error = SessionError(failure, cleanup=cleanup, stderr='loader stderr', returncode=7)
                        error.backend_metadata = {'library': str(library), 'sha256': 'a' * 64}
                        with patch('easysewer.runtime._process_session.ProcessSession', side_effect=error):
                            with self.assertRaises(SessionError) as caught:
                                backend.session(working_directory=Path(directory))
                        result = caught.exception
                        self.assertIn(message, str(result))
                        self.assertIn(hint, str(result))
                        self.assertEqual(result.cleanup, cleanup)
                        self.assertEqual(result.stderr, 'loader stderr')
                        self.assertEqual(result.returncode, 7)
                        self.assertEqual(result.failure.stage, 'load')
                        self.assertEqual(result.backend_metadata, error.backend_metadata)

    def test_custom_and_unrelated_failures_are_not_relabelled(self):
        with tempfile.TemporaryDirectory() as directory:
            library = Path(directory) / 'solver.so'
            library.write_bytes(b'fixture')
            for explicit, stage, message, error_type in (
                (True, 'load', "version `GLIBC_2.33' not found", SessionError),
                (False, 'launch', 'libgomp.so.1: cannot open shared object file', SessionError),
                (False, 'load', 'Native library SHA-256 does not match', SessionError),
                (False, 'load', "version `GLIBC_2.33' not found", SessionTimeout),
                (False, 'load', "version `GLIBC_2.33' not found", SessionCancelled),
            ):
                with self.subTest(explicit=explicit, stage=stage), host(), \
                        patch('easysewer.runtime.native.probe_library_path', return_value=str(library)):
                    error = error_type(NativeFailure(stage=stage, code=None, message=message))
                    with patch('easysewer.runtime._process_session.ProcessSession', side_effect=error):
                        with self.assertRaises(SessionError) as caught:
                            StandardBackend(library=str(library) if explicit else None).session(
                                working_directory=Path(directory))
                    self.assertIs(caught.exception, error)


if __name__ == '__main__':
    unittest.main()
