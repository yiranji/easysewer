"""Build pinned, host-only LLVM libomp for the macOS arm64 release wheel.

Use the official LLVM checkout at source.json's commit, with sparse-checkout
paths ``openmp cmake runtimes llvm/cmake third-party/unittest``. No fetching,
Homebrew bottles, or runtime installation is performed by this recipe.
Outputs must be outside the verified source tree.
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


HERE = Path(__file__).resolve().parent


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def git(source, *args):
    return subprocess.check_output(['git', '-C', str(source), *args])


def verify_source(source):
    source = Path(source).resolve(strict=True)
    lock = json.loads((HERE / 'source.json').read_text(encoding='utf-8'))
    if git(source, 'rev-parse', 'HEAD').decode().strip() != lock['commit']:
        raise ValueError('LLVM source is not the pinned release commit')
    for directory, expected in lock['trees'].items():
        if git(source, 'rev-parse', 'HEAD:' + directory).decode().strip() != expected:
            raise ValueError('Unexpected LLVM source tree: ' + directory)
    entries = git(source, 'ls-tree', '-rz', 'HEAD', '--', *lock['trees']).split(b'\0')
    files = {}
    for entry in entries:
        if not entry:
            continue
        metadata, encoded_name = entry.split(b'\t', 1)
        mode, kind, oid = metadata.split()
        name = encoded_name.decode('utf-8')
        path = source / name
        if kind != b'blob' or mode not in (b'100644', b'100755') or path.is_symlink():
            raise ValueError('Unexpected LLVM source entry: ' + name)
        raw = path.read_bytes()
        actual = hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest()
        if actual != oid.decode():
            raise ValueError('LLVM source bytes changed: ' + name)
        files[name] = digest(raw)
    # Do not let untracked/ignored CMake fragments become undocumented inputs.
    if git(source, 'ls-files', '--others', '--', *lock['trees']).strip():
        raise ValueError('Untracked files exist inside the pinned LLVM source scopes')
    for name, expected in lock['files'].items():
        if files.get(name) != expected:
            raise ValueError('LLVM source anchor differs: ' + name)
    return lock, files


def build(source, build_directory, prefix, *, cmake='cmake', compiler='clang', cxx='clang++', jobs=3):
    if platform.system() != 'Darwin' or platform.machine().lower() != 'arm64' or struct.calcsize('P') != 8:
        raise ValueError('The libomp release recipe requires native macOS arm64 and 64-bit Python')
    if not isinstance(jobs, int) or isinstance(jobs, bool) or jobs < 1:
        raise ValueError('OpenMP build parallelism must be a positive integer')
    source = Path(source).resolve(strict=True)
    build_directory = Path(build_directory).resolve()
    prefix = Path(prefix).resolve()
    for destination in (build_directory, prefix):
        if destination == source or destination.is_relative_to(source):
            raise ValueError('OpenMP build/install outputs must be outside the source checkout')
        if destination.exists() and any(destination.iterdir()):
            raise ValueError('OpenMP build/install destinations must be empty')
    if build_directory == prefix or build_directory.is_relative_to(prefix) or prefix.is_relative_to(build_directory):
        raise ValueError('OpenMP build/install directories must be separate')
    lock, files = verify_source(source)
    build_directory.mkdir(parents=True, exist_ok=True)
    prefix.mkdir(parents=True, exist_ok=True)
    commands = [
        [str(cmake), '-S', str(source / 'runtimes'), '-B', str(build_directory),
         '-DCMAKE_BUILD_TYPE=Release', '-DCMAKE_INSTALL_PREFIX=' + str(prefix),
         '-DCMAKE_C_COMPILER=' + str(compiler), '-DCMAKE_CXX_COMPILER=' + str(cxx),
         '-DCMAKE_OSX_ARCHITECTURES=arm64', '-DCMAKE_OSX_DEPLOYMENT_TARGET=14.0',
         '-DCMAKE_C_FLAGS=-fno-fast-math -ffp-contract=off',
         '-DCMAKE_CXX_FLAGS=-fno-fast-math -ffp-contract=off',
         '-DLLVM_ENABLE_RUNTIMES=openmp', '-DLLVM_ENABLE_PER_TARGET_RUNTIME_DIR=OFF',
         '-DLLVM_INCLUDE_TESTS=OFF', '-DLLVM_INCLUDE_DOCS=OFF',
         '-DRUNTIMES_FORTRAN_MODULES=OFF',
         '-DCMAKE_DISABLE_FIND_PACKAGE_LLVM=ON', '-DCMAKE_DISABLE_FIND_PACKAGE_Clang=ON',
         '-DLIBOMP_ENABLE_SHARED=ON', '-DLIBOMP_INSTALL_ALIASES=OFF',
         '-DOPENMP_ENABLE_LIBOMPTARGET=OFF', '-DOPENMP_ENABLE_OMPT_TOOLS=OFF',
         '-DLIBOMP_OMPD_SUPPORT=OFF'],
        # LLVM23 configures GoogleTest even with LLVM_INCLUDE_TESTS=OFF. Its
        # sources are locked, but build only omp; do not build/install tests.
        [str(cmake), '--build', str(build_directory), '--target', 'omp', '--parallel', str(jobs)],
        [str(cmake), '--install', str(build_directory)],
    ]
    environment = os.environ.copy()
    environment['SOURCE_DATE_EPOCH'] = str(lock['commit_timestamp'])
    environment['MACOSX_DEPLOYMENT_TARGET'] = '14.0'
    environment['PYTHONDONTWRITEBYTECODE'] = '1'
    with (build_directory / 'build.log').open('w', encoding='utf-8') as log:
        for command in commands:
            log.write(repr(command) + '\n'); log.flush()
            subprocess.run(command, env=environment, stdout=log, stderr=subprocess.STDOUT, check=True)
    runtime = prefix / 'lib/libomp.dylib'
    if not runtime.is_file() or not (prefix / 'include/omp.h').is_file():
        raise ValueError('Pinned OpenMP build did not install the required library and header')
    evidence_dir = prefix / 'share/easysewer-openmp'
    evidence_dir.mkdir(parents=True, exist_ok=True)
    license_path = evidence_dir / 'LLVM-OpenMP-LICENSE.txt'
    shutil.copyfile(source / 'openmp/LICENSE.TXT', license_path)
    record = dict(source=lock, source_files=files, commands=commands,
                  source_manifest_sha256=digest(json.dumps(files, sort_keys=True).encode()),
                  recipe_sha256=digest(Path(__file__).read_bytes()),
                  compiler=subprocess.check_output([str(compiler), '--version'], text=True),
                  host=platform.platform(), architecture='arm64', minimum_os='14.0',
                  runtime=dict(path='lib/libomp.dylib', sha256=digest(runtime.read_bytes()), size=runtime.stat().st_size),
                  license=dict(path='share/easysewer-openmp/LLVM-OpenMP-LICENSE.txt', sha256=digest(license_path.read_bytes())))
    (evidence_dir / 'build.json').write_text(json.dumps(record, indent=2) + '\n', encoding='utf-8')
    return record


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True)
    parser.add_argument('--build', required=True)
    parser.add_argument('--prefix', required=True)
    parser.add_argument('--cmake', default='cmake')
    parser.add_argument('--compiler', default='clang')
    parser.add_argument('--cxx', default='clang++')
    parser.add_argument('--jobs', type=int, default=3)
    args = parser.parse_args()
    result = build(args.source, args.build, args.prefix, cmake=args.cmake,
                   compiler=args.compiler, cxx=args.cxx, jobs=args.jobs)
    print(json.dumps(result['runtime'], indent=2))
