"""Runner policies, ownership, fault rollback and decoding without native math."""

from dataclasses import replace
from datetime import timedelta
import hashlib
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from easysewer.io.report_document import ReportDocument
from easysewer.model import FileReference
from easysewer.runtime import RunConfig, Runner
from easysewer.runtime._workspace import OutputTransaction, Workspace, copy_input, digest_file
from test_options_v2 import network
import test_backend_v2 as protocol_fixture


def config(path, **options):
    return RunConfig(output_directory=FileReference(path=str(path),direction='output'),**options)


class OutputTransactionTests(unittest.TestCase):
    def test_publish_files_and_resource_tree_then_release_all_locks(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);stage=root/'stage';stage.mkdir()
            (stage/'new').write_bytes(b'new');(root/'old').write_bytes(b'old')
            (stage/'assets').mkdir();(stage/'assets'/'resource').write_bytes(b'captured')
            transaction=OutputTransaction('fixture',overwrite=True)
            try:
                transaction.reserve(((root/'old',False),(root/'assets',True)))
                transaction.publish({root/'old':stage/'new',root/'assets':stage/'assets'})
                self.assertTrue(transaction.committed)
            finally:transaction.close()
            self.assertEqual((root/'old').read_bytes(),b'new')
            self.assertEqual((root/'assets'/'resource').read_bytes(),b'captured')
            self.assertFalse(list(root.glob('.easysewer-*')))

    def test_partial_publication_rolls_back_files_and_new_directories(self):
        for aliased in (False,True):
            with self.subTest(aliased=aliased),tempfile.TemporaryDirectory() as directory:
                root=Path(directory)
                if aliased:
                    # Exercise spelling changes on every host, including ones
                    # without Windows short-name aliases or symlink privileges.
                    (root/'alias').mkdir();root=root/'alias'/'..'
                source=root/'source';source.write_bytes(b'new')
                (root/'a').write_bytes(b'old-a');(root/'b').write_bytes(b'old-b')
                new=root/'created'/'nested'/'c'
                failed_target=(root/'b').resolve()
                real_replace=os.replace
                def fail_second(source,target):
                    if Path(target).resolve()==failed_target and Path(source).name.startswith('.easysewer-publish-'):
                        self.assertEqual((root/'a').read_bytes(),b'new')
                        raise OSError('injected publication failure')
                    return real_replace(source,target)
                transaction=OutputTransaction('fixture',overwrite=True)
                try:
                    transaction.reserve(((root/'a',False),(root/'b',False),(new,False)))
                    with patch('easysewer.runtime._workspace.os.replace',side_effect=fail_second),self.assertRaisesRegex(OSError,'injected publication failure'):
                        transaction.publish({root/'a':source,root/'b':source,new:source})
                    self.assertFalse(transaction.committed)
                finally:transaction.close()
                self.assertEqual((root/'a').read_bytes(),b'old-a');self.assertEqual((root/'b').read_bytes(),b'old-b')
                self.assertFalse((root/'created').exists())
                self.assertFalse(list(root.glob('.easysewer-*')))

    def test_preobserved_hashes_reject_changes_to_files_and_resource_trees(self):
        for tree in (False,True):
            with self.subTest(tree=tree),tempfile.TemporaryDirectory() as directory:
                root=Path(directory);source=root/'source';source.mkdir()
                item=source/'data';item.write_bytes(b'observed')
                expected={item:digest_file(item)}
                target=root/'result'
                if not tree:target.write_bytes(b'previous-success')
                transaction=OutputTransaction('fixture',overwrite=True)
                try:
                    transaction.reserve(((target,tree),))
                    item.write_bytes(b'changed-after-observation')
                    with self.assertRaisesRegex(ValueError,'changed before publication'):
                        transaction.publish({target:source if tree else item},expected_sources=expected)
                finally:transaction.close()
                if tree:self.assertFalse(target.exists())
                else:self.assertEqual(target.read_bytes(),b'previous-success')
                self.assertFalse(list(root.glob('.easysewer-*')))

    def test_reservations_conflicts_external_edit_and_input_hardlink_are_protected(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);target=root/'out';source=root/'source';source.write_bytes(b'data')
            target.write_bytes(b'old')
            first=OutputTransaction('first',overwrite=True);second=OutputTransaction('second',overwrite=True)
            try:
                first.reserve(((target,False),))
                with self.assertRaises(FileExistsError):second.reserve(((target,False),))
                target.write_bytes(b'changed by another tool')
                with self.assertRaises(ValueError):first.publish({target:source})
            finally:first.close();second.close()
            self.assertEqual(target.read_bytes(),b'changed by another tool')
            hardlink=root/'linked';os.link(source,hardlink)
            transaction=OutputTransaction('alias',overwrite=True,protected=(source,))
            try:
                with self.assertRaises(ValueError):transaction.reserve(((hardlink,False),))
            finally:transaction.close()
            self.assertEqual(source.read_bytes(),b'data')

    def test_capture_is_independent_and_detects_source_change_mid_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);source=root/'source';source.write_bytes(b'a'*2000000)
            original=source.read_bytes();called=[]
            def change():
                called.append(1)
                if len(called)==2:source.write_bytes(b'b'*2000000)
            with self.assertRaises(ValueError):copy_input(source,root/'failed',checkpoint=change)
            digest,size=copy_input(source,root/'captured')
            source.write_bytes(original)
            self.assertEqual(digest,hashlib.sha256(b'b'*2000000).hexdigest())
            self.assertEqual(size,2000000)
            self.assertEqual((root/'captured').read_bytes(),b'b'*2000000)

    def test_capture_checks_content_when_same_size_writes_keep_the_fingerprint(self):
        from easysewer.runtime import _workspace
        for during in ('copy', 'verification'):
            with self.subTest(during=during), tempfile.TemporaryDirectory() as directory:
                root=Path(directory);source=root/'source';source.write_bytes(b'a'*2000000)
                stamp=_workspace.fingerprint(source);called=[]
                def change():
                    called.append(1)
                    # Three checkpoints copy the two chunks and observe EOF.
                    # The next checkpoints belong to the independent hash pass.
                    if len(called)==(2 if during=='copy' else 5):
                        source.write_bytes(b'b'*2000000)
                with patch.object(_workspace,'fingerprint',return_value=stamp):
                    with self.assertRaisesRegex(ValueError,'changed while being captured'):
                        copy_input(source,root/'failed',checkpoint=change)
                self.assertEqual(source.read_bytes(),b'b'*2000000)

    def test_workspace_deletes_only_its_original_root_and_failure_retention_is_explicit(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);workspace=Workspace(root,'run')
            (workspace.path/'owned').write_bytes(b'owned');workspace.close()
            self.assertFalse(workspace.path.exists())
            workspace=Workspace(root,'retained');workspace.retained=True;workspace.close()
            self.assertTrue(workspace.path.is_dir())
            # Directory substitution is refused even when it has the same name.
            workspace.retained=False;old=workspace.path.with_name(workspace.path.name+'-moved')
            workspace.path.rename(old);workspace.path.mkdir()
            (workspace.path/'foreign').write_bytes(b'keep')
            with self.assertRaises(RuntimeError):workspace.close()
            self.assertEqual((workspace.path/'foreign').read_bytes(),b'keep')


