"""Repair native dependencies, audit final bytes, and seal the runtime manifest.

The order is intentional: compile -> wheel -> auditwheel/delocate -> dependency
and baseline audit -> final build records/manifest -> wheel RECORD. No native
payload may change after the manifest is written. This tool does not qualify a
wheel by executing it; release CI must install and exercise it on its native OS.
"""

import argparse
import base64
import csv
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import zipfile


TARGETS = {
    'windows-x86_64': dict(system='Windows', architecture='x86_64', directory='win',
                          minimum_os=None, libc=None, tag='win_amd64', suffix='.dll.esdll'),
    'linux-x86_64': dict(system='Linux', architecture='x86_64', directory='linux',
                        minimum_os=None, libc={'name': 'glibc', 'minimum_version': '2.33'},
                        tag='manylinux_2_33_x86_64', repair_tag='manylinux_2_34_x86_64', suffix='.so.esso'),
    'linux-aarch64': dict(system='Linux', architecture='aarch64', directory='linux-aarch64',
                         minimum_os=None, libc={'name': 'glibc', 'minimum_version': '2.28'},
                         tag='manylinux_2_28_aarch64', repair_tag='manylinux_2_28_aarch64', suffix='.so.esso'),
    'darwin-arm64': dict(system='Darwin', architecture='arm64', directory='macos-arm64',
                        minimum_os='14.0', libc=None, tag='macosx_14_0_arm64', suffix='.dylib.esso'),
}
POLICIES = {
    'swmm5': 'easysewer:standard:5.2.4:16',
    'swmm-output': 'easysewer:output-io:1',
    'flexible_ponding': 'easysewer:flexible-ponding:incremental-volume:2:native:14',
}
# This C-only release permits only glibc itself as an external Linux runtime.
# In particular, a newer libgcc/libstdc++ ABI must not escape the glibc audit.
LINUX_SYSTEM = frozenset(('libc.so.6', 'libm.so.6', 'libpthread.so.0', 'libdl.so.2',
    'librt.so.1', 'libutil.so.1', 'libresolv.so.2',
    'ld-linux-x86-64.so.2', 'ld-linux-aarch64.so.1'))
LICENSE_HASHES = {
    'GCC-COPYING3.txt': '8ceb4b9ee5adedde47b31e975c1d90c73ad27b6b165a1dcd80c7c545eb65b903',
    'GCC-COPYING.RUNTIME.txt': '9d6b43ce4d8de0c878bf16b54d8e7a10d9bd42b75178153e3af6a815bdc90f74',
    'LLVM-OpenMP-LICENSE.txt': 'fdad1758a9e1f9d5a81e18879b3406772115edc92c24bfa36b70c654f325e8e4',
}
WINDOWS_SYSTEM = frozenset(('kernel32.dll', 'msvcrt.dll', 'ucrtbase.dll'))
MAGICS = (b'\x7fELF', b'\xcf\xfa\xed\xfe', b'\xfe\xed\xfa\xcf',
          b'\xca\xfe\xba\xbe', b'\xbe\xba\xfe\xca', b'\xca\xfe\xba\xbf', b'\xbf\xba\xfe\xca')


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def file_record(path, package):
    return dict(path=Path(path).relative_to(package).as_posix(), sha256=sha256(path),
                size=Path(path).stat().st_size)


