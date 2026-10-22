"""Real worker loader failures must not open Windows critical-error dialogs."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from easysewer.runtime import StandardBackend, FlexiblePondingBackend
from easysewer.runtime import _process_session as transport
from worker_command_hooks import before_worker_main

EVIDENCE = []


class WorkerCommandHookTests(unittest.TestCase):
    def test_hook_preserves_original_command_and_runs_before_main(self):
        for source in (
                "marker = 'package-\u6d4b\u8bd5'; from easysewer.runtime._solver_worker import main; main()",
                "marker = 'package-\u6d4b\u8bd5'\nfrom easysewer.runtime._solver_worker import main\nmain()\n"):
            with self.subTest(source=source):
                command = ['base-python', '-I', '-B', '-u', '-c', source]
                result = before_worker_main(command, "events.append(('hook', marker))")
                self.assertEqual(command[-1], source)
                self.assertEqual(result[:-1], command[:-1])
                events = []
                namespace = dict(events=events)
                with patch('easysewer.runtime._solver_worker.main',
                           side_effect=lambda: events.append(('main', namespace['marker']))):
                    exec(result[-1], namespace)
                self.assertEqual(events, [('hook', 'package-\u6d4b\u8bd5'), ('main', 'package-\u6d4b\u8bd5')])

    def test_hook_accepts_current_bootstrap_and_retains_isolation_and_guard_prefix(self):
        original = transport._worker_command()
        modified = before_worker_main(original, 'injected = True')
        self.assertEqual(modified[:-1], original[:-1])
        self.assertEqual(modified[-1].replace("exec('injected = True'); ", '', 1), original[-1])
        compile(modified[-1], '<instrumented-worker>', 'exec')

    def test_hook_rejects_unrecognized_commands_instead_of_silently_missing_injection(self):
        prefix = 'from easysewer.runtime._solver_worker import main\n'
        for source in (prefix + 'main(1)', prefix + 'main()\nprint("later")',
                       prefix + 'other()', 'from other import main\nmain()'):
            with self.subTest(source=source), self.assertRaises(AssertionError):
                before_worker_main(['python', '-I', '-B', '-u', '-c', source], 'pass')
        with self.assertRaises(AssertionError):
            before_worker_main(['python', '-c', prefix + 'main()'], 'pass')


class WindowsErrorModeChecks:
    def test_worker_loader_preserves_flags_and_returns_corrupt_dll_errors(self):
        import ctypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.GetErrorMode.argtypes = []
        kernel.GetErrorMode.restype = ctypes.c_uint
        parent_mode = kernel.GetErrorMode()
        original_command = transport._worker_command
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            observed = root/'loads.jsonl'
            # Guard the actual CDLL entry point: on the old implementation this
            # fails BEFORE loading a corrupt DLL, avoiding another modal dialog.
            hook = '''
import ctypes, json
from pathlib import Path
kernel = ctypes.WinDLL('kernel32', use_last_error=True)
kernel.GetErrorMode.argtypes = []
kernel.GetErrorMode.restype = ctypes.c_uint
kernel.SetErrorMode.argtypes = [ctypes.c_uint]
kernel.SetErrorMode.restype = ctypes.c_uint
kernel.SetErrorMode(0x8000)
original_cdll = ctypes.CDLL
def observed_cdll(*args, **kwargs):
    mode = kernel.GetErrorMode()
    with Path(OBSERVED).open('a', encoding='utf-8') as stream:
        stream.write(json.dumps(dict(mode=mode, library=str(args[0])))+'\\n')
    assert mode & 1, 'Loader would display a critical-error dialog'
    return original_cdll(*args, **kwargs)
ctypes.CDLL = observed_cdll
'''.replace('OBSERVED', repr(str(observed)))
            def command():
                return before_worker_main(original_command(), hook)
            with patch.object(transport, '_worker_command', command):
                for backend_type in self.backend_types:
                    for name, data in [('broken.dll', b'not a library'), ('truncated.dll', b'MZ'+b'\0'*126)]:
                        with self.subTest(backend=backend_type.__name__, name=name):
                            path = root/name
                            path.write_bytes(data)
                            result = backend_type(library=str(path)).probe()
                            self.assertFalse(result.available)
                            self.assertIn('load:', result.reason)
                            self.assertIn('WinError', result.reason)
                            self.assertNotIn('timed out', result.reason)
                            EVIDENCE.append(dict(backend=backend_type.__name__, file=name, reason=result.reason))
                    healthy = backend_type().probe()
                    self.assertTrue(healthy.available, healthy.reason)
            observations = [json.loads(line) for line in observed.read_text(encoding='utf-8').splitlines()]
            self.assertEqual(len(observations), 3 * len(self.backend_types))
            self.assertTrue(all(item['mode'] == 0x8001 for item in observations), observations)
            self.assertEqual(kernel.GetErrorMode(), parent_mode)
            EVIDENCE.append(dict(loads=observations, parent_mode_unchanged=True))


@unittest.skipUnless(os.name == 'nt', 'Windows error-mode policy')
class WindowsErrorModeTests(WindowsErrorModeChecks, unittest.TestCase):
    backend_types = (StandardBackend, FlexiblePondingBackend)


@unittest.skipUnless(os.name == 'nt', 'Windows error-mode policy')
class StandardWindowsErrorModeTests(WindowsErrorModeChecks, unittest.TestCase):
    backend_types = (StandardBackend,)


@unittest.skipUnless(os.name == 'nt', 'Windows error-mode policy')
class ExperimentalWindowsErrorModeTests(WindowsErrorModeChecks, unittest.TestCase):
    backend_types = (FlexiblePondingBackend,)


def load_tests(loader, tests, pattern):
    return unittest.TestSuite((loader.loadTestsFromTestCase(WorkerCommandHookTests),
                               loader.loadTestsFromTestCase(WindowsErrorModeTests)))


if __name__ == '__main__':
    unittest.main()
