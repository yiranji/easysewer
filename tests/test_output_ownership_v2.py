"""External edits must survive publication, rollback and restored-stream adoption."""
from contextlib import contextmanager
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from easysewer.runtime import CheckpointOutput, Runner
from easysewer.runtime import _workspace as workspace


@contextmanager
def fixed_stamps(stamps):
    original = workspace.fingerprint
    with patch.object(workspace, 'fingerprint', side_effect=lambda p: stamps.get(Path(p), original(p))):
        yield


class OutputOwnershipTests(unittest.TestCase):
    def test_target_is_rechecked_after_scanning_the_prepared_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve(); target = root/'output'; source = root/'source'
            target.write_bytes(b'old'); source.write_bytes(b'new')
            transaction = workspace.OutputTransaction('verification-order', overwrite=True)
            stamp = workspace.fingerprint(target); original = workspace.content_state
            def observe(path, **kwargs):
                value = original(path, **kwargs)
                if Path(path).name.startswith('.easysewer-publish-'): target.write_bytes(b'EXT')
                return value
            try:
                with fixed_stamps({target: stamp}):
                    transaction.reserve(((target, False),))
                    with patch.object(workspace, 'content_state', side_effect=observe):
                        with self.assertRaisesRegex(ValueError, 'content changed after reservation'):
                            transaction.publish({target: source})
            finally: transaction.close()
            self.assertFalse(transaction.committed); self.assertEqual(target.read_bytes(), b'EXT')
            self.assertFalse(list(root.glob('.easysewer-*')))

    def test_cancellation_after_replacement_rolls_back_unchanged_files_and_trees(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve(); target = root/'a'; tree = root/'b'
            source = root/'source'; source.write_bytes(b'new'); target.write_bytes(b'old')
            assets = root/'assets'; assets.mkdir(); (assets/'empty').mkdir(); (assets/'data').write_bytes(b'data')
            transaction = workspace.OutputTransaction('cancel-after-replace', overwrite=True)
            def checkpoint():
                if all(t.published is not None for t in transaction.targets):
                    raise InterruptedError('cancel final verification')
            try:
                transaction.reserve(((target, False), (tree, True)))
                with self.assertRaisesRegex(InterruptedError, 'cancel final verification'):
                    transaction.publish({target: source, tree: assets}, checkpoint=checkpoint)
            finally: transaction.close()
            self.assertEqual(target.read_bytes(), b'old'); self.assertFalse(tree.exists())
            self.assertEqual(transaction.issues, []); self.assertFalse(transaction.committed)
            self.assertFalse(list(root.glob('.easysewer-*')))

    def test_same_stamp_edits_before_and_during_preparation_preserve_destination(self):
        for during_copy in (False, True):
            with self.subTest(during_copy=during_copy), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve(); target = root/'output'; source = root/'source'
                target.write_bytes(b'old'); source.write_bytes(b'new')
                transaction = workspace.OutputTransaction('reserve-edit', overwrite=True)
                stamps = {target: workspace.fingerprint(target)}
                original = workspace.copy_input
                def copying(*args, **kwargs):
                    value = original(*args, **kwargs)
                    target.write_bytes(b'EXT')
                    return value
                try:
                    with fixed_stamps(stamps):
                        transaction.reserve(((target, False),))
                        if not during_copy: target.write_bytes(b'EXT')
                        with patch.object(workspace, 'copy_input', side_effect=copying if during_copy else original):
                            with self.assertRaisesRegex(ValueError, 'content changed after reservation'):
                                transaction.publish({target: source})
                finally: transaction.close()
                self.assertEqual(target.read_bytes(), b'EXT')
                self.assertFalse(transaction.committed)
                self.assertFalse(list(root.glob('.easysewer-*')))

    def test_reservation_hashing_obeys_cancellation_and_releases_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory).resolve()/'output'; target.write_bytes(b'x'*2000000)
            transaction = workspace.OutputTransaction('cancel', overwrite=True)
            calls = 0
            def checkpoint():
                nonlocal calls
                calls += 1
                if calls == 3: raise InterruptedError('cancel during content read')
            try:
                with self.assertRaises(InterruptedError):
                    transaction.reserve(((target, False),), checkpoint=checkpoint)
            finally: transaction.close()
            self.assertEqual(target.read_bytes(), b'x'*2000000)
            self.assertFalse(list(target.parent.glob('.easysewer-*')))

    def test_partial_rollback_preserves_same_stamp_published_file_and_original_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve(); a = root/'a'; b = root/'b'; source = root/'source'
            a.write_bytes(b'old'); b.write_bytes(b'old-b'); source.write_bytes(b'new')
            transaction = workspace.OutputTransaction('rollback-file', overwrite=True)
            original = os.replace; stamps = {}; primary = OSError('second publication failed')
            def replacing(src, dst):
                if Path(dst) == b and Path(src).name.startswith('.easysewer-publish-'):
                    stamps[a] = workspace.fingerprint(a); a.write_bytes(b'EXT'); raise primary
                return original(src, dst)
            try:
                transaction.reserve(((a, False), (b, False)))
                with fixed_stamps(stamps), patch.object(workspace.os, 'replace', side_effect=replacing):
                    with self.assertRaises(OSError) as caught: transaction.publish({a: source, b: source})
                    self.assertIs(caught.exception, primary)
            finally: transaction.close()
            self.assertEqual(a.read_bytes(), b'EXT'); self.assertEqual(b.read_bytes(), b'old-b')
            backups = list(root.glob('.easysewer-backup-*'))
            self.assertEqual([p.read_bytes() for p in backups], [b'old'])
            self.assertTrue(any('Published output changed' in v for v in transaction.issues))
            self.assertTrue(list(root.glob('.easysewer-lock-*')))
            from easysewer.runtime import inspect_run_recovery
            journal, = root.glob('.easysewer-recovery-*.json')
            self.assertEqual(inspect_run_recovery(journal).state, 'interrupted')

    def test_directory_rollback_checks_child_bytes_identity_and_membership(self):
        for change in ('edit', 'add', 'delete', 'replace'):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve(); a = root/'a'; b = root/'b'
                source = root/'source'; source.mkdir(); (source/'data').write_bytes(b'new')
                transaction = workspace.OutputTransaction('rollback-tree', overwrite=True)
                original = os.replace; stamps = {}
                def replacing(src, dst):
                    if Path(dst) == b and Path(src).name.startswith('.easysewer-publish-'):
                        stamps[a] = workspace.fingerprint(a)
                        if change == 'edit':
                            stamps[a/'data'] = workspace.fingerprint(a/'data'); (a/'data').write_bytes(b'EXT')
                        elif change == 'add': (a/'foreign').write_bytes(b'keep')
                        elif change == 'delete': (a/'data').unlink()
                        else:
                            (a/'data').rename(root/'moved'); (a/'data').write_bytes(b'new')
                        raise OSError('later target failed')
                    return original(src, dst)
                try:
                    transaction.reserve(((a, True), (b, True)))
                    with fixed_stamps(stamps), patch.object(workspace.os, 'replace', side_effect=replacing):
                        with self.assertRaises(OSError): transaction.publish({a: source, b: source})
                finally: transaction.close()
                self.assertTrue(a.is_dir()); self.assertFalse(b.exists())
                if change == 'edit': self.assertEqual((a/'data').read_bytes(), b'EXT')
                if change == 'add': self.assertEqual((a/'foreign').read_bytes(), b'keep')
                if change == 'delete': self.assertFalse((a/'data').exists())
                if change == 'replace': self.assertEqual((root/'moved').read_bytes(), b'new')
                self.assertTrue(transaction.issues)

    def test_changed_backup_is_retained_on_both_commit_and_rollback(self):
        for fail in (False, True):
            with self.subTest(fail=fail), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve(); target = root/'out'; source = root/'source'
                target.write_bytes(b'old'); source.write_bytes(b'new')
                transaction = workspace.OutputTransaction('backup-edit', overwrite=True)
                original = os.replace
                def replacing(src, dst):
                    if Path(dst) == target and Path(src).name.startswith('.easysewer-publish-'):
                        backup = transaction.targets[0].backup; backup.write_bytes(b'EXT')
                        if fail: raise OSError('failed after backup changed')
                    return original(src, dst)
                try:
                    transaction.reserve(((target, False),))
                    with patch.object(workspace.os, 'replace', side_effect=replacing):
                        if fail:
                            with self.assertRaises(OSError): transaction.publish({target: source})
                        else: transaction.publish({target: source})
                finally: transaction.close()
                self.assertEqual([p.read_bytes() for p in root.glob('.easysewer-backup-*')], [b'EXT'])
                self.assertEqual(target.read_bytes() if target.exists() else None, None if fail else b'new')
                self.assertTrue(any('backup changed' in v for v in transaction.issues))

    def test_changed_prepared_bytes_are_rejected_before_replacing_previous_success(self):
        for directory in (False, True):
            with self.subTest(directory=directory), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve(); target = root/'out'; source = root/'source'
                if directory: source.mkdir(); (source/'data').write_bytes(b'new')
                else: source.write_bytes(b'new'); target.write_bytes(b'old')
                transaction = workspace.OutputTransaction('prepared-edit', overwrite=True)
                original = workspace.copy_input
                def copying(src, dst, **kwargs):
                    value = original(src, dst, **kwargs); Path(dst).write_bytes(b'EXT'); return value
                try:
                    transaction.reserve(((target, directory),))
                    with patch.object(workspace, 'copy_input', side_effect=copying):
                        with self.assertRaisesRegex(ValueError, 'Prepared output changed'):
                            transaction.publish({target: source})
                finally: transaction.close()
                self.assertEqual(target.read_bytes() if target.exists() else None, None if directory else b'old')

    def test_edit_immediately_after_final_replace_is_not_accepted_or_rolled_back_over(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve(); target = root/'out'; source = root/'source'
            source.write_bytes(b'new'); target.write_bytes(b'old')
            transaction = workspace.OutputTransaction('final-edit', overwrite=True); original = os.replace
            def replacing(src, dst):
                result = original(src, dst)
                if Path(dst) == target and Path(src).name.startswith('.easysewer-publish-'): target.write_bytes(b'EXT')
                return result
            try:
                transaction.reserve(((target, False),))
                with patch.object(workspace.os, 'replace', side_effect=replacing):
                    with self.assertRaisesRegex(RuntimeError, 'Published output changed'):
                        transaction.publish({target: source})
            finally: transaction.close()
            self.assertFalse(transaction.committed); self.assertEqual(target.read_bytes(), b'EXT')
            self.assertEqual([p.read_bytes() for p in root.glob('.easysewer-backup-*')], [b'old'])

    def test_restored_collection_checks_content_even_with_unchanged_fingerprint(self):
        for during_copy in (False, True):
            with self.subTest(during_copy=during_copy), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve(); source = root/'source'; target = root/'target'
                source.write_bytes(b'complete'); target.write_bytes(b'old')
                baseline = {target: workspace.file_state(target)}
                original = workspace.copy_input
                def copying(*args, **kwargs):
                    value = original(*args, **kwargs); target.write_bytes(b'EXT'); return value
                with fixed_stamps({target: baseline[target][0]}):
                    if not during_copy: target.write_bytes(b'EXT')
                    with patch('easysewer.runtime.runner.copy_input', side_effect=copying if during_copy else original):
                        with self.assertRaisesRegex(ValueError, 'Startup output changed'):
                            Runner._adopt_checkpoint_outputs(
                                (CheckpointOutput(role='run:output', path=source, destination=target),),
                                baseline, root, lambda: None)
                self.assertEqual(source.read_bytes(), b'complete'); self.assertEqual(target.read_bytes(), b'EXT')
                self.assertFalse(list(root.glob('.easysewer-restored-*')))
