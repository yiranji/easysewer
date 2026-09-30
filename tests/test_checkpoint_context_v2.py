from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from easysewer.runtime._checkpoint_context import read, write
from easysewer.runtime._checkpoint_container import Limits
from test_checkpoint_container_v2 import snapshot


class CheckpointContextTests(unittest.TestCase):
    def test_context_is_offline_bounded_and_content_verified(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);value=snapshot(root)
            with patch('ctypes.CDLL',side_effect=AssertionError('No native bootstrap loading')):
                digest,_=write(root/'context',value)
                self.assertEqual(read(root/'context',digest),value)
                for expected,limits in (('0'*64,Limits()),(digest,Limits(total_bytes=1)),(digest,Limits(manifest_bytes=1))):
                    with self.assertRaises(ValueError):read(root/'context',expected,limits=limits)
                blob=next((root/'context/blobs').iterdir());data=blob.read_bytes();blob.write_bytes(b'X'+data[1:])
                with self.assertRaises(ValueError):read(root/'context',digest)

    def test_context_failure_and_existing_destination_preserve_ownership(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);value=snapshot(root);target=root/'context'
            with self.assertRaises(ValueError):write(target,value,limits=Limits(total_bytes=1))
            self.assertFalse(target.exists())
            with patch('os.fsync',side_effect=OSError('disk full')):
                with self.assertRaises(OSError):write(target,value)
            self.assertFalse(target.exists())
            target.mkdir();(target/'keep').write_bytes(b'original')
            with self.assertRaises(FileExistsError):write(target,value)
            self.assertEqual((target/'keep').read_bytes(),b'original')
