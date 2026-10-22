"""Build-policy unit checks; ARM execution is covered by native CI, not mocks."""

from contextlib import contextmanager
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


toolchain = load('tested_native_toolchain', 'native/toolchain.py')
openmp = load('tested_openmp_build', 'native/openmp/build.py')


@contextmanager
def host(system, machine, bits=64):
    with mock.patch.object(toolchain.platform, 'system', return_value=system), \
         mock.patch.object(toolchain.platform, 'machine', return_value=machine), \
         mock.patch.object(toolchain.struct, 'calcsize', return_value=bits//8):
        yield


class NativeToolchainTests(unittest.TestCase):
    def test_linux_native_architectures_preserve_strict_floating_point(self):
        for architecture in ('x86_64', 'aarch64'):
            with self.subTest(architecture=architecture), host('Linux', architecture):
                flags, links, record = toolchain.configuration('linux', architecture=architecture, openmp=True)
                self.assertIn('-fno-fast-math', flags)
                self.assertIn('-ffp-contract=off', flags)
                self.assertIn('-fopenmp', flags)
                self.assertNotIn('-ffast-math', flags)
                self.assertNotIn('-march=native', flags)
                self.assertEqual(record['architecture'], architecture)
                self.assertEqual(record['abi'], 'cdecl:64')
                self.assertEqual(links, ['-lm'])

    def test_arm_cannot_be_claimed_from_x64_or_wrong_os(self):
        for system, machine, target, architecture in (
                ('Linux', 'x86_64', 'linux', 'aarch64'),
                ('Darwin', 'x86_64', 'darwin', 'arm64'),
                ('Linux', 'aarch64', 'darwin', 'arm64')):
            with self.subTest(target=target, host=(system,machine)), host(system, machine):
                with self.assertRaisesRegex(ValueError, 'matching native'):
                    toolchain.configuration(target, architecture=architecture)

    def test_excluded_architectures_and_32bit_python_are_rejected(self):
        for target, architecture in (('windows','arm64'), ('darwin','x86_64'), ('linux','armv7l')):
            with self.assertRaisesRegex(ValueError, 'Unsupported'):
                toolchain.configuration(target, architecture=architecture)
        with host('Linux', 'x86_64', 32), self.assertRaisesRegex(ValueError, '64-bit'):
            toolchain.configuration('linux')

    def test_darwin_build_prefix_is_explicit_and_never_a_runtime_requirement(self):
        with tempfile.TemporaryDirectory() as temporary, host('Darwin', 'arm64'):
            prefix = Path(temporary)
            (prefix/'include').mkdir(); (prefix/'lib').mkdir()
            (prefix/'include/omp.h').write_text('/* test */')
            (prefix/'lib/libomp.dylib').write_bytes(b'test')
            flags, links, record = toolchain.configuration('darwin', openmp=True,
                openmp_prefix=prefix, output_name='swmm5.dylib.esso')
            self.assertIn('-dynamiclib', flags)
            self.assertIn('-mmacosx-version-min=14.0', flags)
            self.assertIn('-Xpreprocessor', flags)
            self.assertIn('-Wl,-install_name,@rpath/swmm5.dylib.esso', flags)
            self.assertIn(str(prefix/'lib/libomp.dylib'), links)
            self.assertEqual(record['deployment_target'], '14.0')
            with self.assertRaisesRegex(ValueError, 'OpenMP prefix'):
                toolchain.configuration('darwin', openmp=True)
            with self.assertRaisesRegex(ValueError, '14.0'):
                toolchain.configuration('darwin', deployment_target='11.0')

    def test_windows_x64_developer_cross_recipe_retains_static_runtime_flags(self):
        with host('Linux','x86_64'):
            flags, _, _ = toolchain.configuration('windows', architecture='AMD64', openmp=True)
        for flag in ('-static','-static-libgcc','-Wl,--no-insert-timestamp','-Wl,--image-base,0x180000000'):
            self.assertIn(flag, flags)

    def test_darwin_only_options_are_not_silently_ignored(self):
        with self.assertRaisesRegex(ValueError, 'Darwin-only'):
            toolchain.configuration('linux', deployment_target='14.0')


class OpenMPSourceLockTests(unittest.TestCase):
    def test_official_release_and_license_are_pinned(self):
        record=json.loads((ROOT/'native/openmp/source.json').read_text())
        self.assertEqual(record['repository'],'https://github.com/llvm/llvm-project.git')
        self.assertEqual(record['tag'],'llvmorg-23.1.4')
        self.assertEqual(record['commit'],'7a941dfb2877a0b8cf30323fefa85ad019e0b0f5')
        self.assertEqual(set(record['trees']),{'openmp','cmake','runtimes','llvm/cmake','third-party/unittest'})
        self.assertEqual(record['minimum_os'],'14.0')
        license_bytes=(ROOT/'native/licenses/LLVM-OpenMP-LICENSE.txt').read_bytes()
        self.assertEqual(record['files']['openmp/LICENSE.TXT'],hashlib.sha256(license_bytes).hexdigest())

    def test_host_only_recipe_uses_locked_runtimes_entry_and_keeps_macos_floor(self):
        # This inspects command construction, not native ARM execution.
        lock=json.loads((ROOT/'native/openmp/source.json').read_text())
        with tempfile.TemporaryDirectory() as temporary, host('Darwin','arm64'):
            root=Path(temporary); source=root/'source'; prefix=root/'prefix'
            (source/'openmp').mkdir(parents=True)
            (source/'openmp/LICENSE.TXT').write_bytes((ROOT/'native/licenses/LLVM-OpenMP-LICENSE.txt').read_bytes())
            def run(command, **kwargs):
                self.assertEqual(kwargs['env']['MACOSX_DEPLOYMENT_TARGET'],'14.0')
                self.assertEqual(kwargs['env']['SOURCE_DATE_EPOCH'],str(lock['commit_timestamp']))
                self.assertEqual(kwargs['env']['PYTHONDONTWRITEBYTECODE'],'1')
                if '--install' in command:
                    (prefix/'lib').mkdir(); (prefix/'include').mkdir()
                    (prefix/'lib/libomp.dylib').write_bytes(b'unit fixture, not native code')
                    (prefix/'include/omp.h').write_bytes(b'unit fixture')
            with mock.patch.object(openmp,'verify_source',return_value=(lock,lock['files'])), \
                 mock.patch.object(openmp.subprocess,'run',side_effect=run), \
                 mock.patch.object(openmp.subprocess,'check_output',return_value='test compiler'):
                record=openmp.build(source,root/'build',prefix,jobs=2)
            configure,compile_command,install=record['commands']
            self.assertEqual(configure[configure.index('-S')+1],str(source/'runtimes'))
            for flag in ('-DLLVM_ENABLE_RUNTIMES=openmp','-DLLVM_INCLUDE_TESTS=OFF',
                         '-DLLVM_INCLUDE_DOCS=OFF','-DLLVM_ENABLE_PER_TARGET_RUNTIME_DIR=OFF',
                         '-DRUNTIMES_FORTRAN_MODULES=OFF','-DOPENMP_ENABLE_LIBOMPTARGET=OFF',
                         '-DOPENMP_ENABLE_OMPT_TOOLS=OFF','-DLIBOMP_OMPD_SUPPORT=OFF',
                         '-DCMAKE_DISABLE_FIND_PACKAGE_LLVM=ON','-DCMAKE_DISABLE_FIND_PACKAGE_Clang=ON',
                         '-DCMAKE_OSX_ARCHITECTURES=arm64','-DCMAKE_OSX_DEPLOYMENT_TARGET=14.0',
                         '-DCMAKE_C_FLAGS=-fno-fast-math -ffp-contract=off',
                         '-DCMAKE_CXX_FLAGS=-fno-fast-math -ffp-contract=off'):
                self.assertIn(flag,configure)
            self.assertEqual(compile_command[compile_command.index('--target')+1],'omp')
            self.assertEqual(record['source'],lock)
            self.assertEqual(record['minimum_os'],'14.0')
            self.assertEqual(record['runtime']['sha256'],hashlib.sha256((prefix/'lib/libomp.dylib').read_bytes()).hexdigest())

    def test_source_verification_rejects_dirty_or_untracked_input(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary); source=root/'source'; source.mkdir()
            recipe=root/'recipe'; recipe.mkdir()
            path=source/'openmp/test.c'; path.parent.mkdir(); path.write_bytes(b'original\n')
            data=path.read_bytes()
            oid=hashlib.sha1(b'blob '+str(len(data)).encode()+b'\0'+data).hexdigest()
            lock={'commit':'pinned','trees':{'openmp':'tree'},'files':{'openmp/test.c':hashlib.sha256(data).hexdigest()}}
            (recipe/'source.json').write_text(json.dumps(lock))
            responses={('rev-parse','HEAD'):b'pinned\n', ('rev-parse','HEAD:openmp'):b'tree\n',
                       ('ls-tree','-rz','HEAD','--','openmp'):f'100644 blob {oid}\topenmp/test.c\0'.encode(),
                       ('ls-files','--others','--','openmp'):b''}
            with mock.patch.object(openmp,'HERE',recipe), mock.patch.object(openmp,'git',side_effect=lambda _,*a:responses[a]):
                self.assertEqual(openmp.verify_source(source)[1]['openmp/test.c'],hashlib.sha256(data).hexdigest())
                path.write_bytes(b'changed\n')
                with self.assertRaisesRegex(ValueError,'bytes changed'):
                    openmp.verify_source(source)
                path.write_bytes(data)
                responses[('ls-files','--others','--','openmp')]=b'openmp/hidden.cmake\n'
                with self.assertRaisesRegex(ValueError,'Untracked'):
                    openmp.verify_source(source)

    def test_libomp_release_build_is_not_executable_on_x64_linux(self):
        with host('Linux','x86_64'), self.assertRaisesRegex(ValueError,'native macOS arm64'):
            openmp.build('unused','unused','unused')


if __name__ == '__main__':
    unittest.main()
