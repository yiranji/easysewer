"""Platform wheel staging is explicit, host-bound and provenance checked."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('native_wheel_build', ROOT / 'tools/build_native_wheel.py')
BUILD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BUILD)


class NativeWheelBuildTests(unittest.TestCase):
    def test_native_build_rejects_cross_architecture_and_musl(self):
        with patch.object(BUILD.platform, 'system', return_value='Linux'), \
                patch.object(BUILD.platform, 'machine', return_value='x86_64'), \
                patch.object(BUILD.struct, 'calcsize', return_value=8), \
                patch.object(BUILD.platform, 'libc_ver', return_value=('glibc', '2.33')):
            BUILD.require_native_host('linux-x86_64')
            with self.assertRaisesRegex(ValueError, 'selected 64-bit target'):
                BUILD.require_native_host('linux-aarch64')
            with patch.object(BUILD.platform, 'libc_ver', return_value=('musl', '1.2')):
                with self.assertRaisesRegex(ValueError, 'musl'):
                    BUILD.require_native_host('linux-x86_64')

    def test_windows_target_uses_process_abi_not_host_architecture(self):
        with patch.object(BUILD.platform, 'system', return_value='Windows'), \
                patch.object(BUILD.platform, 'machine', return_value='ARM64'), \
                patch.object(BUILD.struct, 'calcsize', return_value=8), \
                patch.object(BUILD.sysconfig, 'get_platform', return_value='win-amd64'):
            BUILD.require_native_host('windows-x86_64')
            with patch.object(BUILD.sysconfig, 'get_platform', return_value='win-arm64'):
                with self.assertRaises(ValueError):
                    BUILD.require_native_host('windows-x86_64')

    def fixture(self, root, target, *, flexible=False):
        _, _, directory, suffix = BUILD.TARGETS[target]
        folder = root / 'libs' / directory
        folder.mkdir(parents=True)
        for name in (('swmm5', 'swmm-output', 'flexible_ponding') if flexible else ('swmm5', 'swmm-output')):
            binary = folder / (name + suffix)
            binary.write_bytes(b'test native bytes: ' + name.encode())
            (folder / (name + '.build.json')).write_text(json.dumps({
                'sha256': BUILD.digest(binary), 'size': binary.stat().st_size}), encoding='utf-8')

    def test_manifest_requires_core_but_not_experimental_example(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.fixture(root, 'linux-aarch64')
            manifest = BUILD.provisional_manifest(root, 'linux-aarch64')
            target = manifest['targets']['linux-aarch64']
            self.assertEqual(target['libc']['minimum_version'], '2.28')
            self.assertEqual(set(target['libraries']), {'swmm5', 'swmm-output'})
            self.assertTrue(all(row['required'] for row in target['libraries'].values()))

    def test_x64_floor_and_experimental_status_are_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.fixture(root, 'linux-x86_64', flexible=True)
            target = BUILD.provisional_manifest(root, 'linux-x86_64')['targets']['linux-x86_64']
            self.assertEqual(target['libc']['minimum_version'], '2.33')
            self.assertFalse(target['libraries']['flexible_ponding']['required'])

    def test_changed_build_artifact_cannot_be_sealed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.fixture(root, 'darwin-arm64')
            binary = root / 'libs/macos-arm64/swmm5.dylib.esso'
            binary.write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError, 'Build evidence'):
                BUILD.provisional_manifest(root, 'darwin-arm64')

    def test_core_missing_cannot_be_sealed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'libs').mkdir()
            with self.assertRaisesRegex(ValueError, 'Required native payload'):
                BUILD.provisional_manifest(root, 'darwin-arm64')


if __name__ == '__main__':
    unittest.main()
