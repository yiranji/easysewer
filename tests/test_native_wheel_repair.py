"""Final-byte manifest, dependency closure and platform-boundary regression checks."""

import base64
import csv
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
import stat
import struct
import tempfile
import unittest
from unittest import mock
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('tested_wheel_repair', ROOT/'tools/repair_native_wheel.py')
repair = importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(repair)


def macho(*, minimum=(14,0,0), cpu=0x100000c, needed=(), rpaths=()):
    version=minimum[0]<<16 | minimum[1]<<8 | minimum[2]
    commands=[struct.pack('<6I',0x32,24,1,version,version,0)]
    for value, kind, header in [(x,0xc,24) for x in needed]+[(x,0x8000001c,12) for x in rpaths]:
        raw=value.encode()+b'\0'; size=(header+len(raw)+7)//8*8
        command=struct.pack('<3I',kind,size,header)+b'\0'*(header-12)+raw
        commands.append(command+b'\0'*(size-len(command)))
    return struct.pack('<8I',0xfeedfacf,cpu,0,6,len(commands),sum(map(len,commands)),0,0)+b''.join(commands)


class NativeImageAuditTests(unittest.TestCase):
    def test_real_existing_windows_images_have_only_system_dependencies(self):
        for path in (ROOT/'src/easysewer/libs/win').glob('*.esdll'):
            with self.subTest(path=path.name):
                info=repair.inspect_image(path)
                self.assertEqual(info['architecture'],'x86_64')
                self.assertEqual(set(info['needed']),{'KERNEL32.dll','msvcrt.dll'})

    def test_thin_arm64_macho_headers_capture_deployment_and_loader_dependencies(self):
        info=repair.inspect_macho(Path('solver'),macho(needed=('@loader_path/.libs/libomp.dylib',)))
        self.assertEqual(info['minimum_os'],'14.0.0')
        self.assertEqual(info['needed'],['@loader_path/.libs/libomp.dylib'])
        for raw in (macho(cpu=0x1000007),b'\xca\xfe\xba\xbe'+b'\0'*50,macho()[:-4]):
            with self.assertRaises(ValueError):
                repair.inspect_macho(Path('solver'),raw)

    def test_relative_paths_must_stay_inside_package(self):
        with tempfile.TemporaryDirectory() as temporary:
            package=Path(temporary); path=package/'libs/linux/swmm5.so.esso'
            expected=(package/'libs/linux/.libs').resolve()
            self.assertEqual(repair._loader_directory('$ORIGIN/.libs',path,package,'Linux'),expected)
            for value in ('/usr/local/lib', '$ORIGIN/../../../../outside', ''):
                with self.assertRaises(ValueError):
                    repair._loader_directory(value,path,package,'Linux')

    def test_macho_dependency_closure_requires_bundled_runtime_and_actual_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            package=Path(temporary); directory=package/'libs/macos-arm64'; deps=directory/'.libs'
            deps.mkdir(parents=True)
            solver=directory/'swmm5.dylib.esso'; output=directory/'swmm-output.dylib.esso'; runtime=deps/'libomp.dylib'
            solver.write_bytes(macho(needed=('@loader_path/.libs/libomp.dylib',)))
            output.write_bytes(macho(needed=('/usr/lib/libSystem.B.dylib',)))
            runtime.write_bytes(macho(needed=('/usr/lib/libSystem.B.dylib',)))
            with mock.patch.object(repair,'run',return_value=''):
                roots, closures, _=repair.audit_native_tree(package,'darwin-arm64')
                self.assertEqual(closures['swmm5'],[runtime.resolve()])
                runtime.write_bytes(macho(minimum=(16,0,0)))
                with self.assertRaisesRegex(ValueError,'newer than 14.0'):
                    repair.audit_native_tree(package,'darwin-arm64')
                runtime.unlink()
                with self.assertRaisesRegex(ValueError,'Unresolved'):
                    repair.audit_native_tree(package,'darwin-arm64')

    def test_mac_build_prefix_dependency_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            package=Path(temporary); directory=package/'libs/macos-arm64';directory.mkdir(parents=True)
            (directory/'swmm5.dylib.esso').write_bytes(macho(needed=('/opt/homebrew/lib/libomp.dylib',)))
            (directory/'swmm-output.dylib.esso').write_bytes(macho())
            with mock.patch.object(repair,'run',return_value=''), self.assertRaisesRegex(ValueError,'non-relative build path'):
                repair.audit_native_tree(package,'darwin-arm64')


    def test_each_linux_architecture_audits_runtime_symbols_against_its_own_floor(self):
        for target, floor, rejected in (('linux-x86_64','2.33','2.34'), ('linux-aarch64','2.28','2.29')):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as temporary:
                package=Path(temporary); spec=repair.TARGETS[target]
                directory=package/'libs'/spec['directory']; deps=directory/'.libs'; deps.mkdir(parents=True)
                for name in ('swmm5.so.esso','swmm-output.so.esso'):
                    (directory/name).write_bytes(b'\x7fELFfixture')
                runtime=deps/'libgomp-test.so.1'; runtime.write_bytes(b'\x7fELFfixture')
                runtime_version=floor
                def inspect(path):
                    solver=path.name=='swmm5.so.esso'
                    return dict(format='ELF',architecture=spec['architecture'],
                        needed=['libgomp-test.so.1'] if solver else ['libc.so.6'],
                        rpaths=['$ORIGIN/.libs'] if solver else [],soname=path.name,
                        glibc_versions=[runtime_version if path==runtime else floor])
                with mock.patch.object(repair,'inspect_image',side_effect=inspect):
                    repair.audit_native_tree(package,target)
                    runtime_version=rejected
                    with self.assertRaisesRegex(ValueError,'newer than '+floor):
                        repair.audit_native_tree(package,target)

    def test_pinned_openmp_compile_bytes_are_checked_before_repair(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary); package=root/'easysewer'; licenses=package/'libs/licenses'; licenses.mkdir(parents=True)
            runtime=root/'prefix/lib/libomp.dylib'; runtime.parent.mkdir(parents=True);runtime.write_bytes(macho())
            solver=package/'solver';solver.write_bytes(macho(needed=('@rpath/libomp.dylib',),rpaths=(str(runtime.parent),)))
            lock=json.loads((ROOT/'native/openmp/source.json').read_text())
            record=dict(source=lock,architecture='arm64',minimum_os='14.0',runtime=dict(sha256=repair.sha256(runtime),size=runtime.stat().st_size))
            path=licenses/'LLVM-OpenMP-build.json';path.write_text(json.dumps(record))
            repair.verify_openmp_input(package,solver)
            runtime.write_bytes(runtime.read_bytes()+b'changed')
            with self.assertRaisesRegex(ValueError,'does not match'):
                repair.verify_openmp_input(package,solver)
            record['source']['commit']='different';path.write_text(json.dumps(record))
            with self.assertRaisesRegex(ValueError,'pinned native source'):
                repair.openmp_build_record(package)


