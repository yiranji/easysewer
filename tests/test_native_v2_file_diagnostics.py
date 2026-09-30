"""Actual Runner error classification, file provenance and relocated recovery."""
import errno
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from easysewer import get_native_capabilities
from easysewer.io.inp import InpDocument
from easysewer.model import FileReference, Model
from easysewer.model.climate import ClimateFile
from easysewer.runtime import RunResult
from test_native_v2_climate import user_weather
from test_options_v2 import network
from test_runner_v2 import config
import test_native_v2_runner_checkpoint as recovery

EVIDENCE = []


def weather_model(root):
    user_weather(root / 'weather.dat')
    m = network(); m.update_climate(file=ClimateFile(file=FileReference(path=str(root / 'weather.dat')), units='F'))
    return Model.from_document(InpDocument.from_text(m.to_document().text, source=str(root / 'original.inp')), strict=True)


@unittest.skipUnless(get_native_capabilities()['swmm_solver'] and get_native_capabilities()['flexible_ponding'],
                     'Both native families required')
class NativeFileDiagnosticTests(unittest.TestCase):
    def issue(self, result, code):
        issue = next(d for d in result.diagnostics.diagnostics if d.code == code)
        self.assertEqual(issue.subject.collection, 'swmm:climate')
        self.assertEqual(issue.subject.path, ('file', 'file', 'path'))
        self.assertTrue(issue.locations)
        return issue

    def archive(self, result, target):
        result.save(target)
        restored = RunResult.load(target)
        self.assertEqual(restored.diagnostics, result.diagnostics)
        self.assertEqual(restored.failure, result.failure)
        return restored

    def test_operational_capture_errors_remain_failed_and_roundtrip(self):
        with patch.dict(os.environ, EASYSEWER_CHECKPOINT_PACKAGED='1'):
            for family in ('standard', 'custom'):
                with self.subTest(family=family), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory); model = weather_model(root); before = model.to_json_document().to_bytes()
                    for index, failure in enumerate((OSError(errno.ENOSPC, 'Disk full during capture'),
                                                    ValueError('Input changed while being captured'))):
                        with patch('easysewer.runtime._preparation.copy_input', side_effect=failure):
                            result = recovery.runner(family).run(model, config(root / str(index),
                                backend=recovery.backend(family).key, keep_failed_artifacts=False))
                        self.assertEqual(result.status, 'failed', result.failure)
                        self.assertEqual(result.failure.exception_type, type(failure).__name__)
                        self.assertEqual(result.failure.message, str(failure))
                        self.assertEqual(result.failure.stage, 'capture')
                        d = self.issue(result, 'run.resource_capture')
                        self.assertEqual(d.locations[0].status, 'current')
                        self.assertEqual(d.span.source, str(root / 'original.inp'))
                        self.archive(result, root / ('archive' + str(index)))
                    self.assertEqual(model.to_json_document().to_bytes(), before)
                    EVIDENCE.append(dict(kind='capture-failures', family=family, failures=2, archives=2))

    def test_incomplete_inspection_keeps_original_declaration_history(self):
        with patch.dict(os.environ, EASYSEWER_CHECKPOINT_PACKAGED='1'):
            for family in ('standard', 'custom'):
                with self.subTest(family=family), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory); model = weather_model(root)
                    result = recovery.runner(family).run(model, config(root / 'limited',
                        backend=recovery.backend(family).key, file_inspection_limit=1, keep_failed_artifacts=False))
                    self.assertEqual(result.status, 'rejected', result.failure)
                    for code in ('files.inspection_limit', 'run.incomplete_inspection'):
                        d = self.issue(result, code)
                        self.assertEqual(d.locations[0].status, 'changed')
                        self.assertIsNone(d.span)
                        self.assertEqual(d.locations[0].spans[0].source, str(root / 'original.inp'))
                    self.archive(result, root / 'archive')
                    EVIDENCE.append(dict(kind='incomplete-inspection', family=family, status=result.status))

    def test_moved_checkpoint_success_and_resource_fault_sources(self):
        helper = recovery.NativeRunnerCheckpointTests()
        with patch.dict(os.environ, EASYSEWER_CHECKPOINT_PACKAGED='1'):
            for family in ('standard', 'custom'):
                with self.subTest(family=family), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory); original, saved = helper.original(root, family, model=weather_model(root))
                    helper.success(original); self.assertTrue(saved)
                    expected = self.archive(original, root / 'expected')
                    shutil.rmtree(root / 'first'); (root / 'original.hsf').unlink(); (root / 'weather.dat').unlink()
                    (root / 'saved').rename(root / 'moved')
                    checkpoint = root / 'moved' / saved[0].directory.name
                    resumed = recovery.runner(family).resume(checkpoint, recovery.resume_config(root / 'resumed'))
                    helper.equivalent(expected, resumed)
                    self.archive(resumed, root / 'resumed-archive')
                    for mode in ('changed', 'missing'):
                        target = root / mode
                        def corrupt(progress):
                            if progress.phase == 'opening':
                                workspace = next(p for p in target.glob('.easysewer-*') if p.is_dir())
                                declared = next(row for row in original.snapshot.resources if row.sha256 is not None)
                                resource = workspace / 'execution' / declared.relative_path
                                if mode == 'changed': resource.write_bytes(b'changed after materialization')
                                else: resource.unlink()
                        result = recovery.runner(family).resume(checkpoint,
                            recovery.resume_config(target, keep_failed_artifacts=False), progress=corrupt)
                        self.assertEqual(result.status, 'rejected' if mode == 'changed' else 'failed', result.failure)
                        d = self.issue(result, 'run.resource_changed' if mode == 'changed' else 'run.resource_unavailable')
                        label = 'checkpoint-input:' + result.snapshot.input_sha256
                        self.assertEqual(d.locations[0].status, 'current')
                        self.assertEqual(d.span.source, label)
                        self.assertEqual(d.locations[0].spans[0].source, label)
                        self.archive(result, root / (mode + '-archive'))
                    EVIDENCE.append(dict(kind='file-recovery', family=family, out_sha256=resumed.output.sha256,
                        original_resources_removed=True, fault_modes=['changed', 'missing']))


if __name__ == '__main__': unittest.main()