def run(command, *, env=None):
    environment = dict(os.environ if env is None else env, LC_ALL='C')
    result = subprocess.run([str(value) for value in command], env=environment,
                            capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError('Native audit/repair failed: ' + repr(command) + '\n' + result.stdout + result.stderr)
    return result.stdout + result.stderr


def safe_extract(wheel, destination):
    """Read wheel contents without permitting links, duplicates, or path escapes."""
    with zipfile.ZipFile(wheel) as archive:
        seen = set()
        for entry in archive.infolist():
            name = entry.filename
            relative = PurePosixPath(name)
            if (not name or name in seen or '\\' in name or relative.is_absolute()
                    or any(part in ('', '.', '..') for part in name.rstrip('/').split('/'))
                    or ':' in name or stat.S_ISLNK(entry.external_attr >> 16)):
                raise ValueError('Unsafe or duplicate wheel member: ' + name)
            seen.add(name)
            path = destination.joinpath(*relative.parts)
            if entry.is_dir():
                path.mkdir(parents=True, exist_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(archive.read(entry))
                mode = (entry.external_attr >> 16) & 0o777
                if mode:
                    path.chmod(mode)


def _cstring(raw, offset, end=None):
    if offset < 0 or offset >= len(raw) or (end is not None and offset >= end):
        raise ValueError('Invalid native string offset')
    stop = raw.find(b'\0', offset, len(raw) if end is None else end)
    if stop < 0:
        raise ValueError('Unterminated native string')
    return raw[offset:stop].decode('utf-8')


def inspect_pe(path, raw):
    """Read PE32+ import names without requiring a Windows compiler at runtime."""
    if len(raw) < 64:
        raise ValueError('Truncated PE header')
    pe = struct.unpack_from('<I', raw, 0x3c)[0]
    if raw[pe:pe+4] != b'PE\0\0' or pe + 24 > len(raw):
        raise ValueError('Invalid PE signature')
    machine, sections = struct.unpack_from('<HH', raw, pe + 4)
    optional_size = struct.unpack_from('<H', raw, pe + 20)[0]
    optional = pe + 24
    if machine != 0x8664 or optional + optional_size > len(raw) or optional_size < 128:
        raise ValueError('Expected a complete x86-64 PE image')
    if struct.unpack_from('<H', raw, optional)[0] != 0x20b:
        raise ValueError('Expected PE32+ image')
    section_offset = optional + optional_size
    if section_offset + sections * 40 > len(raw):
        raise ValueError('Truncated PE section table')
    ranges = []
    for index in range(sections):
        virtual_size, rva, size, offset = struct.unpack_from('<IIII', raw, section_offset + index*40 + 8)
        ranges.append((rva, max(virtual_size, size), offset, size))

    def file_offset(rva):
        for start, length, offset, size in ranges:
            if start <= rva < start + length:
                delta = rva - start
                if delta >= size or offset + delta >= len(raw):
                    break
                return offset + delta
        raise ValueError('Invalid PE import RVA')

    imports = []
    number_of_directories = struct.unpack_from('<I', raw, optional + 108)[0]
    if number_of_directories > 1:
        rva, size = struct.unpack_from('<II', raw, optional + 120)
        if rva:
            cursor = file_offset(rva)
            limit = min(len(raw), cursor + size)
            while cursor + 20 <= limit:
                descriptor = struct.unpack_from('<IIIII', raw, cursor)
                if not any(descriptor):
                    break
                imports.append(_cstring(raw, file_offset(descriptor[3])))
                cursor += 20
            else:
                raise ValueError('Unterminated PE import directory')
    # Delay-load dependencies must not bypass the dependency closure audit.
    if number_of_directories > 13 and optional_size >= 112 + 14*8:
        delay_rva, _ = struct.unpack_from('<II', raw, optional + 112 + 13*8)
        if delay_rva:
            raise ValueError('Delay-loaded PE dependencies are not supported by this release recipe')
    return dict(format='PE', architecture='x86_64', needed=imports, rpaths=[], soname=path.name)


def inspect_macho(path, raw):
    # Only thin little-endian arm64 is supported, never universal2/Intel/Rosetta.
    if raw[:4] != b'\xcf\xfa\xed\xfe' or len(raw) < 32:
        raise ValueError('Expected a thin little-endian 64-bit Mach-O image')
    _, cpu, _, filetype, count, command_bytes, _, _ = struct.unpack_from('<8I', raw)
    if cpu != 0x100000c or filetype != 6 or command_bytes > len(raw)-32:
        raise ValueError('Expected an arm64 Mach-O dynamic library')
    needed, rpaths, minimum = [], [], None
    soname = path.name
    cursor = 32
    for _ in range(count):
        if cursor + 8 > 32 + command_bytes:
            raise ValueError('Truncated Mach-O command table')
        command, size = struct.unpack_from('<II', raw, cursor)
        if size < 8 or cursor + size > 32 + command_bytes:
            raise ValueError('Invalid Mach-O load command')
        if command in (0xc, 0xd, 0x80000018, 0x8000001f, 0x80000023):
            if size < 24:
                raise ValueError('Truncated Mach-O dylib command')
            offset = struct.unpack_from('<I', raw, cursor + 8)[0]
            value = _cstring(raw, cursor + offset, cursor + size)
            if command == 0xd:
                soname = value
            else:
                needed.append(value)
        elif command == 0x8000001c:
            if size < 12:
                raise ValueError('Truncated Mach-O RPATH command')
            offset = struct.unpack_from('<I', raw, cursor + 8)[0]
            rpaths.append(_cstring(raw, cursor + offset, cursor + size))
        elif command in (0x32, 0x24):
            if size < (24 if command == 0x32 else 16):
                raise ValueError('Truncated Mach-O deployment target')
            if command == 0x32 and struct.unpack_from('<I', raw, cursor+8)[0] != 1:
                raise ValueError('Mach-O platform is not macOS')
            version = struct.unpack_from('<I', raw, cursor + (12 if command == 0x32 else 8))[0]
            minimum = (version >> 16, (version >> 8) & 255, version & 255)
        cursor += size
    if cursor != 32 + command_bytes or minimum is None:
        raise ValueError('Invalid Mach-O command table or missing minimum OS')
    return dict(format='Mach-O', architecture='arm64', needed=needed, rpaths=rpaths,
                soname=soname, minimum_os='.'.join(map(str, minimum)))


def elf_version_needs(versions):
    """Keep imported symbol versions separate from a library's own exports."""
    needs, counts = {}, {}
    active, provider, expected = False, None, None
    for line in versions.splitlines():
        if line.startswith('Version '):
            active = line.startswith('Version needs section ')
            if active:
                count = re.search(r'contains (\d+) entr', line)
                if count is None or expected is not None:
                    raise ValueError('Invalid ELF symbol-version requirements section')
                expected = int(count.group(1))
        if not active:
            continue
        match = re.search(r'\bFile:\s+(\S+)\s+Cnt:\s+(\d+)', line)
        if match:
            provider, count = match.groups()
            if provider in needs:
                raise ValueError('Duplicate ELF symbol-version provider')
            needs[provider], counts[provider] = [], int(count)
        match = re.search(r'\bName:\s+(\S+)\s+Flags:', line)
        if match:
            if provider is None:
                raise ValueError('ELF symbol version has no provider')
            needs[provider].append(match.group(1))
    if ((expected is not None and len(needs) != expected)
            or any(len(needs[name]) != count for name, count in counts.items())):
        raise ValueError('Incomplete ELF symbol-version requirements')
    return needs


def inspect_image(path):
    path = Path(path)
    raw = path.read_bytes()
    if raw[:4] == b'\x7fELF':
        if len(raw) < 64 or raw[4:6] != b'\x02\x01':
            raise ValueError('Expected little-endian ELF64: ' + str(path))
        kind, machine = struct.unpack_from('<HH', raw, 16)
        if kind != 3 or machine not in (62, 183):
            raise ValueError('Unsupported ELF shared object: ' + str(path))
        dynamic = run(['readelf', '--wide', '--dynamic', path])
        versions = run(['readelf', '--wide', '--version-info', path])
        notes = run(['readelf', '--wide', '--notes', path])
        requirements = elf_version_needs(versions)
        sonames = re.findall(r'\(SONAME\).*?\[([^\]]+)\]', dynamic)
        rpaths = []
        for value in re.findall(r'\((?:RPATH|RUNPATH)\).*?\[([^\]]*)\]', dynamic):
            rpaths.extend(value.split(':'))
        return dict(format='ELF', architecture={62: 'x86_64', 183: 'aarch64'}[machine],
                    needed=re.findall(r'\(NEEDED\).*?\[([^\]]+)\]', dynamic),
                    rpaths=rpaths, soname=sonames[0] if sonames else None,
                    version_needs=requirements,
                    relr=bool(re.search(r'\((?:RELR|RELRSZ|RELRENT)\)', dynamic)),
                    x86_isa_needed=re.findall(r'x86 ISA needed:\s*([^\n]+)', notes),
                    glibc_versions=sorted({v[6:] for names in requirements.values() for v in names
                                           if re.fullmatch(r'GLIBC_\d+(?:\.\d+)+', v)}))
    if raw[:2] == b'MZ':
        return inspect_pe(path, raw)
    if raw[:4] in MAGICS:
        return inspect_macho(path, raw)
    raise ValueError('Unrecognized native image: ' + str(path))


def _version(value):
    parts = tuple(map(int, value.split('.')))
    return parts + (0,) * (3-len(parts))


def _inside(path, package):
    resolved = path.resolve()
    if not resolved.is_relative_to(package.resolve()):
        raise ValueError('Native loader path escapes installed package: ' + str(path))
    return resolved


def audit_linux_abi(info, spec, path):
    """Check the final ELF ABI, including every bundled runtime, not its tag.

    GNU ISA-needed notes catch declared CPU requirements. Their absence is not
    disassembly proof; pinned baseline compiler inputs and native release tests
    remain necessary. CET compatibility markers (IBT/SHSTK) are not ISA levels.
    """
    minimum = spec['libc']['minimum_version']
    if any(_version(v) > _version(minimum) for v in info['glibc_versions']):
        raise ValueError('Native image requires glibc newer than ' + minimum + ': ' + str(path))
    if info['relr']:
        raise ValueError('DT_RELR requires a newer glibc baseline: ' + str(path))
    if info['architecture'] == 'x86_64':
        for declaration in info['x86_isa_needed']:
            if set(re.split(r'[, ]+', declaration.strip())) != {'x86-64-baseline'}:
                raise ValueError('Native image requires a non-baseline x86 ISA: ' + str(path))
    for provider, versions in info['version_needs'].items():
        if provider not in info['needed']:
            raise ValueError('ELF symbol-version provider is not a declared dependency: ' + provider)
        if provider in LINUX_SYSTEM:
            for version in versions:
                match = re.fullmatch(r'GLIBC_(\d+(?:\.\d+)+)', version)
                if not match:
                    raise ValueError('Unrecognized external glibc symbol version: ' + version)
                if _version(match.group(1)) > _version(minimum):
                    raise ValueError('Native image requires glibc newer than ' + minimum + ': ' + str(path))
    loader = {'x86_64': 'ld-linux-x86-64.so.2', 'aarch64': 'ld-linux-aarch64.so.1'}[spec['architecture']]
    if any(name.startswith('ld-linux-') and name != loader for name in info['needed']):
        raise ValueError('Native image requires the wrong ELF system loader')


def _loader_directory(value, image, package, system):
    token = '$ORIGIN' if system == 'Linux' else '@loader_path'
    value = value.replace('${ORIGIN}', '$ORIGIN') if system == 'Linux' else value
    if value != token and not value.startswith(token + '/'):
        raise ValueError('Native image contains an unsafe/non-relative loader path: ' + value)
    return _inside(image.parent / value[len(token):].lstrip('/'), package)


def audit_native_tree(package, target):
    """Check every native image and resolve its non-system dependency closure."""
    package = Path(package).resolve()
    spec = TARGETS[target]
    native_directory = package / 'libs' / spec['directory']
    images = {}
    for path in package.rglob('*'):
        if not path.is_file():
            continue
        if path.is_symlink():
            raise ValueError('Native wheel must not contain symlinks')
        with path.open('rb') as stream:
            magic = stream.read(4)
        if magic in MAGICS or magic[:2] == b'MZ':
            if not path.is_relative_to(native_directory):
                raise ValueError('Wheel contains a foreign/unregistered native payload: ' + str(path))
            images[path] = inspect_image(path)
    roots = {}
    for name in POLICIES:
        path = native_directory / (name + spec['suffix'])
        if path.exists():
            if name == 'flexible_ponding' and spec['architecture'] != 'x86_64':
                raise ValueError('FlexiblePonding is an optional x64 example, not an ARM payload')
            if path not in images:
                raise ValueError('Unrecognized required native payload: ' + name)
            roots[name] = path
        elif name != 'flexible_ponding':
            raise ValueError('Required native payload is missing: ' + name)
    edges = {}
    for path, info in images.items():
        if info['architecture'] != spec['architecture']:
            raise ValueError('Native image architecture differs from wheel target: ' + str(path))
        expected_format = {'Linux': 'ELF', 'Darwin': 'Mach-O', 'Windows': 'PE'}[spec['system']]
        if info['format'] != expected_format:
            raise ValueError('Native image format differs from wheel target')
        if spec['system'] == 'Linux':
            audit_linux_abi(info, spec, path)
            if path not in roots.values() and (info['soname'] != path.name or not
                    re.fullmatch(r'.+-[0-9a-f]{8,}\.so(?:\.[0-9]+)*', path.name)):
                raise ValueError('Bundled ELF runtime requires a collision-resistant SONAME: ' + str(path))
        if spec['system'] == 'Darwin':
            if _version(info['minimum_os']) > _version(spec['minimum_os']):
                raise ValueError('Native image requires macOS newer than ' + spec['minimum_os'] + ': ' + str(path))
            run(['codesign', '--verify', '--strict', path])
        search = [_loader_directory(value, path, package, spec['system']) for value in info['rpaths']]
        dependencies = []
        for name in info['needed']:
            if spec['system'] == 'Linux':
                if name in LINUX_SYSTEM:
                    continue
                if '/' in name:
                    raise ValueError('ELF dependency contains an absolute/relative build path: ' + name)
                candidates = [directory / name for directory in search]
            elif spec['system'] == 'Darwin':
                if name.startswith(('/usr/lib/', '/System/Library/')):
                    continue
                if name.startswith('@loader_path/'):
                    candidates = [_inside(path.parent / name[len('@loader_path/'):], package)]
                elif name.startswith('@rpath/'):
                    candidates = [directory / name[len('@rpath/'):] for directory in search]
                else:
                    raise ValueError('Mach-O dependency contains a non-relative build path: ' + name)
            else:
                lower = name.lower()
                if lower in WINDOWS_SYSTEM or lower.startswith('api-ms-win-crt-'):
                    continue
                # This release uses statically linked MinGW runtimes. Do not
                # silently rely on PATH or add unsafe DLL search directories.
                raise ValueError('Unbundled Windows runtime dependency: ' + name)
            matches = [candidate.resolve() for candidate in candidates if candidate.is_file()]
            if not matches or matches[0] not in images:
                raise ValueError('Unresolved package-relative native dependency: ' + name)
            dependency = _inside(matches[0], package)
            if dependency == path:
                raise ValueError('Native image depends on itself')
            dependencies.append(dependency)
        edges[path] = dependencies
    closures = {}
    for name, root in roots.items():
        found, todo = set(), list(edges[root])
        while todo:
            dependency = todo.pop()
            if dependency not in found:
                found.add(dependency)
                todo.extend(edges[dependency])
        if root in found:
            raise ValueError('Cyclic native dependency graph')
        closures[name] = sorted(found)
    dependency_images = set(images) - set(roots.values())
    reachable = set().union(*map(set, closures.values()))
    if dependency_images != reachable:
        raise ValueError('Wheel contains unregistered/orphan native dependency images')
    if spec['system'] in ('Linux', 'Darwin'):
        prefix = 'libgomp' if spec['system'] == 'Linux' else 'libomp'
        if not any(path.name.startswith(prefix) for path in closures['swmm5']):
            raise ValueError('Standard solver does not bundle its required OpenMP runtime')
    return roots, closures, {path.relative_to(package).as_posix(): info for path, info in images.items()}


def _check_record(name, record):
    command = record.get('command', [])
    if '-fno-fast-math' not in command or '-ffp-contract=off' not in command:
        raise ValueError('Native build does not preserve the floating-point policy')
    if any(flag in command for flag in ('-ffast-math', '-Ofast', '-ffp-contract=fast', '-march=native')):
        raise ValueError('Unsafe native compiler optimization policy')
    if name == 'swmm5':
        if record.get('standard_fixes') != 16 or record.get('source', {}).get('patch') != POLICIES[name]:
            raise ValueError('Unrecognized Standard source/build policy')
        if record.get('source', {}).get('base', {}).get('commit') != '7952ca837988b1c32f791812eccc9fd64547e093':
            raise ValueError('Standard source is not the pinned EPA commit')
    elif name == 'swmm-output':
        if record.get('source', {}).get('base', {}).get('commit') != '7952ca837988b1c32f791812eccc9fd64547e093':
            raise ValueError('Output-reader source is not the pinned EPA commit')
        if record.get('output_io') != 1 or record.get('source', {}).get('patch') != POLICIES[name]:
            raise ValueError('Unrecognized output-reader build policy')
    elif record.get('native_io_fixes') != 14 or record.get('abi') != 201:
        raise ValueError('Unrecognized legacy FlexiblePonding build policy')


def openmp_build_record(package):
    path = Path(package) / 'libs/licenses/LLVM-OpenMP-build.json'
    record = json.loads(path.read_text(encoding='utf-8'))
    lock = json.loads((Path(__file__).resolve().parents[1] / 'native/openmp/source.json').read_text(encoding='utf-8'))
    if record.get('source') != lock or record.get('architecture') != 'arm64' or record.get('minimum_os') != lock['minimum_os']:
        raise ValueError('OpenMP runtime build evidence does not match the pinned native source recipe')
    return path, record


def verify_openmp_input(package, solver):
    """Validate the compile-time libomp bytes before delocate rewrites them."""
    _, record = openmp_build_record(package)
    image = inspect_image(solver)
    names = [name for name in image['needed'] if Path(name).name == 'libomp.dylib']
    if len(names) != 1:
        raise ValueError('Expected exactly one pinned compile-time libomp dependency')
    name = names[0]
    if name.startswith('@rpath/'):
        candidates = [Path(value) / Path(name).name for value in image['rpaths'] if value.startswith('/')]
    elif name.startswith('/'):
        candidates = [Path(name)]
    else:
        raise ValueError('Cannot resolve the explicit build-time OpenMP prefix')
    runtime = next((path for path in candidates if path.is_file()), None)
    expected = record.get('runtime', {})
    if runtime is None or sha256(runtime) != expected.get('sha256') or runtime.stat().st_size != expected.get('size'):
        raise ValueError('Compile-time OpenMP runtime does not match its pinned build evidence')


def verify_input_records(package, target):
    spec = TARGETS[target]
    directory = Path(package) / 'libs' / spec['directory']
    for name in POLICIES:
        path = directory / (name + spec['suffix'])
        if not path.is_file():
            if name != 'flexible_ponding':
                raise ValueError('Missing compiled payload: ' + name)
            continue
        record = json.loads((directory / (name + '.build.json')).read_text(encoding='utf-8'))
        _check_record(name, record)
        if record.get('sha256') != sha256(path) or record.get('size') != path.stat().st_size:
            raise ValueError('Compiled payload does not match its build evidence: ' + name)
    if spec['system'] == 'Darwin':
        verify_openmp_input(package, directory / ('swmm5' + spec['suffix']))


def finalize_tree(package, target, *, repair_evidence=None):
    package = Path(package).resolve()
    spec = TARGETS[target]
    roots, closures, audit = audit_native_tree(package, target)
    expected_licenses = (['LLVM-OpenMP-LICENSE.txt'] if spec['system'] == 'Darwin'
                         else ['GCC-COPYING3.txt', 'GCC-COPYING.RUNTIME.txt'])
    licenses = []
    for name in expected_licenses:
        path = package / 'libs/licenses' / name
        if not path.is_file() or not path.stat().st_size:
            raise ValueError('Missing bundled-runtime license: ' + name)
        if sha256(path) != LICENSE_HASHES[name]:
            raise ValueError('Bundled-runtime license differs from the verified upstream text: ' + name)
        licenses.append(file_record(path, package))
    runtime_records = []
    if spec['system'] == 'Darwin':
        runtime_record_path, runtime_record = openmp_build_record(package)
        runtimes = [dependency for dependency in closures['swmm5'] if dependency.name.startswith('libomp')]
        if len(runtimes) != 1:
            raise ValueError('Expected exactly one bundled LLVM OpenMP runtime')
        runtime_record['bundled_runtime'] = file_record(runtimes[0], package)
        runtime_record['bundled_runtime']['native_audit'] = audit[runtimes[0].relative_to(package).as_posix()]
        runtime_record_path.write_text(json.dumps(runtime_record, indent=2) + '\n', encoding='utf-8')
        runtime_records.append(file_record(runtime_record_path, package))
    libraries = {}
    for name, path in roots.items():
        record_path = path.parent / (name + '.build.json')
        record = json.loads(record_path.read_text(encoding='utf-8'))
        _check_record(name, record)
        record.setdefault('compile_sha256', record['sha256'])
        record.setdefault('compile_size', record['size'])
        record['sha256'] = sha256(path)
        record['size'] = path.stat().st_size
        record['architecture'] = spec['architecture']
        record['pointer_bits'] = 64
        record['calling_abi'] = 'cdecl:64'  # FlexiblePonding's numerical ABI stays 201.
        dependencies = [file_record(dependency, package) for dependency in closures[name]]
        record['repair'] = dict(repair_evidence or {}, target=target, dependencies=dependencies,
                               licenses=licenses, runtime_build_records=runtime_records,
                               native_audit=audit[path.relative_to(package).as_posix()])
        record_path.write_text(json.dumps(record, indent=2) + '\n', encoding='utf-8')
        libraries[name] = dict(file_record(path, package), numerical_policy=POLICIES[name],
                              build_record=record_path.relative_to(package).as_posix(),
                              build_record_sha256=sha256(record_path), required=name != 'flexible_ponding',
                              dependencies=dependencies, external_dependencies=[])
    manifest = dict(schema='easysewer:native-runtime:1', targets={target: dict(
        system=spec['system'], architecture=spec['architecture'], pointer_bits=64, abi='cdecl:64',
        minimum_os=spec['minimum_os'], libc=spec['libc'], libraries=libraries)})
    (package / 'libs/runtime-manifest.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
    return dict(manifest=manifest, native_audit=audit, licenses=licenses)


def retag_final_tree(tree, filename, target):
    """Seal the PEP 600 tag only after independently auditing the final tree.

    auditwheel has no 2.33 policy. Its 2.34 intermediate supplies dependency
    bundling, not permission to promise 2.33 compatibility. PEP 600 permits a
    producer's own policy: https://peps.python.org/pep-0600/#core-definition.
    The native glibc 2.33 clean-install release job must also pass before upload.
    """
    spec = TARGETS[target]
    if target != 'linux-x86_64':
        return filename
    wheel_info, = tree.glob('*.dist-info/WHEEL')
    metadata = wheel_info.read_text(encoding='utf-8')
    repaired_tag = 'py3-none-' + spec['repair_tag']
    if re.findall(r'^Tag: (.+)$', metadata, re.MULTILINE) != [repaired_tag]:
        raise ValueError('Unexpected auditwheel tags before final x64 ABI audit')
    if not filename.endswith('-' + repaired_tag + '.whl'):
        raise ValueError('Repaired wheel filename/tag mismatch')
    audit_native_tree(tree / 'easysewer', target)
    final_tag = 'py3-none-' + spec['tag']
    metadata = metadata.replace('Tag: ' + repaired_tag, 'Tag: ' + final_tag)
    wheel_info.write_text(metadata, encoding='utf-8')
    return filename[:-len(repaired_tag + '.whl')] + final_tag + '.whl'


def write_wheel(tree, destination, target):
    """Write final RECORD only after all repaired payload/evidence hashes settle."""
    wheel_info, = tree.glob('*.dist-info/WHEEL')
    metadata = wheel_info.read_text(encoding='utf-8')
    tags = re.findall(r'^Tag: (.+)$', metadata, re.MULTILINE)
    expected = 'py3-none-' + TARGETS[target]['tag']
    if tags != [expected] or 'Root-Is-Purelib: false' not in metadata:
        raise ValueError('Final wheel metadata must contain exactly the truthful native platform tag: ' + expected)
    if not destination.name.endswith('-' + expected + '.whl'):
        raise ValueError('Final wheel filename/tag mismatch')
    record_path = wheel_info.with_name('RECORD')
    rows = []
    for path in sorted(tree.rglob('*')):
        if not path.is_file() or path == record_path:
            continue
        raw = path.read_bytes()
        digest = base64.urlsafe_b64encode(hashlib.sha256(raw).digest()).rstrip(b'=').decode()
        rows.append([path.relative_to(tree).as_posix(), 'sha256=' + digest, str(len(raw))])
    rows.append([record_path.relative_to(tree).as_posix(), '', ''])
    buffer = io.StringIO(newline='')
    csv.writer(buffer, lineterminator='\n').writerows(rows)
    record_path.write_text(buffer.getvalue(), encoding='utf-8', newline='')
    # Fixed ZIP metadata makes the sealing step reproducible for fixed inputs.
    with zipfile.ZipFile(destination, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(tree.rglob('*')):
            if not path.is_file():
                continue
            info = zipfile.ZipInfo(path.relative_to(tree).as_posix(), (2023, 10, 10, 10, 9, 34))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (stat.S_IFREG | (path.stat().st_mode & 0o777)) << 16
            archive.writestr(info, path.read_bytes())


def canonical_repair_command(command, wheel, workspace):
    """Remove only transient invocation locations from shipped provenance.

    Actual paths remain in the external repair evidence. Keep all other
    arguments untouched, including source/toolchain locations and audit flags.
    """
    wheel, workspace = Path(wheel), Path(workspace)
    prefix = str(workspace) + os.sep
    result = []
    for argument in command:
        if argument == str(wheel):
            result.append('<input-wheel>/' + wheel.name)
        elif argument == str(workspace):
            result.append('<repair-workspace>')
        elif argument.startswith(prefix):
            result.append('<repair-workspace>/' + Path(argument).relative_to(workspace).as_posix())
        else:
            result.append(argument)
    return result


def repair(wheel, target, output):
    if target not in TARGETS:
        raise ValueError('Unknown native wheel target: ' + target)
    wheel = Path(wheel).resolve(strict=True)
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    spec = TARGETS[target]
    environment = os.environ.copy()
    # Never let arbitrary runtime lookup settings conceal missing dependencies.
    for key in ('LD_LIBRARY_PATH', 'DYLD_LIBRARY_PATH', 'DYLD_FALLBACK_LIBRARY_PATH'):
        environment.pop(key, None)
    with tempfile.TemporaryDirectory(prefix='easysewer-wheel-repair-') as temporary:
        workspace = Path(temporary)
        initial = workspace / 'initial'
        safe_extract(wheel, initial)
        verify_input_records(initial / 'easysewer', target)
        repaired = workspace / 'repaired'
        repaired.mkdir()
        if spec['system'] == 'Linux':
            command = ['auditwheel', 'repair', '--plat', spec['repair_tag'], '--only-plat',
                       '--lib-sdir', '/libs/' + spec['directory'] + '/.libs', '-w', str(repaired), str(wheel)]
            version = run(['auditwheel', '--version'], env=environment).strip()
            log = run(command, env=environment)
        elif spec['system'] == 'Darwin':
            environment['MACOSX_DEPLOYMENT_TARGET'] = spec['minimum_os']
            command = ['delocate-wheel', '--require-archs', 'arm64', '--sanitize-rpaths',
                       '--lib-sdir', 'libs/' + spec['directory'] + '/.libs', '-w', str(repaired), str(wheel)]
            version = run(['delocate-wheel', '--version'], env=environment).strip()
            log = run(command, env=environment)
        else:
            command, version, log = [], 'no repair: static MinGW runtime', ''
            shutil.copyfile(wheel, repaired / wheel.name)
        repaired_wheels = list(repaired.glob('*.whl'))
        if len(repaired_wheels) != 1:
            raise ValueError('Expected exactly one repaired native wheel')
        repaired_wheel = repaired_wheels[0]
        tree = workspace / 'final'
        safe_extract(repaired_wheel, tree)
        result = finalize_tree(tree / 'easysewer', target,
                               repair_evidence=dict(command=canonical_repair_command(command, wheel, workspace),
                                   tool_version=version,
                                   final_platform_tag=spec['tag']))
        # auditwheel has no glibc2.33 policy. Existing x64 binaries keep their
        # real 2.33 floor: repair under 2.34, audit the complete final ABI and
        # dependency closure, then seal the independently qualified PEP 600 tag.
        final_name = retag_final_tree(tree, repaired_wheel.name, target)
        destination = output / final_name
        if destination.exists():
            raise ValueError('Refusing to overwrite a previously finalized wheel: ' + str(destination))
        write_wheel(tree, destination, target)
        (output / (destination.stem + '.repair.log')).write_text(
            'command: ' + json.dumps(command) + '\n' + log, encoding='utf-8')
        evidence = dict(wheel=str(destination), sha256=sha256(destination), target=target,
                        repair_command=command, repair_tool_version=version,
                        native_audit=result['native_audit'], licenses=result['licenses'],
                        recipe_sha256=sha256(__file__))
        (output / (destination.stem + '.repair.json')).write_text(json.dumps(evidence, indent=2) + '\n', encoding='utf-8')
        return evidence


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--wheel', required=True)
    parser.add_argument('--platform', choices=tuple(TARGETS), required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    print(json.dumps(repair(args.wheel, args.platform, args.output), indent=2))
