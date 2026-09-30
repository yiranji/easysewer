"""Real filesystem boundaries for immutable journal versions and failed replacement."""
import json,os
from pathlib import Path
import tempfile,unittest
from unittest.mock import patch
from easysewer.runtime import recovery


class JournalReaders(unittest.TestCase):
    def test_old_handle_keeps_complete_version_during_many_updates(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'journal-中文-💧.json'
            recovery._write(path,dict(version=0,payload='旧'*2000))
            if os.name=='nt':
                from easysewer.runtime._recovery_archive import _windows_open
                stream=_windows_open(path,create=False,read_only=True)
            else:stream=path.open('rb')
            with stream:
                before=stream.read();identity=os.fstat(stream.fileno()).st_ino
                for version in range(1,41):
                    data=dict(version=version,payload='新'*2000)
                    recovery._write(path,data)
                    self.assertEqual(json.loads(path.read_bytes()),data)
                    stream.seek(0);self.assertEqual(stream.read(),before)
                    self.assertNotEqual(path.stat().st_ino,identity)
                self.assertEqual(list(path.parent.glob('*.pending')),[])

    def test_non_windows_or_injected_permission_error_is_not_swallowed(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'journal.json';recovery._write(path,dict(version=1))
            before=path.read_bytes();error=PermissionError('injected publication denial')
            with patch.object(recovery.os,'replace',side_effect=error):
                with self.assertRaises(PermissionError) as caught:recovery._write(path,dict(version=2))
            self.assertIs(caught.exception,error);self.assertEqual(path.read_bytes(),before)
            self.assertEqual(list(path.parent.glob('*.pending')),[])
            recovery._write(path,dict(version=3));self.assertEqual(json.loads(path.read_bytes())['version'],3)


@unittest.skipUnless(os.name == 'nt', 'Windows sharing and path semantics')
class WindowsJournalReaders(unittest.TestCase):
    def test_nonsharing_reader_preserves_destination_and_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'journal.json';recovery._write(path,dict(version=1));before=path.read_bytes()
            with path.open('rb') as stream:
                with self.assertRaises(PermissionError):recovery._write(path,dict(version=2))
                self.assertEqual(path.read_bytes(),before);self.assertEqual(stream.read(),before)
                self.assertEqual(list(path.parent.glob('*.pending')),[])
            recovery._write(path,dict(version=3));self.assertEqual(json.loads(path.read_bytes())['version'],3)

    def test_readonly_destination_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'journal.json';recovery._write(path,dict(version=1));before=path.read_bytes()
            os.chmod(path,0o444)
            try:
                with self.assertRaises(PermissionError):recovery._write(path,dict(version=2))
                self.assertEqual(path.read_bytes(),before)
                self.assertEqual(list(path.parent.glob('*.pending')),[])
            finally:os.chmod(path,0o666)

    def test_extended_path_and_unicode_replacement(self):
        from easysewer.runtime._recovery_archive import _windows_open
        with tempfile.TemporaryDirectory() as directory:
            parent=Path(directory)/('a'*110)/('b'*110)/('c'*60);parent.mkdir(parents=True)
            path=parent/'journal-中文-💧.json';self.assertGreater(len(str(path)),260)
            recovery._write(path,dict(version=1))
            with _windows_open(path,create=False,read_only=True) as stream:
                recovery._write(path,dict(version=2))
                self.assertEqual(json.load(stream)['version'],1)
                self.assertEqual(json.loads(path.read_bytes())['version'],2)

    def test_native_replace_missing_target_and_source_sharing_denial(self):
        from easysewer.runtime._recovery_archive import _windows_replace
        with tempfile.TemporaryDirectory() as directory:
            source=Path(directory)/'source';target=Path(directory)/'target'
            source.write_bytes(b'new')
            with source.open('rb'):
                with self.assertRaises(PermissionError):_windows_replace(source,target)
                self.assertFalse(target.exists());self.assertEqual(source.read_bytes(),b'new')
            _windows_replace(source,target);self.assertFalse(source.exists());self.assertEqual(target.read_bytes(),b'new')


@unittest.skipUnless(os.name == 'nt', 'Windows UTF-16 filenames')
class WindowsPathCodeUnits(unittest.TestCase):
    def test_existing_windows_surrogate_path_retains_exact_name(self):
        from easysewer.runtime import recovery
        from easysewer.runtime._recovery_archive import _windows_open
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'journal-\ud800.json'
            recovery._write(path,dict(version=1))
            with _windows_open(path,create=False,read_only=True) as stream:
                recovery._write(path,dict(version=2))
                self.assertEqual(json.load(stream)['version'],1)
                self.assertEqual(json.loads(path.read_bytes())['version'],2)
            self.assertEqual([p.name for p in path.parent.iterdir()],[path.name])
