"""Synthetic cross-platform artifact identities; no foreign binaries are loaded."""
from contextlib import contextmanager, ExitStack
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from easysewer import get_native_capabilities
from easysewer import _native_registry as registry
from easysewer.runtime import FlexiblePondingBackend, StandardBackend


def write_manifest(root, keys=('windows-x86_64', 'linux-x86_64'), *, flexible=True, dependency=False):
    root = Path(root)
    targets = {}
    directories = {'windows-x86_64': 'win', 'linux-x86_64': 'linux',
                   'linux-aarch64': 'linux-aarch64', 'darwin-arm64': 'macos-arm64'}
    for key in keys:
        system, architecture = registry.TARGETS[key]
        directory = root / 'libs' / directories[key]
        directory.mkdir(parents=True, exist_ok=True)
        libraries = {}
        for name, policy in registry.POLICIES.items():
            if name == 'flexible_ponding' and not flexible:
                continue
            suffix = '.dll.esdll' if system == 'Windows' else '.dylib' if system == 'Darwin' else '.so.esso'
            path = directory / (name + suffix)
            path.write_bytes((key + name).encode('ascii'))
            record = dict(sha256=hashlib.sha256(path.read_bytes()).hexdigest(), size=path.stat().st_size)
            evidence = directory / (name + '.build.json')
            evidence.write_text(json.dumps(record), encoding='utf-8')
            dependencies = []
            if dependency:
                library = directory / '.libs' / 'openmp.bin'
                library.parent.mkdir(exist_ok=True)
                library.write_bytes(b'bundled-runtime')
                dependencies.append(dict(path=library.relative_to(root).as_posix(),
                    sha256=hashlib.sha256(library.read_bytes()).hexdigest(), size=library.stat().st_size))
            libraries[name] = dict(path=path.relative_to(root).as_posix(), **record,
                numerical_policy=policy, build_record=evidence.relative_to(root).as_posix(),
                build_record_sha256=hashlib.sha256(evidence.read_bytes()).hexdigest(),
                required=name != 'flexible_ponding', dependencies=dependencies,
                external_dependencies=['libgomp.so.1'] if system == 'Linux' and name != 'swmm-output' and not dependency else [])
        targets[key] = dict(system=system, architecture=architecture, pointer_bits=64, abi='cdecl:64',
                            minimum_os='15.0' if system == 'Darwin' else None,
                            libc=dict(name='glibc', minimum_version='2.33') if system == 'Linux' else None,
                            libraries=libraries)
    manifest = dict(schema=registry.SCHEMA, targets=targets)
    (root / 'libs/runtime-manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
    return manifest


@contextmanager
def runtime(system='Linux', machine='x86_64', bits=64, python_platform='linux-x86_64', soabi=''):
    with ExitStack() as stack:
        # Simulate registry inputs without changing worker-launch OS behavior.
        stack.enter_context(patch.object(registry, 'sys', SimpleNamespace(
            platform={'Windows': 'win32', 'Linux': 'linux', 'Darwin': 'darwin'}.get(system, system.lower()),
            executable=sys.executable)))
        stack.enter_context(patch('os.uname', return_value=SimpleNamespace(machine=machine), create=True))
        stack.enter_context(patch.dict('os.environ', {'PROCESSOR_ARCHITEW6432': machine, 'PROCESSOR_ARCHITECTURE': machine}))
        stack.enter_context(patch('platform.system', return_value=system))
        stack.enter_context(patch('platform.machine', return_value=machine))
        stack.enter_context(patch('struct.calcsize', return_value=bits // 8))
        stack.enter_context(patch('sysconfig.get_platform', return_value=python_platform))
        stack.enter_context(patch('sysconfig.get_config_var', return_value=soabi))
        stack.enter_context(patch('platform.libc_ver', return_value=('glibc', '2.99')))
        stack.enter_context(patch('platform.mac_ver', return_value=('15.0', (), 'arm64')))
        yield


class NativeRegistryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manifest = write_manifest(self.root, tuple(registry.TARGETS), dependency=True)
        self.addCleanup(patch.stopall)
        patch.object(registry, '_package_roots', return_value=(self.root,)).start()

    def save(self):
        (self.root / 'libs/runtime-manifest.json').write_text(json.dumps(self.manifest), encoding='utf-8')

    def test_each_process_abi_selects_only_its_own_artifact(self):
        for key, (system, architecture) in registry.TARGETS.items():
            with self.subTest(key=key), runtime(system, architecture, python_platform='win-amd64' if system == 'Windows' else key):
                artifact = registry.select_library('swmm5', verify=True)
                self.assertEqual(artifact.target_key, key)
                self.assertEqual(artifact.path.read_bytes(), (key + 'swmm5').encode('ascii'))

    def test_translated_windows_and_linux_use_interpreter_architecture(self):
        with runtime('Windows', 'ARM64', python_platform='win-amd64'):
            self.assertEqual(registry.runtime_identity()['target'], 'windows-x86_64')
            self.assertEqual(registry.backend_architecture(), 'AMD64')
        with runtime('Windows', 'AMD64', python_platform='win-arm64'):
            self.assertIsNone(registry.runtime_identity()['target'])
            self.assertEqual(registry.backend_architecture(), 'ARM64')
        with runtime('Linux', 'aarch64', soabi='cpython-313-x86_64-linux-gnu'):
            self.assertEqual(registry.runtime_identity()['target'], 'linux-x86_64')
        with runtime('Linux', 'x86_64', soabi='cpython-313-aarch64-linux-gnu'):
            self.assertEqual(registry.runtime_identity()['target'], 'linux-aarch64')

    def test_windows_build_identity_and_static_probes_never_query_platform(self):
        for tag, machine, target in (('win-amd64', 'ARM64', 'windows-x86_64'),
                                     ('win-arm64', 'AMD64', None), ('win32', 'AMD64', None),
                                     ('win-unknown', 'AMD64', None)):
            with self.subTest(tag=tag), runtime('Windows', machine, python_platform=tag), \
                    patch('platform.system', side_effect=AssertionError('OS version probe')), \
                    patch('platform.machine', side_effect=AssertionError('OS version probe')), \
                    patch('platform.uname', side_effect=AssertionError('OS version probe')), \
                    patch('os.uname', side_effect=AssertionError('Windows has no uname')), \
                    patch('subprocess.Popen', side_effect=AssertionError('spawn')):
                identity = registry.runtime_identity()
                self.assertEqual(identity['system'], 'Windows')
                self.assertEqual(identity['target'], target)
                self.assertEqual(any(get_native_capabilities().values()), target is not None)
                if target is None:
                    self.assertFalse(StandardBackend().probe().available)
        (self.root / 'libs/runtime-manifest.json').unlink()
        with runtime('Windows', 'AMD64', python_platform='win-amd64'), \
                patch('platform.system', side_effect=AssertionError('OS version probe')), \
                patch('platform.machine', side_effect=AssertionError('OS version probe')), \
                patch('subprocess.Popen', side_effect=AssertionError('spawn')):
            self.assertFalse(StandardBackend().probe().available)

    def test_unsupported_os_never_uses_build_platform_detection(self):
        with runtime('AIX', 'ppc64'), patch('sysconfig.get_platform', side_effect=AssertionError('may spawn on AIX')):
            self.assertIsNone(registry.runtime_identity()['target'])
            self.assertFalse(any(get_native_capabilities().values()))

    def test_universal2_uses_active_slice_and_rosetta_is_not_arm(self):
        for machine, target in (('arm64', 'darwin-arm64'), ('x86_64', None)):
            with runtime('Darwin', machine, python_platform='macosx-15.0-universal2'):
                self.assertEqual(registry.runtime_identity()['target'], target)

    def test_unsupported_32_bit_and_architecture_are_not_substituted(self):
        for changes in (dict(bits=32), dict(machine='riscv64')):
            with runtime(**changes):
                with self.assertRaises(registry.NativeRegistryError) as caught:
                    registry.select_library('swmm5')
                self.assertEqual(caught.exception.code, 'unsupported_abi')

    def test_known_incompatible_libc_and_macos_baselines_fail_before_loading(self):
        for libc in (('musl', '1.2'), ('glibc', '2.28')):
            with runtime(), patch('platform.libc_ver', return_value=libc), self.assertRaises(registry.NativeRegistryError) as caught:
                registry.select_library('swmm5', verify=True)
            self.assertEqual(caught.exception.code, 'runtime_incompatible')
        with runtime('Darwin', 'arm64'), patch('platform.mac_ver', return_value=('14.0', (), 'arm64')), self.assertRaisesRegex(registry.NativeRegistryError, 'macOS 15.0'):
            registry.select_library('swmm5', verify=True)

    def test_missing_manifest_is_pure_and_present_invalid_manifest_fails_closed(self):
        path = self.root / 'libs/runtime-manifest.json'
        for content, code in ((None, 'native_absent'), ('not json', 'manifest_invalid')):
            if content is None:
                path.unlink()
            else:
                path.write_text(content)
            with runtime(), patch('subprocess.Popen') as launch:
                self.assertFalse(any(get_native_capabilities().values()))
                info = StandardBackend().probe()
                self.assertFalse(info.available)
                launch.assert_not_called()
                with self.assertRaises(registry.NativeRegistryError) as caught:
                    registry.select_library('swmm5')
                self.assertEqual(caught.exception.code, code)

    def test_final_hash_dependency_and_record_tampering_are_rejected_before_load(self):
        with runtime():
            artifact = registry.select_library('swmm5')
            paths = (artifact.path, self.root / artifact.record['dependencies'][0]['path'],
                     self.root / artifact.record['build_record'])
            for path in paths:
                with self.subTest(path=path):
                    original = path.read_bytes()
                    path.write_bytes(original + b'changed')
                    try:
                        with patch('subprocess.Popen') as launch:
                            info = StandardBackend().probe()
                            self.assertFalse(info.available)
                            self.assertIn('SHA-256', info.reason)
                            launch.assert_not_called()
                    finally:
                        path.write_bytes(original)

    def test_manifest_paths_unknown_policy_duplicate_fields_and_wrong_abi_fail_closed(self):
        mutations = (
            lambda target: target.update(architecture='aarch64'),
            lambda target: target['libraries']['swmm5'].update(path='../escape.so'),
            lambda target: target['libraries']['swmm5'].update(path='/tmp/escape.so'),
            lambda target: target['libraries']['swmm5'].update(numerical_policy='unverified'),
            lambda target: target['libraries']['swmm5'].update(sha256='bad'),
        )
        original = json.dumps(self.manifest)
        for mutate in mutations:
            self.manifest = json.loads(original)
            mutate(self.manifest['targets']['linux-x86_64'])
            self.save()
            with runtime(), self.assertRaises(registry.NativeRegistryError) as caught:
                registry.select_library('swmm5')
            self.assertEqual(caught.exception.code, 'manifest_invalid')
        (self.root / 'libs/runtime-manifest.json').write_text('{"schema":1,"schema":2}')
        with self.assertRaisesRegex(registry.NativeRegistryError, 'Duplicate'):
            registry.read_manifest()

    def test_resolved_symlink_escape_is_rejected_without_platform_privileges(self):
        with runtime():
            selected = registry.select_library('swmm5').path
            original = Path.resolve
            outside = self.root.parent / 'outside-library.so'
            def resolve(path, *args, **kwargs):
                return outside if path == selected else original(path, *args, **kwargs)
            with patch.object(Path, 'resolve', resolve), self.assertRaisesRegex(registry.NativeRegistryError, 'escapes'):
                registry.select_library('swmm5')

    def assert_corruption_is_contained(self, expected_code):
        from easysewer import doctor
        with patch.object(doctor, '_run_worker', return_value=dict(ok=True, evidence={})), \
                patch('subprocess.Popen') as launch:
            report = doctor.diagnose()
            self.assertEqual(report['exit_code'], 11)
            self.assertEqual(report['checks'][0]['code'], expected_code)
            info = StandardBackend().probe()
            self.assertFalse(info.available)
            launch.assert_not_called()

    def test_manifest_and_artifact_resolution_loops_are_diagnostics(self):
        with runtime():
            manifest_path = self.root / 'libs/runtime-manifest.json'
            artifact = registry.select_library('swmm5')
            original = Path.resolve
            for failing, code in ((manifest_path, 'manifest_invalid'), (artifact.path, 'artifact_invalid'),
                                  (self.root / artifact.record['build_record'], 'artifact_invalid'),
                                  (self.root / artifact.record['dependencies'][0]['path'], 'artifact_invalid')):
                for error_type in (RuntimeError, OSError):
                    def resolve(path, *args, **kwargs):
                        if path == failing:
                            raise error_type('Symlink loop from Path.resolve')
                        return original(path, *args, **kwargs)
                    with self.subTest(path=failing, error=error_type), patch.object(Path, 'resolve', resolve):
                        self.assert_corruption_is_contained(code)

    def test_manifest_existence_errors_and_nonregular_files_are_diagnostics(self):
        manifest = self.root / 'libs/runtime-manifest.json'
        original = Path.exists
        def exists(path):
            if path == manifest:
                raise PermissionError('Cannot inspect manifest')
            return original(path)
        with runtime(), patch.object(Path, 'exists', exists):
            self.assert_corruption_is_contained('manifest_invalid')
        original_file = Path.is_file
        def is_file(path):
            return False if path == manifest else original_file(path)
        with runtime(), patch.object(Path, 'is_file', is_file), patch.object(Path, 'open', side_effect=AssertionError('must not open nonregular manifest')):
            self.assert_corruption_is_contained('manifest_invalid')

    def test_deep_manifest_and_build_record_json_are_diagnostics(self):
        deep = '[' * 20000 + '0' + ']' * 20000
        with runtime():
            manifest_path = self.root / 'libs/runtime-manifest.json'
            manifest_path.write_text(deep)
            self.assert_corruption_is_contained('manifest_invalid')
            self.save()
            artifact = registry.select_library('swmm5')
            evidence = self.root / artifact.record['build_record']
            evidence.write_text(deep)
            self.manifest['targets']['linux-x86_64']['libraries']['swmm5']['build_record_sha256'] = hashlib.sha256(evidence.read_bytes()).hexdigest()
            self.save()
            self.assert_corruption_is_contained('artifact_invalid')

    def test_unbounded_version_numbers_are_invalid_manifest_diagnostics(self):
        self.manifest['targets']['linux-x86_64']['libc']['minimum_version'] = '9' * 5000
        self.save()
        with runtime():
            self.assert_corruption_is_contained('manifest_invalid')

    def test_oversized_build_record_is_rejected_before_hashing(self):
        with runtime():
            artifact = registry.select_library('swmm5')
            evidence = self.root / artifact.record['build_record']
            evidence.write_bytes(b'x' * (1024 * 1024 + 1))
            original = registry._digest
            def digest(path):
                if path == evidence:
                    raise AssertionError('Oversized build record must not be hashed')
                return original(path)
            with patch.object(registry, '_digest', digest), self.assertRaisesRegex(registry.NativeRegistryError, 'size limit'):
                artifact.verify()

    def test_optional_flexible_is_absent_without_substitution(self):
        del self.manifest['targets']['linux-aarch64']['libraries']['flexible_ponding']
        self.save()
        with runtime('Linux', 'aarch64'), patch('subprocess.Popen') as launch:
            self.assertTrue(get_native_capabilities()['swmm_solver'])
            self.assertFalse(get_native_capabilities()['flexible_ponding'])
            info = FlexiblePondingBackend().probe()
            self.assertFalse(info.available)
            self.assertIn('Experimental', info.reason)
            self.assertIn('no fallback', info.reason)
            launch.assert_not_called()

    def test_new_final_hash_confers_known_policy_but_adjacent_user_json_does_not(self):
        with runtime():
            artifact = registry.select_library('swmm5', verify=True)
            self.assertEqual(registry.policy_for_digest('swmm5', artifact.record['sha256']), registry.POLICIES['swmm5'])
            outside = self.root / 'user.so'
            outside.write_bytes(b'unknown user binary')
            outside.with_suffix('.build.json').write_text(json.dumps(dict(numerical_policy=registry.POLICIES['swmm5'])))
            backend = StandardBackend(library=str(outside))
            metadata = dict(library=str(outside), sha256=hashlib.sha256(outside.read_bytes()).hexdigest(),
                            engine_version=52004, platform='Linux', architecture='x86_64', abi='cdecl:64')
            self.assertEqual(backend._loaded(metadata).numerical_policy, 'user-library:unverified')
            metadata.update(library=str(artifact.path), sha256=artifact.record['sha256'])
            info = StandardBackend()._loaded(metadata)
            self.assertTrue(info.available)
            self.assertEqual(info.numerical_policy, registry.POLICIES['swmm5'])
            self.assertIn('easysewer:checkpoint:2', info.capabilities)

    def test_cross_architecture_checkpoint_identity_stays_distinct(self):
        from types import SimpleNamespace
        from easysewer.runtime._checkpoint_lifecycle import CheckpointLifecycle
        from test_checkpoint_container_v2 import snapshot
        value = snapshot(self.root)
        value = replace(value, backend=replace(value.backend, platform='Linux', architecture='x86_64', abi='cdecl:64'))
        metadata = {name: getattr(value.backend, name) for name in
                    ('sha256', 'engine_version', 'platform', 'architecture', 'abi')}
        for changed in (dict(architecture='aarch64'), dict(platform='Darwin'), dict(abi='cdecl:32')):
            solver = SimpleNamespace(open_attempted=False, start_attempted=False, closed=False,
                                     metadata=dict(metadata, **changed))
            with self.subTest(changed=changed), self.assertRaisesRegex(ValueError, 'differs from the actual engine'):
                CheckpointLifecycle(solver, value)

    def test_imports_and_static_discovery_do_not_load_ctypes_or_spawn(self):
        code = '''import sys, subprocess
subprocess.Popen = lambda *a, **k: (_ for _ in ()).throw(AssertionError('spawn'))
from easysewer import get_native_capabilities
from easysewer.runtime import StandardBackend
import easysewer.doctor
get_native_capabilities()
assert 'ctypes' not in sys.modules
'''
        result = subprocess.run([sys.executable, '-I', '-B', '-c',
            'import sys; sys.path.insert(0, ' + repr(str(Path(registry.__file__).resolve().parent.parent)) + ');' + code],
            capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
