"""Accelerated backend identity and provenance without loading native code."""
from contextlib import ExitStack
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import struct
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from easysewer import _native_registry as registry, doctor
from easysewer.runtime import AcceleratedBackend, Runner, RunConfig, SessionError, StandardBackend
from easysewer.runtime.accelerated import ACCELERATED_BUILD_POLICY
from easysewer.runtime._checkpoint_container import _native_header, execution_digest
from easysewer.runtime._checkpoint_lifecycle import CheckpointLifecycle
from easysewer.runtime._native_accelerated import NativeAcceleratedSolver
from easysewer.runtime._native_solver import NativeSolver
from test_checkpoint_container_v2 import native_prefix, snapshot
from test_native_runtime_registry import runtime, write_manifest


class AcceleratedBackendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manifest = write_manifest(self.root, keys=('windows-x86_64',))
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(registry, '_package_roots', return_value=(self.root,)))
        self.stack.enter_context(runtime('Windows', 'AMD64', python_platform='win-amd64'))

    @property
    def libraries(self):
        return self.manifest['targets']['windows-x86_64']['libraries']

    def save_manifest(self):
        (self.root / 'libs/runtime-manifest.json').write_text(json.dumps(self.manifest), encoding='utf-8')

    def metadata(self):
        record = self.libraries['swmm-accelerated']
        return dict(library=str(self.root / record['path']), sha256=record['sha256'],
                    engine_version=52004, platform='Windows', architecture='AMD64',
                    abi='cdecl:64:accelerated:1')

    def assert_unverified(self, info):
        self.assertTrue(info.available)
        self.assertEqual(info.numerical_policy, 'user-library:unverified')
        self.assertEqual(info.output_semantics, ())
        self.assertFalse(any(value.startswith(('easysewer:checkpoint:', 'easysewer:standard-fixes:',
                            'easysewer:hydraulic-acceleration:')) for value in info.capabilities))

    def test_default_remains_standard_and_accelerated_absence_has_no_fallback(self):
        self.assertEqual(RunConfig.__dataclass_fields__['backend'].default, 'swmm:standard')
        self.assertIsInstance(Runner().backends['swmm:accelerated'], AcceleratedBackend)
        self.assertEqual(AcceleratedBackend.display_name, 'SWMM Accelerated Solver')
        self.assertEqual(AcceleratedBackend.worker_kind, 'accelerated')
        standard = StandardBackend()._check_runtime()
        self.assertEqual(Path(standard).name, 'swmm5.dll.esdll')
        del self.libraries['swmm-accelerated']
        self.save_manifest()
        self.assertEqual(StandardBackend()._check_runtime(), standard)
        with self.assertRaises(SessionError):
            AcceleratedBackend()._check_runtime()

    def test_verified_bytes_have_distinct_policy_and_standard16_capabilities(self):
        backend = AcceleratedBackend()
        info = backend._loaded(self.metadata())
        self.assertTrue(info.available)
        self.assertEqual(info.key, 'swmm:accelerated')
        self.assertEqual(info.numerical_policy, ACCELERATED_BUILD_POLICY)
        self.assertNotIn('swmm:standard', info.capabilities)
        for capability in ('swmm:accelerated', 'easysewer:standard-fixes:16',
                           'easysewer:checkpoint:2', 'easysewer:hydraulic-acceleration:1'):
            self.assertIn(capability, info.capabilities)
        stale = replace(info, capabilities=info.capabilities + ('easysewer:standard-fixes:999',
                        'easysewer:hydraulic-acceleration:999', 'swmm:standard'))
        self.assertEqual(backend.execution_info(stale), info)
        self.assertEqual(backend.execution_info(info), info)
        # A copied artifact has the same provenance only when its bytes match
        # the separately verified installed registry artifact.
        copied = self.root / 'explicit-copy.dll'
        copied.write_bytes(Path(info.library).read_bytes())
        explicit = AcceleratedBackend(library=str(copied))
        copied_info = explicit._loaded(dict(self.metadata(), library=str(copied)))
        self.assertEqual(copied_info.numerical_policy, info.numerical_policy)
        self.assertEqual(copied_info.capabilities, info.capabilities)

    def test_user_digest_and_adjacent_record_cannot_grant_verified_capabilities(self):
        standard_bytes = (self.root / self.libraries['swmm5']['path']).read_bytes()
        for raw in (b'unrecognized external library', standard_bytes):
            digest = hashlib.sha256(raw).hexdigest()
            with self.subTest(digest=digest):
                library = self.root / 'user-library.dll'
                library.write_bytes(raw)
                metadata = dict(self.metadata(), library=str(library), sha256=digest)
                backend = AcceleratedBackend(library=metadata['library'], expected_sha256=digest)
                info = backend._loaded(metadata)
                self.assert_unverified(info)
                library.with_suffix('.build.json').write_text(
                    json.dumps(dict(numerical_policy=ACCELERATED_BUILD_POLICY, sha256=digest)),
                    encoding='utf-8')
                self.assertEqual(backend.execution_info(info), info)

    def test_changed_build_record_revokes_provenance_without_weakening_byte_checks(self):
        metadata = self.metadata()
        backend = AcceleratedBackend(library=metadata['library'])
        record = self.root / self.libraries['swmm-accelerated']['build_record']
        record.write_bytes(record.read_bytes() + b'\n')
        self.assert_unverified(backend._loaded(metadata))
        with self.assertRaises(registry.NativeRegistryError):
            registry.select_library('swmm-accelerated', verify=True)

    def test_accelerated_manifest_entry_must_be_optional(self):
        self.assertFalse(registry.select_library('swmm-accelerated', verify=True).record['required'])
        self.libraries['swmm-accelerated']['required'] = True
        self.save_manifest()
        with self.assertRaises(registry.NativeRegistryError):
            registry.select_library('swmm-accelerated')

    def test_acceleration_marker_version_and_routing_family_are_required(self):
        class Marker:
            def __init__(self, value):
                self.value = value

            def __call__(self):
                return self.value

        for version, custom, accepted in ((None, False, False), (0, False, False),
                                           (1, False, True), (1, True, False)):
            library = SimpleNamespace()
            if version is not None:
                library.swmm_getEasySewerAccelerationVersion = Marker(version)
            if custom:
                library.swmm_execRouting = object()

            def initialize(solver, *args, **kwargs):
                solver.lib = library
                solver.metadata = {'abi': 'cdecl:64'}

            with self.subTest(version=version, custom=custom), \
                    patch.object(NativeSolver, '__init__', initialize):
                if accepted:
                    solver = NativeAcceleratedSolver('unused')
                    self.assertEqual(solver.metadata['abi'], 'cdecl:64:accelerated:1')
                    self.assertEqual(solver.backend_key, 'swmm:accelerated')
                else:
                    with self.assertRaises(ValueError):
                        NativeAcceleratedSolver('unused')

    def test_standard_and_accelerated_entries_reject_each_others_native_identity(self):
        metadata = self.metadata()
        library = SimpleNamespace(swmm_getEasySewerAccelerationVersion=object())
        with patch('easysewer.runtime._native_solver.c.CDLL', return_value=library):
            with self.assertRaisesRegex(ValueError, 'explicit accelerated backend'):
                NativeSolver(metadata['library'])
        metadata['abi'] = 'cdecl:64'
        self.assertFalse(AcceleratedBackend()._loaded(metadata).available)

    def test_checkpoint_binding_and_lifecycle_preserve_accelerated_backend_identity(self):
        standard = snapshot(self.root)
        accelerated = replace(standard, backend=replace(standard.backend, key='swmm:accelerated'))
        self.assertNotEqual(execution_digest(standard, []), execution_digest(accelerated, []))
        for value, key, accepted in ((standard, 'swmm:standard', True),
                (accelerated, 'swmm:accelerated', True), (standard, 'swmm:accelerated', False),
                (accelerated, 'swmm:standard', False)):
            solver = SimpleNamespace(open_attempted=False, start_attempted=False, closed=False,
                backend_key=key, metadata={name: getattr(value.backend, name)
                    for name in ('sha256', 'engine_version', 'platform', 'architecture', 'abi')})
            with self.subTest(snapshot=value.backend.key, solver=key):
                if accepted:
                    self.assertEqual(CheckpointLifecycle(solver, value).state, 'NEW')
                else:
                    with self.assertRaisesRegex(ValueError, 'backend family mismatch'):
                        CheckpointLifecycle(solver, value)
        binding = 'a' * 64
        raw = bytearray(native_prefix(accelerated, bytes.fromhex(binding)))
        self.assertEqual(_native_header(bytes(raw), accelerated, binding), (False, 7.))
        struct.pack_into('<i', raw, 12, 1)
        struct.pack_into('<i', raw, 60, 1)
        with self.assertRaises(ValueError):
            _native_header(bytes(raw), accelerated, binding)

    def test_doctor_optional_acceleration_failure_or_absence_keeps_core_ready(self):
        def worker(request, timeout):
            if request['name'] == 'swmm-accelerated':
                return dict(ok=False, exit_code=12, code='load_failed', message='marker mismatch')
            return dict(ok=True, evidence={})

        for absent in (False, True):
            with self.subTest(absent=absent):
                if absent:
                    del self.libraries['swmm-accelerated']
                    self.save_manifest()
                with patch.object(doctor, '_run_worker', side_effect=worker):
                    report = doctor.diagnose()
                self.assertTrue(report['ready'])
                self.assertEqual(report['exit_code'], 0)
                accelerated = next(item for item in report['checks'] if item['name'] == 'swmm-accelerated')
                self.assertEqual(accelerated['status'], 'absent' if absent else 'error')
                self.assertFalse(accelerated['required'])
                self.assertFalse(accelerated['experimental'])


if __name__ == '__main__':
    unittest.main()
