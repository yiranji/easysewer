"""Build, bundle and verify one native platform wheel from an audited source tree.

Linux/macOS builds require the pinned public EPA source checkout. The existing
Windows binaries and x64-only experimental example retain their recorded
provenance. No private FlexiblePonding source is fetched by this tool.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import struct
import subprocess
import sys
import sysconfig


TARGETS = {
    'windows-x86_64': ('Windows', 'x86_64', 'win', '.dll.esdll'),
    'linux-x86_64': ('Linux', 'x86_64', 'linux', '.so.esso'),
    'linux-aarch64': ('Linux', 'aarch64', 'linux-aarch64', '.so.esso'),
    'darwin-arm64': ('Darwin', 'arm64', 'macos-arm64', '.dylib.esso'),
}
POLICIES = {'swmm5': 'easysewer:standard:5.2.4:16',
            'swmm-output': 'easysewer:output-io:1',
            'flexible_ponding': 'easysewer:flexible-ponding:incremental-volume:2:native:14'}


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def require_native_host(target):
    system, architecture, _, _ = TARGETS[target]
    machine = platform.machine().lower()
    if system == 'Windows':
        matching = sysconfig.get_platform() == 'win-amd64'
    else:
        aliases = {'amd64': 'x86_64', 'arm64': 'aarch64', 'aarch64': 'aarch64'}
        matching = aliases.get(machine, machine) == aliases.get(architecture, architecture)
    if platform.system() != system or struct.calcsize('P') != 8 or not matching:
        raise ValueError('Native release builds must execute on the selected 64-bit target: ' + target)
    if system == 'Linux' and platform.libc_ver()[0] != 'glibc':
        raise ValueError('Linux native release wheels require glibc; musl is not supported')


def provisional_manifest(package, target):
    system, architecture, directory, suffix = TARGETS[target]
    libraries = {}
    for name, policy in POLICIES.items():
        binary = package / 'libs' / directory / (name + suffix)
        if not binary.is_file():
            if name != 'flexible_ponding':
                raise ValueError('Required native payload is absent: ' + name)
            continue
        build = binary.with_name(name + '.build.json')
        evidence = json.loads(build.read_text(encoding='utf-8'))
        if evidence['sha256'] != digest(binary) or evidence['size'] != binary.stat().st_size:
            raise ValueError('Build evidence does not match native payload: ' + name)
        libraries[name] = dict(path=binary.relative_to(package).as_posix(),
            sha256=digest(binary), size=binary.stat().st_size,
            numerical_policy=policy, build_record=build.relative_to(package).as_posix(),
            build_record_sha256=digest(build), required=name != 'flexible_ponding',
            dependencies=[], external_dependencies=[])
    record = dict(system=system, architecture=architecture, pointer_bits=64, abi='cdecl:64',
                  minimum_os='14.0' if system == 'Darwin' else None,
                  libc={'name': 'glibc', 'minimum_version':
                        '2.33' if architecture == 'x86_64' else '2.28'} if system == 'Linux' else None,
                  libraries=libraries)
    manifest = dict(schema='easysewer:native-runtime:1', targets={target: record})
    (package / 'libs/runtime-manifest.json').write_text(
        json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
    return manifest


def build(root, target, output, *, upstream=None, compiler=None, openmp_prefix=None):
    require_native_host(target)
    root, output = Path(root).resolve(), Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    stage = output / 'stage'
    stage.mkdir()
    shutil.copytree(root / 'src', stage / 'src', ignore=shutil.ignore_patterns(
        '__pycache__', '*.pyc', '*.pyo', '*.egg-info'))
    for name in ('pyproject.toml', 'setup.py', 'README.md'):
        shutil.copyfile(root / name, stage / name)
    for path in root.glob('LICENSE*'):
        if path.is_file():
            shutil.copyfile(path, stage / path.name)
    package = stage / 'src/easysewer'
    system, architecture, directory, suffix = TARGETS[target]
    libraries = package / 'libs'
    for path in libraries.iterdir():
        if path.is_dir() and path.name not in (directory, 'licenses'):
            shutil.rmtree(path)
    destination = libraries / directory
    destination.mkdir(exist_ok=True)
    licenses = libraries / 'licenses'
    licenses.mkdir(exist_ok=True)

    def run(label, command, *, cwd=root, env=None):
        command = [str(value) for value in command]
        with (output / (label + '.log')).open('x', encoding='utf-8') as log:
            subprocess.run(command, cwd=cwd, env=env, stdout=log, stderr=subprocess.STDOUT, check=True)

    compile_core = target in ('linux-aarch64', 'darwin-arm64')
    if compile_core:
        if upstream is None:
            raise ValueError('The pinned public EPA SWMM checkout is required on Linux/macOS')
        for component, name in (('standard', 'swmm5'), ('output', 'swmm-output')):
            prepared = output / ('prepared-' + component)
            run('prepare-' + component, [sys.executable, '-B', root / 'native' / component / 'prepare.py',
                '--source', Path(upstream).resolve(), '--destination', prepared])
            binary = destination / (name + suffix)
            command = [sys.executable, '-B', root / 'native' / component / 'build.py',
                '--source', prepared, '--compiler', compiler or ('clang' if system == 'Darwin' else 'gcc'),
                '--output', binary, '--target', system.lower(), '--architecture', architecture]
            if system == 'Darwin':
                if openmp_prefix is None:
                    raise ValueError('macOS release builds require the pinned native/openmp build prefix')
                command += ['--deployment-target', '14.0', '--openmp-prefix', Path(openmp_prefix).resolve()]
            run('compile-' + component, command)
            binary.with_suffix(binary.suffix + '.json').replace(destination / (name + '.build.json'))
            binary.with_suffix(binary.suffix + '.log').replace(output / (component + '-compiler.log'))
    if system == 'Darwin':
        evidence = Path(openmp_prefix) / 'share/easysewer-openmp'
        shutil.copyfile(evidence / 'LLVM-OpenMP-LICENSE.txt', licenses / 'LLVM-OpenMP-LICENSE.txt')
        shutil.copyfile(evidence / 'build.json', licenses / 'LLVM-OpenMP-build.json')
    else:
        for name in ('GCC-COPYING3.txt', 'GCC-COPYING.RUNTIME.txt', 'GCC-provenance.json'):
            shutil.copyfile(root / 'native/licenses' / name, licenses / name)
    provisional_manifest(package, target)
    raw = output / 'raw'
    raw.mkdir()
    environment = dict(os.environ, EASYSEWER_NATIVE_TARGET=target)
    run('wheel', [sys.executable, '-B', '-c',
        'import sys;from setuptools.build_meta import build_wheel;build_wheel(sys.argv[1])', raw],
        cwd=stage, env=environment)
    wheel, = raw.glob('*.whl')
    final = output / 'wheelhouse'
    run('repair', [sys.executable, '-B', root / 'tools/repair_native_wheel.py',
                  '--wheel', wheel, '--platform', target, '--output', final])
    repaired, = final.glob('*.whl')
    record = dict(target=target, wheel=str(repaired), sha256=digest(repaired),
                  raw_wheel_sha256=digest(wheel), compiled_core=compile_core,
                  experimental_example=target in ('windows-x86_64', 'linux-x86_64'))
    (output / 'build.json').write_text(json.dumps(record, indent=2) + '\n', encoding='utf-8')
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--platform', choices=TARGETS, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--upstream', type=Path)
    parser.add_argument('--compiler')
    parser.add_argument('--openmp-prefix', type=Path)
    args = parser.parse_args()
    print(json.dumps(build(args.source, args.platform, args.output, upstream=args.upstream,
                           compiler=args.compiler, openmp_prefix=args.openmp_prefix), indent=2))


if __name__ == '__main__':
    main()
