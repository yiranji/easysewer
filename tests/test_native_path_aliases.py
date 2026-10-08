"""Optional native launch aliases preserve directory and checkpoint ownership."""

import ctypes
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

from easysewer.runtime import _native_paths as paths
from easysewer.runtime._native_flexible import NativeFlexibleSolver
from easysewer.runtime.flexible import FlexiblePondingPolicy


class NativePathAliasesTests(unittest.TestCase):
    def test_other_platforms_and_ascii_paths_do_not_query_short_names(self):
        for platform, directory in (('linux', '/model/中文'), ('win32', 'C:\\model')):
            with self.subTest(platform=platform), patch.object(paths.sys, 'platform', platform), \
                    patch.object(paths, '_short_path') as short:
                self.assertEqual(paths.worker_directory(directory), directory)
                short.assert_not_called()

    def test_existing_ascii_spelling_must_resolve_to_the_same_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root/'中文').mkdir(); (root/'work').mkdir(); (root/'other').mkdir()
            # Two spellings of the same real directory let all platforms check
            # identity independently of whether the host provides 8.3 names.
            original = root/'中文'/'..'/'work'
            alias = str(root/'work')
            with patch.object(paths.sys, 'platform', 'win32'), patch.object(paths, '_short_path', return_value=alias):
                self.assertEqual(paths.worker_directory(original), alias if alias.isascii() else str(original))
                self.assertTrue(Path(alias).samefile(original))
            with patch.object(paths.sys, 'platform', 'win32'), patch.object(paths, '_short_path', return_value=str(root/'other')):
                self.assertEqual(paths.worker_directory(original), str(original))

    def test_missing_non_ascii_relative_and_unavailable_aliases_preserve_original(self):
        with tempfile.TemporaryDirectory() as directory:
            original = Path(directory)/'中文'; original.mkdir()
            for alias in (None, '', str(original), 'relative', str(Path(directory)/'missing'), 'bad\0name'):
                with self.subTest(alias=alias), patch.object(paths.sys, 'platform', 'win32'), \
                        patch.object(paths, '_short_path', return_value=alias):
                    self.assertEqual(paths.worker_directory(original), str(original))
            for error in (OSError('unavailable'), AttributeError('missing API'),
                          ValueError('invalid path'), RuntimeError('alias loop')):
                with self.subTest(error=error), patch.object(paths.sys, 'platform', 'win32'), \
                        patch.object(paths, '_short_path', side_effect=error):
                    self.assertEqual(paths.worker_directory(original), str(original))

    def test_changed_identity_is_not_accepted_even_after_matching_resolution(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve(); (root/'中文').mkdir(); (root/'work').mkdir()
            original = root/'中文'/'..'/'work'
            with patch.object(paths.sys, 'platform', 'win32'), \
                    patch.object(paths, '_short_path', return_value=str(root/'work')), \
                    patch.object(Path, 'samefile', return_value=False):
                self.assertEqual(paths.worker_directory(original), str(original))

    def test_short_path_api_uses_wide_dynamic_buffer(self):
        alias = 'C:\\MODEL~1'
        def query(name, buffer, capacity):
            self.assertEqual(name, 'C:\\中文')
            if buffer is None:
                self.assertEqual(capacity, 0)
                return len(alias)+1
            self.assertEqual(capacity, len(alias)+1)
            buffer.value = alias
            return len(alias)
        function = Mock(side_effect=query)
        with patch.object(ctypes, 'WinDLL', return_value=SimpleNamespace(GetShortPathNameW=function), create=True):
            self.assertEqual(paths._short_path('C:\\中文'), alias)
        self.assertEqual(function.call_count, 2)

    def test_short_path_api_failure_or_changed_buffer_requirement_is_optional(self):
        for results in ([0], [32769], [12, 0], [12, 12], [12, 20]):
            function = Mock(side_effect=results)
            with self.subTest(results=results), patch.object(ctypes, 'WinDLL',
                    return_value=SimpleNamespace(GetShortPathNameW=function), create=True):
                self.assertIsNone(paths._short_path('C:\\中文'))

    def test_restore_translation_keeps_canonical_records_and_only_spells_private_files(self):
        root = Path('/workspace/中文')
        for name in ('input-0', 'output-12'):
            original = root/'.checkpoint-restore-owned'/name
            with patch.object(paths.sys, 'platform', 'win32'):
                relative = paths.restore_path(original, root)
            self.assertEqual(relative, Path('.checkpoint-restore-owned')/name)
            self.assertEqual(root/relative, original)
            with patch.object(paths.sys, 'platform', 'linux'):
                self.assertIs(paths.restore_path(original, root), original)

    def test_restore_translation_rejects_unowned_or_non_ascii_paths(self):
        root = Path('/workspace/中文')
        invalid = (root/'model.out', root/'.checkpoint-restore-owned'/'output-中文',
                   root/'.checkpoint-restore-'/'input-0',
                   root/'.checkpoint-restore-owned'/'output-١',
                   root/'.checkpoint-restore-owned'/'output-1-extra',
                   root/'.checkpoint-restore-owned'/'..'/'output-0',
                   root.parent/'.checkpoint-restore-owned'/'output-0')
        for path in invalid:
            with self.subTest(path=path), patch.object(paths.sys, 'platform', 'win32'), self.assertRaises(ValueError):
                paths.restore_path(path, root)


class FlexibleTraceAliasTests(unittest.TestCase):
    def configure(self, trace):
        solver = object.__new__(NativeFlexibleSolver)
        solver.parameters = None; solver.input_sha256 = 'a'*64
        solver.flow_units = 0; solver.names = []; solver.pollutants = []; solver.records = []
        parameters = dict(policy=FlexiblePondingPolicy().to_json_document().data,
            input_sha256=solver.input_sha256, flow_units='CFS', nodes=[], trace=trace,
            depth_threshold=0., flow_threshold=0., flow_per_volume_rate=1., depth_unit='ft',
            volume_unit='ft3', volume_to_liters=1., quality_enabled=False, pollutants=[])
        solver.configure(parameters)
        return solver

    def test_trace_containment_compares_canonical_cwd_and_preserves_escape_rejection(self):
        previous = Path.cwd()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve(); (root/'alternate').mkdir(); (root/'work').mkdir()
            spelling = root/'alternate'/'..'/'work'
            self.assertTrue(spelling.samefile(root/'work'))
            os.chdir(root/'work')
            try:
                with patch.object(Path, 'cwd', return_value=spelling):
                    self.assertEqual(self.configure('trace.jsonl').parameters['trace'], 'trace.jsonl')
                    for target in ('../escape.jsonl', str(root/'escape.jsonl')):
                        with self.subTest(target=target), self.assertRaisesRegex(ValueError, 'inside the private'):
                            self.configure(target)
                with patch.object(Path, 'cwd', return_value=root/'alternate'):
                    with self.assertRaisesRegex(ValueError, 'inside the private'):
                        self.configure('trace.jsonl')
            finally:
                os.chdir(previous)


if __name__ == '__main__':
    unittest.main()
