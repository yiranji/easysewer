"""Required standard/output execution on each supported native platform.

These checks deliberately have no availability skip: selecting the core release
profile requires real native execution, not successful import or discovery.
"""
from dataclasses import replace
from datetime import timedelta
import ctypes
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

import easysewer
from easysewer.io.inp import InpDocument
from easysewer.model import FileReference, Model, Ref
from easysewer.runtime import (CheckpointSchedule, ResumeConfig, Runner, RunnerCheckpoint,
                              RunResult, StandardBackend)
from easysewer.utils import probe_library_path
from test_runner_v2 import config
from test_native_v2_checkpoint_dynwave import fixture

EVIDENCE = []


def loaded_openmp_images(*, platform=None):
    """Read actual loader mappings, not filenames guessed from dependencies."""
    platform = sys.platform if platform is None else platform
    paths = []
    if platform == 'linux':
        for line in Path('/proc/self/maps').read_text(encoding='utf-8').splitlines():
            fields = line.split(maxsplit=5)
            if len(fields) == 6 and fields[5].startswith('/'):
                paths.append(re.sub(r'\\([0-7]{3})', lambda match: chr(int(match[1], 8)), fields[5]))
    elif platform == 'darwin':
        dyld = ctypes.CDLL(None)
        count = dyld._dyld_image_count
        count.argtypes, count.restype = [], ctypes.c_uint32
        name = dyld._dyld_get_image_name
        name.argtypes, name.restype = [ctypes.c_uint32], ctypes.c_char_p
        for index in range(count()):
            value = name(index)
            if value:
                paths.append(os.fsdecode(value))
    return sorted({Path(path).resolve() for path in paths
                   if re.match(r'lib(?:gomp|omp)(?:[.-]|$)', Path(path).name)})


def openmp_origin_evidence(artifact, images):
    test = unittest.TestCase()
    system = artifact.target['system']
    bundled = not artifact.record['external_dependencies'] and system in ('Linux', 'Darwin')
    expected = {}
    if bundled:
        for dependency in artifact.record['dependencies']:
            path = (artifact.root / dependency['path']).resolve()
            if re.match(r'lib(?:gomp|omp)(?:[.-]|$)', path.name):
                test.assertTrue(path.is_relative_to(artifact.root.resolve()))
                test.assertIn('.libs', path.relative_to(artifact.root.resolve()).parts)
                expected[path] = dependency
        test.assertTrue(expected, 'Repaired native wheels must record a bundled OpenMP runtime')
        test.assertEqual(set(images), set(expected), 'Loaded OpenMP must be the bundled runtime, not a system copy')
    if system in ('Linux', 'Darwin'):
        test.assertTrue(images, 'The actual native OpenMP runtime must be present in loader mappings')
    rows = []
    for path in images:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if bundled:
            test.assertEqual(digest, expected[path]['sha256'])
            test.assertEqual(path.stat().st_size, expected[path]['size'])
        rows.append(dict(path=str(path), sha256=digest))
    return dict(bundled_required=bundled, images=rows)


def threaded_model(threads):
    model = Model.from_document(InpDocument.from_text(fixture(ponding=True, fixed=True)), strict=True)
    model.update_options(variable_step=.75, threads=threads)
    # SWMM uses one thread for tiny networks. Sixteen active conduits exercise
    # the actual parallel routing path rather than only an OpenMP version ABI.
    for index in range(1, 8):
        junction, outfall = 'K' + str(index), 'O' + str(index)
        junction_ref = Ref(collection='swmm:nodes', key=junction)
        outfall_ref = Ref(collection='swmm:nodes', key=outfall)
        model.nodes.add(replace(model.nodes['K'], id=junction))
        model.nodes.add(replace(model.nodes['O'], id=outfall))
        model.links.add(replace(model.links['P'], id='P' + str(index), outlet=junction_ref))
        model.links.add(replace(model.links['Q'], id='Q' + str(index), inlet=junction_ref, outlet=outfall_ref))
    return model