class WheelFinalizationTests(unittest.TestCase):
    def stage_windows(self, root):
        package=root/'easysewer'; directory=package/'libs/win'
        shutil.copytree(ROOT/'src/easysewer/libs/win',directory)
        licenses=package/'libs/licenses';licenses.mkdir()
        for name in ('GCC-COPYING3.txt','GCC-COPYING.RUNTIME.txt'):
            shutil.copyfile(ROOT/'native/licenses'/name,licenses/name)
        return package

    def test_only_transient_repair_command_path_arguments_are_canonicalized(self):
        workspace=Path('/tmp/repair-random-one'); wheel=Path('/input/random-one/package.whl')
        command=['delocate-wheel','--require-archs','arm64','-w',str(workspace/'repaired'),str(wheel),
                 '/stable/toolchain/path',str(workspace)+'-not-a-child',
                 '--opaque='+str(workspace/'unchanged')]
        canonical=repair.canonical_repair_command(command,wheel,workspace)
        self.assertEqual(canonical[:6],['delocate-wheel','--require-archs','arm64','-w',
                         '<repair-workspace>/repaired','<input-wheel>/package.whl'])
        self.assertEqual(canonical[6:],command[6:])
        self.assertEqual(command[4],str(workspace/'repaired'))

    def test_identical_native_inputs_seal_identically_across_repair_workspaces(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary); wheels=[]; records=[]
            for number in (1,2):
                tree=root/f'tree-{number}'; package=self.stage_windows(tree)
                info=tree/'easysewer-2.0.1.dist-info';info.mkdir()
                (info/'WHEEL').write_text('Root-Is-Purelib: false\nTag: py3-none-win_amd64\n')
                workspace=root/f'random-repair-{number}'
                wheel=root/f'random-input-{number}'/'input.whl'
                # The command is metadata under test; real PE auditing and
                # final-byte manifest/RECORD sealing still execute unchanged.
                command=['fixture-repair','-w',str(workspace/'repaired'),str(wheel)]
                result=repair.finalize_tree(package,'windows-x86_64',repair_evidence={
                    'command':repair.canonical_repair_command(command,wheel,workspace),
                    'tool_version':'same-toolchain-fixture'})
                records.append(result)
                destination=root/f'output-{number}'/'easysewer-2.0.1-py3-none-win_amd64.whl'
                destination.parent.mkdir()
                repair.write_wheel(tree,destination,'windows-x86_64')
                wheels.append(destination.read_bytes())
            self.assertEqual(records[0],records[1])
            self.assertEqual(wheels[0],wheels[1])

    def test_final_manifest_hashes_are_post_repair_and_records_are_bound(self):
        with tempfile.TemporaryDirectory() as temporary:
            package=self.stage_windows(Path(temporary))
            repair.verify_input_records(package,'windows-x86_64')
            solver=package/'libs/win/swmm5.dll.esdll'
            original=repair.sha256(solver)
            solver.write_bytes(solver.read_bytes()+b'\0repair-byte-change')
            result=repair.finalize_tree(package,'windows-x86_64',repair_evidence={'tool_version':'test'})
            entry=result['manifest']['targets']['windows-x86_64']['libraries']['swmm5']
            self.assertNotEqual(entry['sha256'],original)
            self.assertEqual(entry['sha256'],repair.sha256(solver))
            build_path=package/entry['build_record'];record=json.loads(build_path.read_text())
            self.assertEqual(record['compile_sha256'],original)
            self.assertEqual(record['sha256'],entry['sha256'])
            self.assertEqual(entry['build_record_sha256'],repair.sha256(build_path))
            self.assertIs(entry['required'],True)
            flexible=result['manifest']['targets']['windows-x86_64']['libraries']['flexible_ponding']
            self.assertIs(flexible['required'],False)
            self.assertEqual(flexible['external_dependencies'],[])
            flexible_record=json.loads((package/flexible['build_record']).read_text())
            self.assertEqual(flexible_record['abi'],201)
            self.assertEqual(flexible_record['calling_abi'],'cdecl:64')

    def test_missing_or_changed_license_and_input_payload_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            package=self.stage_windows(Path(temporary))
            license_path=package/'libs/licenses/GCC-COPYING3.txt';license_path.write_text('not the license')
            with self.assertRaisesRegex(ValueError,'license differs'):
                repair.finalize_tree(package,'windows-x86_64')
            (package/'libs/win/swmm5.dll.esdll').write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError,'does not match'):
                repair.verify_input_records(package,'windows-x86_64')

    def test_unsafe_and_duplicate_wheel_members_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            for index,name in enumerate(('../outside','/absolute','easysewer\\evil', 'easysewer/../evil')):
                wheel=root/f'{index}.whl'
                with zipfile.ZipFile(wheel,'w') as archive:archive.writestr(name,b'test')
                with self.assertRaisesRegex(ValueError,'Unsafe'):
                    repair.safe_extract(wheel,root/f'output{index}')
            wheel=root/'link.whl'
            with zipfile.ZipFile(wheel,'w') as archive:
                info=zipfile.ZipInfo('easysewer/link');info.external_attr=(stat.S_IFLNK|0o777)<<16
                archive.writestr(info,b'/outside')
            with self.assertRaisesRegex(ValueError,'Unsafe'):
                repair.safe_extract(wheel,root/'link-output')

    def test_x64_conservative_retag_removes_manylinux_claim_not_runtime_floor(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);info=root/'easysewer-2.0.1.dist-info';info.mkdir()
            metadata=info/'WHEEL';metadata.write_text('Root-Is-Purelib: false\nTag: py3-none-manylinux_2_34_x86_64\n')
            filename=repair.retag_final_tree(root,'easysewer-2.0.1-py3-none-manylinux_2_34_x86_64.whl','linux-x86_64')
            self.assertEqual(filename,'easysewer-2.0.1-py3-none-linux_x86_64.whl')
            self.assertNotIn('manylinux',metadata.read_text())
            self.assertEqual(repair.TARGETS['linux-x86_64']['libc']['minimum_version'],'2.33')
            metadata.write_text('Root-Is-Purelib: false\nTag: py3-none-manylinux_2_28_x86_64\n')
            with self.assertRaisesRegex(ValueError,'Unexpected auditwheel'):
                repair.retag_final_tree(root,'easysewer-2.0.1-py3-none-manylinux_2_28_x86_64.whl','linux-x86_64')

    def test_mac_final_evidence_preserves_compiled_runtime_and_binds_bundled_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            package=Path(temporary)/'easysewer';directory=package/'libs/macos-arm64';deps=directory/'.libs';deps.mkdir(parents=True)
            for name in ('swmm5','swmm-output'):
                path=directory/(name+'.dylib.esso')
                path.write_bytes(macho(needed=('@loader_path/.libs/libomp.dylib',) if name=='swmm5' else ()))
                shutil.copyfile(ROOT/'src/easysewer/libs/linux'/(name+'.build.json'),directory/(name+'.build.json'))
            runtime=deps/'libomp.dylib';runtime.write_bytes(macho())
            licenses=package/'libs/licenses';licenses.mkdir()
            shutil.copyfile(ROOT/'native/licenses/LLVM-OpenMP-LICENSE.txt',licenses/'LLVM-OpenMP-LICENSE.txt')
            lock=json.loads((ROOT/'native/openmp/source.json').read_text())
            source_record=dict(source=lock,architecture='arm64',minimum_os='14.0',runtime=dict(sha256='original_compile_hash',size=1))
            build=licenses/'LLVM-OpenMP-build.json';build.write_text(json.dumps(source_record))
            with mock.patch.object(repair,'run',return_value=''):
                result=repair.finalize_tree(package,'darwin-arm64')
            record=json.loads(build.read_text())
            self.assertEqual(record['runtime']['sha256'],'original_compile_hash')
            self.assertEqual(record['bundled_runtime']['sha256'],repair.sha256(runtime))
            entry=result['manifest']['targets']['darwin-arm64']['libraries']['swmm5']
            solver_record=json.loads((package/entry['build_record']).read_text())
            self.assertEqual(solver_record['repair']['runtime_build_records'][0]['sha256'],repair.sha256(build))

    def test_record_is_written_last_and_native_wheel_cannot_be_any(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary); tree=root/'tree';info=tree/'easysewer-2.0.1.dist-info'; info.mkdir(parents=True)
            (tree/'easysewer').mkdir();(tree/'easysewer/payload').write_bytes(b'final')
            (info/'WHEEL').write_text('Wheel-Version: 1.0\nRoot-Is-Purelib: false\nTag: py3-none-win_amd64\n')
            destination=root/'easysewer-2.0.1-py3-none-win_amd64.whl'
            repair.write_wheel(tree,destination,'windows-x86_64')
            with zipfile.ZipFile(destination) as archive:
                rows=list(csv.reader(io.StringIO(archive.read('easysewer-2.0.1.dist-info/RECORD').decode())))
                by_name={row[0]:row for row in rows}
                value=base64.urlsafe_b64encode(hashlib.sha256(b'final').digest()).rstrip(b'=').decode()
                self.assertEqual(by_name['easysewer/payload'][1:],[f'sha256={value}','5'])
            (info/'WHEEL').write_text('Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n')
            with self.assertRaisesRegex(ValueError,'truthful native platform tag'):
                repair.write_wheel(tree,root/'bad.whl','windows-x86_64')


if __name__ == '__main__':
    unittest.main()
