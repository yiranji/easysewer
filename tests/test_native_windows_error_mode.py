"""Real worker loader failures must not open Windows critical-error dialogs."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from easysewer.runtime import StandardBackend, FlexiblePondingBackend
from easysewer.runtime import _process_session as transport

EVIDENCE = []


@unittest.skipUnless(os.name == 'nt', 'Windows error-mode policy')
class WindowsErrorModeTests(unittest.TestCase):
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
                value = original_command()
                self.assertTrue(value[-1].endswith('; main()'))
                value[-1] = value[-1][:-len('; main()')]+'; exec('+repr(hook)+'); main()'
                return value
            with patch.object(transport, '_worker_command', command):
                for backend_type in (StandardBackend, FlexiblePondingBackend):
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
            self.assertEqual(len(observations), 6)
            self.assertTrue(all(item['mode'] == 0x8001 for item in observations), observations)
            self.assertEqual(kernel.GetErrorMode(), parent_mode)
            EVIDENCE.append(dict(loads=observations, parent_mode_unchanged=True))


if __name__ == '__main__':
    unittest.main()