def core_process_probe(threads, package_file):
    """Executed in a clean interpreter before any OpenMP runtime is loaded."""
    test = unittest.TestCase()
    test.assertEqual(Path(easysewer.__file__).resolve(), Path(package_file).resolve())
    test.assertTrue(sys.flags.isolated)
    test.assertEqual(os.environ['OMP_NUM_THREADS'], str(threads))
    loaded = {}
    handles = []
    for name, marker, expected in (('swmm5', 'swmm_getVersion', 52004),
                                    ('swmm-output', 'swmm_getEasySewerOutputIO', 1)):
        path = probe_library_path(name)
        test.assertIsNotNone(path, 'Required core library absent: ' + name)
        path = Path(path).resolve()
        test.assertTrue(path.is_relative_to(Path(easysewer.__file__).resolve().parent))
        library = ctypes.CDLL(str(path))
        handles.append(library)
        function = getattr(library, marker)
        function.argtypes = []
        function.restype = ctypes.c_int
        test.assertEqual(function(), expected)
        loaded[name] = dict(path=str(path), sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    from easysewer._native_registry import select_library
    origin = openmp_origin_evidence(select_library('swmm5', verify=True), loaded_openmp_images())
    info = StandardBackend().probe()
    test.assertTrue(info.available, info.reason)
    test.assertEqual(info.engine_version, 52004)
    test.assertIn('easysewer:checkpoint:2', info.capabilities)
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        result = Runner().run(threaded_model(threads), config(root/'native results-计算'))
        test.assertTrue(result.succeeded, (result.failure, result.diagnostics))
        test.assertTrue(result.native_completed)
        match = re.search(rb'Number of Threads\s+\.+\s+(\d+)', result.report.read_bytes())
        test.assertIsNotNone(match, 'Native report must expose the effective OpenMP thread count')
        test.assertEqual(int(match[1]), threads)
        from easysewer.runtime._output_api import SWMMOutputAPI
        with result.open_output() as reader, SWMMOutputAPI() as native:
            native.open(result.output.path)
            count = native.get_times(1)
            test.assertGreater(count, 0)
            test.assertEqual(count, reader.metadata.periods)
            node = Ref(collection='swmm:nodes', key='J')
            link = Ref(collection='swmm:links', key='P')
            depth = reader.series(node, 'swmm:depth').values
            flow = reader.series(link, 'swmm:flow').values
            test.assertEqual(depth, tuple(native.get_node_series(reader.metadata.index(node), 0, 0, count)))
            test.assertEqual(flow, tuple(native.get_link_series(reader.metadata.index(link), 0, 0, count)))
            test.assertTrue(all(math.isfinite(value) for value in (*depth, *flow)))
            test.assertGreater(max(depth), 0)
            test.assertGreater(max(flow), 0)
        return dict(threads=threads, actual_threads=int(match[1]), libraries=loaded,
                    periods=count, output_sha256=result.output.sha256, native_output_equal=True,
                    openmp_runtime=origin)


class NativeCorePlatformTests(unittest.TestCase):
    def test_clean_loader_environment_solves_with_one_and_two_openmp_threads(self):
        package = str(Path(easysewer.__file__).resolve().parent.parent)
        tests = str(Path(__file__).resolve().parent)
        command = ('import sys,json; sys.path[:0]=' + repr([package, tests]) + '; '
                   'from test_native_core_platform import core_process_probe; '
                   'print(json.dumps(core_process_probe(int(sys.argv[1]), sys.argv[2])))')
        rows = []
        for threads in (1, 2):
            with self.subTest(threads=threads):
                environment = {key: value for key, value in os.environ.items()
                               if key not in ('LD_LIBRARY_PATH', 'LD_PRELOAD', 'LIBRARY_PATH')
                               and not key.startswith('DYLD_')}
                environment.update(OMP_NUM_THREADS=str(threads), OMP_THREAD_LIMIT=str(threads),
                                   OMP_DYNAMIC='FALSE')
                result = subprocess.run([sys.executable, '-I', '-B', '-c', command, str(threads), easysewer.__file__],
                                        env=environment, capture_output=True, text=True, encoding='utf-8', timeout=120,
                                        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                rows.append(json.loads(result.stdout))
        self.assertEqual(rows[0]['output_sha256'], rows[1]['output_sha256'])
        EVIDENCE.extend(rows)

    def test_unicode_checkpoint_move_archive_and_cancel_release_native_workers(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            saved = []
            model = threaded_model(1)
            # Windows' standard C solver still uses its process ANSI code page;
            # POSIX must execute in a real UTF-8 scratch path, not only publish it.
            scratch = root/('native-scratch' if os.name == 'nt' else 'native-scratch-计算')
            model.update_options(temp_directory=FileReference(path=str(scratch), direction='output'))
            result = Runner().run(model, config(root/'results-计算', step_batch_size=3),
                checkpoints=CheckpointSchedule(directory=FileReference(path=str(root/'saved-状态'), direction='output'),
                    interval=timedelta(seconds=60), on_saved=saved.append))
            self.assertTrue(result.succeeded, result.failure)
            self.assertTrue(saved)
            self.assertFalse(Path(result.snapshot.execution_directory).exists())
            expected = result.output.read_bytes()
            relative = saved[0].directory.relative_to(root/'saved-状态')
            (root/'saved-状态').rename(root/'moved-移動')
            checkpoint = RunnerCheckpoint.load(root/'moved-移動'/relative)
            resumed = Runner().resume(checkpoint, ResumeConfig(
                output_directory=FileReference(path=str(root/'resumed-再開'), direction='output'), step_batch_size=3))
            self.assertTrue(resumed.succeeded, resumed.failure)
            self.assertEqual(resumed.output.read_bytes(), expected)
            self.assertFalse(Path(resumed.snapshot.execution_directory).exists())
            resumed.save(root/'archive-保存')
            (root/'archive-保存').rename(root/'archive-moved-移動')
            restored = RunResult.load(root/'archive-moved-移動')
            self.assertEqual(restored.output.read_bytes(), expected)
            with restored.open_output() as output:
                self.assertGreater(max(output.series(Ref(collection='swmm:nodes', key='J'), 'swmm:depth').values), 0)
            event = threading.Event()
            observations = []
            def cancel(progress):
                if progress.phase == 'running' and progress.simulation_seconds > 0:
                    observations.append(progress.simulation_seconds)
                    event.set()
            from easysewer.runtime import _process_session
            connections = []
            original_connection = _process_session._WorkerConnection
            def traced_connection(directory):
                connection = original_connection(directory)
                connections.append(connection)
                return connection
            with patch.object(_process_session, '_WorkerConnection', side_effect=traced_connection):
                cancelled = Runner().run(model, config(root/'cancelled-停止', step_batch_size=1,
                                                        keep_failed_artifacts=False), progress=cancel, cancel_event=event)
            self.assertTrue(connections)
            for connection in connections:
                self.assertIsNotNone(connection.process.poll(), 'Cancelled native worker is still alive')
                self.assertTrue(all(not thread.is_alive() for thread in connection._threads))
            self.assertTrue(observations, 'Cancellation must happen after a real native routing step')
            self.assertEqual(cancelled.status, 'cancelled')
            self.assertFalse(cancelled.native_completed)
            self.assertIsNone(cancelled.retained_directory)
            self.assertFalse(Path(cancelled.snapshot.execution_directory).exists())
            self.assertFalse((root/'cancelled-停止'/'model.out').exists())
            # Completion after cancellation catches leaked locks and broken
            # subsequent worker launch, rather than relying on process exit.
            retry = Runner().run(model, config(root/'cancelled-停止', step_batch_size=3))
            self.assertTrue(retry.succeeded, retry.failure)
            self.assertEqual(retry.output.read_bytes(), expected)
            EVIDENCE.append(dict(kind='core-lifecycle', checkpoint_moved=True, archive_moved=True,
                                 unicode_paths=True, cancellation_after_native_step=True, retry_exact=True))


if __name__ == '__main__':
    unittest.main()