class RunnerPolicyTests(unittest.TestCase):
    def test_config_new_fields_roundtrip_and_invalid_numbers_are_rejected(self):
        value=config('runs',step_batch_size=17,native_call_timeout=timedelta(seconds=2),file_inspection_limit=1234,normalize_inp=True)
        self.assertEqual(RunConfig.from_json_document(value.to_json_document()).to_json_document().data,value.to_json_document().data)
        for changes in ({'step_batch_size':True},{'step_batch_size':0},{'file_inspection_limit':-1},
            {'file_inspection_limit':True},{'native_call_timeout':timedelta(0)},{'normalize_inp':1}):
            with self.subTest(changes=changes),self.assertRaises((ValueError,TypeError)):replace(value,**changes)

    def test_raw_report_never_guesses_a_codepage_or_drops_invalid_bytes(self):
        raw=b'Volume ft\xb3\r\n'
        value=ReportDocument.from_bytes(raw)
        self.assertIsNone(value.text);self.assertIsNone(value.encoding)
        self.assertEqual(value.raw,raw)
        self.assertEqual(value.diagnostics.diagnostics[0].code,'report.undecoded_bytes')
        decoded=ReportDocument.from_bytes(raw,encoding='cp1252')
        self.assertIn('³',decoded.text);self.assertEqual(decoded.raw,raw)
        with self.assertRaises(UnicodeDecodeError):ReportDocument.from_bytes(raw,on_decode_error='raise')
        utf8=ReportDocument.from_bytes('节点\n'.encode())
        self.assertEqual(utf8.text,'节点\n')

    def test_cancel_before_start_and_missing_backend_create_no_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)/'output';event=threading.Event();event.set()
            result=Runner().run(network(),config(root),cancel_event=event)
            self.assertEqual(result.status,'cancelled');self.assertFalse(root.exists())
            result=Runner(backends={}).run(network(),config(root))
            self.assertEqual(result.status,'rejected');self.assertFalse(root.exists())

    def test_blocked_step_obeys_whole_run_deadline_and_external_cancellation(self):
        for use_deadline in (False,True):
            with self.subTest(deadline=use_deadline),protocol_fixture.BackendContractTests().fixture('hang:step') as (root,backend):
                event=threading.Event();timer=None
                if not use_deadline:
                    timer=threading.Timer(.8,event.set);timer.start()
                try:
                    value=config(root/'outputs',native_call_timeout=timedelta(seconds=5),
                        wall_time_limit=timedelta(seconds=.8) if use_deadline else None,
                        cancellation_poll_interval=timedelta(milliseconds=20),keep_failed_artifacts=False)
                    result=Runner(backends={'swmm:standard':backend}).run(network(),value,cancel_event=event)
                    self.assertEqual(result.status,'timed_out' if use_deadline else 'cancelled',result.failure)
                    self.assertFalse(result.native_completed)
                    self.assertFalse((root/'outputs'/'model.out').exists())
                    self.assertFalse(list((root/'outputs').glob('.easysewer-*')))
                finally:
                    if timer:timer.join()


if __name__=='__main__':unittest.main()
