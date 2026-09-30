"""Runner resume boundaries that must hold without loading native code."""
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from easysewer.model import FileReference
from easysewer.runtime import CheckpointOutput, CheckpointSchedule, ResumeConfig, Runner, RunnerCheckpoint
from easysewer.runtime._workspace import file_state
from test_runner_v2 import config


class RunnerCheckpointTests(unittest.TestCase):
    def test_cancel_and_deadline_before_archive_load_have_no_unverified_lineage(self):
        event=threading.Event();event.set()
        with tempfile.TemporaryDirectory() as directory:
            target=Path(directory)/'new'
            settings=ResumeConfig(output_directory=FileReference(path=str(target),direction='output'))
            with patch.object(RunnerCheckpoint,'load',side_effect=AssertionError('must not load')):
                value=Runner(backends={}).resume('missing',settings,cancel_event=event)
                self.assertEqual(value.status,'cancelled')
                self.assertEqual(value.continuations,());self.assertIsNone(value.snapshot)
                with patch('easysewer.runtime.runner.time.monotonic',side_effect=(0.,2.)):
                    value=Runner(backends={}).resume('missing',replace(settings,wall_time_limit=timedelta(seconds=1)))
                self.assertEqual(value.status,'timed_out');self.assertEqual(value.continuations,())
            self.assertFalse(target.exists())

    def test_resume_settings_cannot_change_execution_configuration(self):
        original=config('old',backend='test:engine',normalize_inp=True,
                        required_capabilities=('test:capability',),cache_reuse=(('HOTSTART','frozen'),))
        value=ResumeConfig(output_directory=FileReference(path='new',direction='output'),step_batch_size=7).apply(original)
        self.assertEqual((value.backend,value.normalize_inp,value.required_capabilities,value.cache_reuse),
                         (original.backend,True,original.required_capabilities,original.cache_reuse))
        self.assertEqual(value.output_directory.path,'new');self.assertEqual(value.step_batch_size,7)
        self.assertEqual(value.report_read,original.report_read)
        with self.assertRaises(TypeError):ResumeConfig(output_directory=FileReference(path='new'),backend='changed')
        with self.assertRaises(ValueError):CheckpointSchedule(directory=FileReference(path='checkpoints',direction='output'),interval=timedelta(0))
        with self.assertRaises(ValueError):CheckpointSchedule(directory=FileReference(path='checkpoints'))

    def test_output_collection_detects_changes_during_copy_and_preserves_sources(self):
        for mode in ('before','during','cancel','success'):
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as directory:
                root=Path(directory).resolve();source=root/'restored';target=root/'startup'
                source.write_bytes(b'complete result');target.write_bytes(b'startup bytes')
                stamps={target:file_state(target)};calls=0
                if mode=='before':target.write_bytes(b'external before')
                def checkpoint():
                    nonlocal calls
                    calls+=1
                    if calls==2:
                        if mode=='during':target.write_bytes(b'external during')
                        if mode=='cancel':raise InterruptedError('stop collection')
                outputs=(CheckpointOutput(role='run:output',path=source,destination=target),)
                if mode=='success':Runner._adopt_checkpoint_outputs(outputs,stamps,root,checkpoint)
                else:
                    with self.assertRaises((ValueError,InterruptedError)):
                        Runner._adopt_checkpoint_outputs(outputs,stamps,root,checkpoint)
                self.assertEqual(source.read_bytes(),b'complete result')
                expected={'before':b'external before','during':b'external during','cancel':b'startup bytes','success':b'complete result'}
                self.assertEqual(target.read_bytes(),expected[mode])
                self.assertEqual({p.name for p in root.iterdir()},{'restored','startup'})

    def test_output_collection_rejects_escape_and_hardlink_alias(self):
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory).resolve();root=base/'work';root.mkdir()
            source=root/'source';source.write_bytes(b'keep');outside=base/'outside';outside.write_bytes(b'outside')
            for target in (outside,root/'alias'):
                if target!=outside:target.hardlink_to(source)
                with self.assertRaises(ValueError):Runner._adopt_checkpoint_outputs(
                    (CheckpointOutput(role='run:output',path=source,destination=target),),
                    {target:file_state(target)},root,lambda:None)
            self.assertEqual(outside.read_bytes(),b'outside');self.assertEqual(source.read_bytes(),b'keep')

    def test_collection_cleanup_failure_preserves_primary_exception(self):
        from easysewer.runtime._workspace import copy_input
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();source=root/'source';target=root/'target'
            source.write_bytes(b'restored');target.write_bytes(b'startup');error=OSError('primary copy failure')
            def failed_copy(*args,**kwargs):
                copy_input(*args,**kwargs)
                raise error
            with patch('easysewer.runtime.runner.copy_input',side_effect=failed_copy),\
                 patch.object(Path,'unlink',side_effect=OSError('cleanup failure')):
                with self.assertRaises(OSError) as caught:Runner._adopt_checkpoint_outputs(
                    (CheckpointOutput(role='run:output',path=source,destination=target),),
                    {target:file_state(target)},root,lambda:None)
            self.assertIs(caught.exception,error)
            self.assertIn('cleanup failure',caught.exception.runner_checkpoint_cleanup)
            self.assertEqual(target.read_bytes(),b'startup');self.assertEqual(source.read_bytes(),b'restored')
