"""Packaged-library discovery is conservative and does not load native code."""

from contextlib import ExitStack, contextmanager
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from easysewer import get_native_capabilities
from easysewer.runtime import (FlexiblePondingBackend, SessionCancelled, SessionError,
                              SessionTimeout, StandardBackend)
from easysewer.runtime.backend import NativeFailure
from easysewer.utils import find_library_path, probe_library_path


@contextmanager
def host(system='Linux', machine='x86_64', bits=64):
    with ExitStack() as stack:
        stack.enter_context(patch('platform.system', return_value=system))
        stack.enter_context(patch('platform.machine', return_value=machine))
        stack.enter_context(patch('struct.calcsize', return_value=bits // 8))
        yield


class NativePlatformDiagnosticsTests(unittest.TestCase):
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
