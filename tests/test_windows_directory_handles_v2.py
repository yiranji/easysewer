"""Real Windows handles exercise archive moves and publication recovery."""
from contextlib import contextmanager, ExitStack
import os
from pathlib import Path
import tempfile
import unittest

from easysewer.runtime import RunResult, inspect_run_recovery, recover_run
from easysewer.runtime import _workspace as storage
from easysewer.runtime._directory_tree import inspect_tree
from test_result_archive_v2 import failure_result

EVIDENCE = []


@contextmanager
def directory_handle(path, *, share_delete=False):
    """Hold an actual file/directory handle; do not mock filesystem operations.

    CreateFileW requires BACKUP_SEMANTICS for directory handles. Omitting
    FILE_SHARE_DELETE prevents delete/rename until the handle is closed.
    https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-createfilew
    """
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.CreateFileW(str(path), 0x80000000, 3 | (4 if share_delete else 0),
                                None, 3, 0x02000000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        yield
    finally:
        if not kernel.CloseHandle(handle):
            raise ctypes.WinError(ctypes.get_last_error())


@unittest.skipUnless(os.name == 'nt', 'Windows handle sharing semantics')
class WindowsDirectoryHandleTests(unittest.TestCase):
    def tree(self, root, name, data):
        path = root/name
        path.mkdir()
        (path/'empty').mkdir()
        (path/'data').write_bytes(data)
        return path

    def test_archive_moves_fail_with_nonsharing_handle_and_succeed_after_release(self):
        for status in ('rejected', 'failed', 'cancelled', 'timed_out'):
            for share_delete in (False, True):
                with self.subTest(status=status, share_delete=share_delete), tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp).resolve()
                    original, moved = root/'archive', root/'moved'
                    value = failure_result(status)
                    value.save(original)
                    before = inspect_tree(original)
                    code = None
                    with directory_handle(original, share_delete=share_delete):
                        if share_delete:
                            original.rename(moved)
                        else:
                            with self.assertRaises(PermissionError) as caught:
                                original.rename(moved)
                            code = caught.exception.winerror
                            self.assertIn(code, (5, 32))
                            self.assertFalse(moved.exists())
                            self.assertEqual(inspect_tree(original), before)
                            self.assertEqual(RunResult.load(original), value)
                    if not share_delete:
                        original.rename(moved)
                    self.assertEqual(inspect_tree(moved), before)
                    self.assertEqual(RunResult.load(moved), value)
                    EVIDENCE.append(dict(case='archive-move', status=status,
                                         share_delete=share_delete, winerror=code, roundtrip=True))

    def test_existing_directory_lock_rolls_back_prior_output_and_allows_fresh_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            source = self.tree(root, 'source', b'new tree')
            target = self.tree(root, 'z-target', b'old tree')
            previous = inspect_tree(target)
            source_manifest = inspect_tree(source)
            report, report_source = root/'a-report', root/'report-source'
            report.write_bytes(b'old report')
            report_source.write_bytes(b'new report')
            t = storage.OutputTransaction('locked-existing', overwrite=True)
            with directory_handle(target):
                try:
                    t.reserve(((report, False), (target, True)))
                    with self.assertRaises(PermissionError) as caught:
                        t.publish({report: report_source, target: source}, expected_trees={source: source_manifest})
                    self.assertIn(caught.exception.winerror, (5, 32))
                    self.assertFalse(t.committed)
                    self.assertEqual(report.read_bytes(), b'old report')
                    self.assertEqual(inspect_tree(target), previous)
                finally:
                    t.close()
            self.assertFalse(t.issues, t.issues)
            self.assertFalse(list(root.glob('.easysewer-*')))
            retry = storage.OutputTransaction('unlocked-existing', overwrite=True)
            try:
                retry.reserve(((report, False), (target, True)))
                retry.publish({report: report_source, target: source}, expected_trees={source: source_manifest})
            finally:
                retry.close()
            self.assertTrue(retry.committed)
            self.assertEqual(report.read_bytes(), b'new report')
            self.assertEqual(inspect_tree(target), source_manifest)
            self.assertEqual(inspect_tree(source), source_manifest)
            self.assertFalse(list(root.glob('.easysewer-*')))
            EVIDENCE.append(dict(case='locked-existing', winerror=caught.exception.winerror,
                                 previous_outputs_restored=True, fresh_retry=True))

    def test_prepared_directory_lock_retains_recovery_then_cleans_after_release(self):
        for member in (False, True):
            with self.subTest(member=member):
                self.check_prepared_directory_lock(member)

    def check_prepared_directory_lock(self, member):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            source = self.tree(root, 'source', b'new tree')
            target = self.tree(root, 'z-target', b'old tree')
            previous = inspect_tree(target)
            manifest = inspect_tree(source)
            report, report_source = root/'a-report', root/'report-source'
            report.write_bytes(b'old report')
            report_source.write_bytes(b'new report')
            t = storage.OutputTransaction('locked-prepared', overwrite=True)
            with ExitStack() as handles:
                locked = []
                def checkpoint():
                    if locked:
                        return
                    for item in t.targets:
                        if item.directory and item.temporary is not None and item.prepared_content is not None:
                            held = item.temporary/'data' if member else item.temporary
                            handles.enter_context(directory_handle(held))
                            locked.append(item.temporary)
                try:
                    t.reserve(((report, False), (target, True)))
                    with self.assertRaises(PermissionError) as caught:
                        t.publish({report: report_source, target: source}, checkpoint=checkpoint,
                                  expected_trees={source: manifest})
                    self.assertIn(caught.exception.winerror, (5, 32))
                    self.assertEqual(len(locked), 1)
                    self.assertFalse(t.committed)
                    self.assertEqual(report.read_bytes(), b'old report')
                    self.assertEqual(inspect_tree(target), previous)
                finally:
                    t.close()
                self.assertTrue(t.issues)
                journal = t.journal.path
                self.assertTrue(journal.exists())
                self.assertTrue(locked[0].exists())
            initial_state = inspect_run_recovery(journal).state
            result = recover_run(journal)
            self.assertEqual(result.state, 'recovered', result.issues)
            self.assertFalse(list(root.glob('.easysewer-*')))
            self.assertEqual(report.read_bytes(), b'old report')
            self.assertEqual(inspect_tree(target), previous)
            self.assertEqual(inspect_tree(source), manifest)
            retry = storage.OutputTransaction('unlocked-prepared', overwrite=True)
            try:
                retry.reserve(((report, False), (target, True)))
                retry.publish({report: report_source, target: source}, expected_trees={source: manifest})
            finally:
                retry.close()
            self.assertTrue(retry.committed)
            self.assertFalse(retry.issues, retry.issues)
            self.assertEqual(report.read_bytes(), b'new report')
            self.assertEqual(inspect_tree(target), manifest)
            self.assertFalse(list(root.glob('.easysewer-*')))
            EVIDENCE.append(dict(case='locked-prepared', winerror=caught.exception.winerror,
                                 held_member=member,
                                 before_recovery=initial_state, recovered=True,
                                 previous_outputs_restored=True, fresh_retry=True))


if __name__ == '__main__':
    unittest.main()
