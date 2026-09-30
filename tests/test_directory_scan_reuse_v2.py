"""Shared consumers must not multiply identical full-tree reads per phase."""
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from easysewer.runtime import _directory_graph as graph, _directory_tree as trees


class DirectoryScanReuseTests(unittest.TestCase):
    def requests(self, source, count=8):
        return tuple(graph.DirectoryRequest(key=str(i), source=source,
            access='read_write' if i == 0 else 'read', required=True) for i in range(count))

    def test_duplicate_consumers_read_each_file_once_per_observation_phase(self):
        for kind in ('directory', 'file'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp).resolve();source=root/'source';source.mkdir()
                (source/'data').write_bytes(b'shared bytes')
                requests=self.requests(source if kind=='directory' else source/'data')
                requests=tuple(replace(r,kind=kind) for r in requests)
                reads=[];original=trees._file_digest
                def digest(path, expected):
                    reads.append(Path(path));return original(path,expected)
                with patch.object(trees,'_file_digest',digest),patch.object(graph,'_file_digest',digest):
                    plan,=graph.plan_graphs(requests)
                    # Initial observation plus a fresh end-of-planning check.
                    self.assertEqual(len(reads),2)
                    self.assertEqual(len(plan.state.layout.views),8)
                    reads.clear();graph.verify_plan(plan)
                    self.assertEqual(len(reads),1)
                self.assertTrue(plan.state.layout.mutable)

    def test_required_and_per_consumer_limits_still_apply(self):
        with tempfile.TemporaryDirectory() as tmp:
            source=Path(tmp).resolve()/'source';source.mkdir();(source/'data').write_bytes(b'1234567')
            requests=self.requests(source,2)
            with self.assertRaisesRegex(ValueError,'budget'):
                graph.plan_graphs((requests[0],replace(requests[1],limits=trees.DirectoryLimits(total_bytes=6))))
            absent=source/'absent'
            requests=self.requests(absent,2)
            with self.assertRaises(FileNotFoundError):
                graph.plan_graphs((replace(requests[0],required=False),requests[1]))

    def test_changed_content_and_identity_are_rechecked_before_capture(self):
        for change in ('content','identity','new_member'):
            with self.subTest(change=change),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp).resolve();source=root/'source';source.mkdir();data=source/'data';data.write_bytes(b'original')
                plan,=graph.plan_graphs(self.requests(source))
                if change=='content':data.write_bytes(b'modified')
                elif change=='identity':data.rename(root/'held');data.write_bytes(b'original')
                else:(source/'new').write_bytes(b'new')
                with self.assertRaisesRegex(ValueError,'changed'):
                    graph.capture_graph(plan,root/'target')
                self.assertFalse((root/'target').exists())

    def test_changes_during_copy_are_rechecked_and_source_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();source=root/'source';source.mkdir();(source/'data').write_bytes(b'original')
            plan,=graph.plan_graphs(self.requests(source));original=graph.copy_input
            def copy(*args,**kwargs):
                result=original(*args,**kwargs);(source/'new').write_bytes(b'external change');return result
            with patch.object(graph,'copy_input',copy),self.assertRaisesRegex(ValueError,'changed'):
                graph.capture_graph(plan,root/'target')
            self.assertEqual((source/'new').read_bytes(),b'external change')
            self.assertEqual((source/'data').read_bytes(),b'original')

    def test_cancellation_is_checked_between_cached_consumers(self):
        with tempfile.TemporaryDirectory() as tmp:
            source=Path(tmp).resolve()/'source';source.mkdir();(source/'data').write_bytes(b'original')
            original=graph._observe;observed=[];checks=[];stop=OSError('cancelled')
            def observe(*args,**kwargs):
                value=original(*args,**kwargs);observed.append(value);return value
            def checkpoint():
                if observed:
                    checks.append(True)
                    if len(checks)==3:raise stop
            with patch.object(graph,'_observe',observe),self.assertRaises(OSError) as caught:
                graph.plan_graphs(self.requests(source,100),checkpoint=checkpoint)
            self.assertIs(caught.exception,stop)
            self.assertEqual((source/'data').read_bytes(),b'original')


if __name__=='__main__':unittest.main()
