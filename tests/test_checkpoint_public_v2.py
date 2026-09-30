"""Public offline checkpoint contract; synthetic payload is not native validation."""
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from easysewer.runtime import Checkpoint, CheckpointLimits
import test_checkpoint_container_v2 as fixture


class PublicCheckpointTests(unittest.TestCase):
    def test_inspection_and_materialization_revalidate_storage(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);saved=fixture.save(root)
            with patch('ctypes.CDLL',side_effect=AssertionError('Offline inspection loaded native code')):
                archive=Checkpoint.load(saved.directory)
                self.assertEqual(archive.simulation_seconds,7)
                self.assertEqual(archive.snapshot,saved.snapshot)
                snapshot=archive.materialize(root/'new')
                self.assertEqual(snapshot.execution_directory,str(root/'new'))
                self.assertEqual((root/'new/model.inp').read_bytes(),saved.snapshot.input_bytes)
                with self.assertRaises(FileExistsError):archive.materialize(root/'new')
                with self.assertRaises(ValueError):Checkpoint.load(saved.directory,limits=CheckpointLimits(total_bytes=1))
                forged=replace(archive,_archive=replace(saved,simulation_seconds=8))
                with self.assertRaises(ValueError):forged.materialize(root/'forged')
                self.assertFalse((root/'forged').exists())
                fixture.rewrite(saved.directory,lambda data:data.update(schema_version='9.0'))
                with self.assertRaises(ValueError):archive.materialize(root/'changed')
                self.assertFalse((root/'changed').exists())
