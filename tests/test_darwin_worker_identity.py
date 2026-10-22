"""macOS kernel identity ABI, malformed observations and fail-closed recovery."""
from contextlib import contextmanager
import ctypes
import errno
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from easysewer.runtime import _worker_identity as identity


BOOT = '12345678-1234-1234-1234-123456789abc'


class DarwinWorkerIdentityTests(unittest.TestCase):
    def stamp(self, **changes):
        value = dict(platform='darwin', pid=17, started=1700000000123456,
                     boot_id=BOOT, pid_namespace=None)
        value.update(changes)
        return value

    def test_import_and_metadata_validation_do_not_load_native_helper(self):
        script = '''
import sys
sys.path.insert(0, sys.argv[1])
from easysewer.runtime import _worker_identity as identity
assert 'easysewer.runtime._darwin_identity' not in sys.modules
assert 'ctypes' not in sys.modules
identity.validate(dict(platform='darwin', pid=17, started=1,
                       boot_id='12345678-1234-1234-1234-123456789abc', pid_namespace=None))
assert 'easysewer.runtime._darwin_identity' not in sys.modules
assert 'ctypes' not in sys.modules
'''
        package_root = str(Path(identity.__file__).resolve().parents[2])
        child = subprocess.run([sys.executable, '-I', '-B', '-c', script, package_root],
                               capture_output=True, text=True, timeout=15)
        self.assertEqual(child.returncode, 0, child.stdout + child.stderr)

    def test_missing_native_helper_keeps_metadata_valid_but_cannot_prove_exit(self):
        with patch.dict(sys.modules, {'easysewer.runtime._darwin_identity': None}), \
                patch.object(identity.sys, 'platform', 'darwin'):
            self.assertEqual(identity.validate(self.stamp()), self.stamp())
            for observation in (identity._darwin_environment, lambda: identity._darwin(17),
                                lambda: identity.has_exited(self.stamp())):
                with self.subTest(observation=observation), self.assertRaises(ImportError):
                    observation()

    @contextmanager
    def kernel(self, *, status=2, pid=17, seconds=1700000000, microseconds=123456,
               returned=None, error=0):
        seen = []
        def pidinfo(actual_pid, flavor, argument, pointer, size):
            info = pointer._obj
            seen.append((actual_pid, flavor, argument, size, type(info)))
            info.pid = pid
            info.status = status
            info.start_seconds = seconds
            info.start_microseconds = microseconds
            # A process name is arbitrary bytes, not part of the identity key.
            info.comm = b'\xff) (name'
            ctypes.set_errno(error)
            return size if returned is None else returned
        library = SimpleNamespace(proc_pidinfo=pidinfo)
        with patch.object(identity, '_darwin_environment', return_value=BOOT), \
                patch.object(ctypes, 'CDLL', return_value=library) as load:
            yield seen, load, library

    def test_fixed_public_bsdinfo_layout_and_live_or_zombie_observation(self):
        for status in (1, 2, 3, 4, 5):
            with self.subTest(status=status), self.kernel(status=status) as (seen, load, library):
                stamp, alive = identity._darwin(17)
                self.assertEqual(stamp, self.stamp())
                self.assertEqual(alive, status != 5)
                pid, flavor, argument, size, structure = seen[0]
                self.assertEqual((pid, flavor, argument, size), (17, 3, 1, 136))
                self.assertEqual(structure.pid.offset, 12)
                self.assertEqual(structure.comm.offset, 48)
                self.assertEqual(structure.start_seconds.offset, 120)
                self.assertEqual(structure.start_microseconds.offset, 128)
                self.assertEqual(library.proc_pidinfo.argtypes,
                                 [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int])
                load.assert_called_once_with('/usr/lib/libproc.dylib', use_errno=True)

    def test_only_esrch_proves_absence_not_permissions_or_other_errors(self):
        with self.kernel(returned=0, error=errno.ESRCH):
            self.assertIsNone(identity._darwin(17))
        for error in (errno.EPERM, errno.EACCES, errno.EIO, errno.ENOENT, 0):
            with self.subTest(error=error), self.kernel(returned=0, error=error):
                with self.assertRaises(OSError) as caught:
                    identity._darwin(17)
                self.assertEqual(caught.exception.errno, error or errno.EIO)

    def test_truncated_wrong_pid_unknown_status_and_bad_timestamps_fail_closed(self):
        for changes in ({'returned': 135}, {'returned': -1}, {'pid': 18}, {'status': 0},
                        {'status': 6}, {'seconds': 0}, {'microseconds': 1000000}, {'seconds': 2**64-1}):
            with self.subTest(changes=changes), self.kernel(**changes):
                with self.assertRaises(ValueError):
                    identity._darwin(17)

    def test_invalid_signed_pid_is_rejected_before_ffi_or_environment_lookup(self):
        for pid in (True, 0, -1, 2**31, 2**32, '17'):
            with self.subTest(pid=pid), patch.object(identity, '_darwin_environment') as environment:
                with self.assertRaises(ValueError):
                    identity._darwin(pid)
                environment.assert_not_called()

    def test_kernel_boot_uuid_is_bounded_normalized_and_read_only(self):
        def sysctl(name, buffer, size_pointer, new_value, new_size):
            self.assertEqual(name, b'kern.bootsessionuuid')
            self.assertEqual((new_value, new_size), (None, 0))
            self.assertEqual(size_pointer._obj.value, 37)
            buffer.value = BOOT.upper().encode('ascii')
            size_pointer._obj.value = 37
            return 0
        library = SimpleNamespace(sysctlbyname=sysctl)
        with patch.object(ctypes, 'CDLL', return_value=library) as load:
            self.assertEqual(identity._darwin_environment(), BOOT)
            load.assert_called_once_with('/usr/lib/libSystem.B.dylib', use_errno=True)
            self.assertEqual(library.sysctlbyname.restype, ctypes.c_int)

    def test_boot_uuid_failure_or_malformed_reply_is_not_an_exit(self):
        for raw, length, error in ((BOOT.encode(), 36, 0), (b'x'*37, 37, 0),
                                   (b'bad\0', 37, 0), (b'\xff'*36+b'\0', 37, 0),
                                   (b'', 37, errno.EPERM), (b'', 37, errno.ENOENT)):
            def sysctl(name, buffer, size_pointer, new_value, new_size):
                if raw:
                    ctypes.memmove(buffer, raw, len(raw))
                size_pointer._obj.value = length
                ctypes.set_errno(error)
                return -1 if error else 0
            with self.subTest(raw=raw, length=length, error=error), \
                    patch.object(ctypes, 'CDLL', return_value=SimpleNamespace(sysctlbyname=sysctl)):
                with self.assertRaises((ValueError, OSError)):
                    identity._darwin_environment()

    def test_validation_keeps_pid_platform_boot_and_namespace_strict(self):
        with patch.object(identity.sys, 'platform', 'darwin'):
            self.assertEqual(identity.validate(self.stamp(), pid=17), self.stamp())
            for changes in ({'pid': 18}, {'pid': 2**31}, {'platform': 'linux'},
                            {'boot_id': None}, {'boot_id': BOOT.upper()}, {'pid_namespace': [1, 2]}):
                with self.subTest(changes=changes), self.assertRaises(ValueError):
                    identity.validate(self.stamp(**changes), pid=17)

    def test_recovery_compares_kernel_boot_and_creation_not_pid_alone(self):
        stamp = self.stamp()
        with patch.object(identity.sys, 'platform', 'darwin'), \
                patch.object(identity, '_darwin_environment', return_value=BOOT):
            for observed, expected in ((None, True), ((stamp, True), False), ((stamp, False), True),
                                       ((self.stamp(started=stamp['started']+1), True), True)):
                with self.subTest(observed=observed), patch.object(identity, '_observe', return_value=observed):
                    self.assertEqual(identity.has_exited(stamp), expected)
            with patch.object(identity, '_observe', side_effect=PermissionError('denied')):
                with self.assertRaises(PermissionError):
                    identity.has_exited(stamp)
            with patch.object(identity, '_darwin_environment', return_value='2'+BOOT[1:]), \
                    patch.object(identity, '_observe') as observe:
                with self.assertRaisesRegex(ValueError, 'boot changed'):
                    identity.has_exited(stamp)
                observe.assert_not_called()

    def test_platform_dispatch_uses_libproc_observer(self):
        with patch.object(identity.sys, 'platform', 'darwin'), \
                patch.object(identity, '_darwin', return_value=(self.stamp(), True)) as observe:
            self.assertEqual(identity._observe(17), (self.stamp(), True))
            observe.assert_called_once_with(17)


if __name__ == '__main__':
    unittest.main()
