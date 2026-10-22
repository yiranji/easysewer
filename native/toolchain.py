"""Explicit native toolchain flags shared by the public Standard and OUT builds.

These are build-time choices, not a claim of runtime qualification. Wheel repair
and execution on the actual target remain separate mandatory release gates.
"""

import hashlib
from pathlib import Path
import platform
import struct


TARGETS = {
    'windows': ('x86_64',),
    'linux': ('x86_64', 'aarch64'),
    'darwin': ('arm64',),
}


def canonical_architecture(machine, target):
    value = machine.lower()
    if value in ('amd64', 'x64', 'x86_64'):
        return 'x86_64'
    if value in ('arm64', 'aarch64'):
        return 'arm64' if target == 'darwin' else 'aarch64'
    return value


def configuration(target, *, architecture=None, deployment_target=None,
                  openmp_prefix=None, openmp=False, output_name=None):
    """Return flags and evidence, rejecting accidental ARM cross-builds.

    Existing MinGW x64 cross-build commands remain supported. ARM releases must
    be built on their matching native OS and architecture, never under Rosetta
    or an x86-64 cross compiler. CI additionally executes the final wheel.
    """
    if target not in TARGETS:
        raise ValueError('Unsupported native target: ' + str(target))
    host_arch = canonical_architecture(platform.machine(), target)
    architecture = canonical_architecture(architecture or host_arch, target)
    if architecture not in TARGETS[target]:
        raise ValueError(f'Unsupported native target: {target}-{architecture}')
    if struct.calcsize('P') != 8:
        raise ValueError('Native builds require 64-bit Python')
    if architecture in ('arm64', 'aarch64'):
        if host_arch != architecture or platform.system().lower() != target:
            raise ValueError('ARM native builds require a matching native host OS and process architecture')
    if target != 'darwin' and (deployment_target is not None or openmp_prefix is not None):
        raise ValueError('Deployment target and OpenMP prefix are Darwin-only options')
    flags = ['-O2', '-fno-fast-math', '-ffp-contract=off']
    link_flags = []
    if target == 'windows':
        flags += ['-shared', '-march=x86-64', '-mtune=generic', '-static', '-static-libgcc',
                  '-Wl,--no-insert-timestamp', '-Wl,--image-base,0x180000000']
    elif target == 'linux':
        flags += ['-shared', '-fPIC']
        flags += (['-march=x86-64', '-mtune=generic'] if architecture == 'x86_64'
                  else ['-march=armv8-a'])
    else:
        deployment_target = deployment_target or '14.0'
        if deployment_target != '14.0':
            raise ValueError('The qualified Darwin deployment target is macOS 14.0')
        flags += ['-dynamiclib', '-fPIC', '-arch', 'arm64', '-mmacosx-version-min=14.0']
        if output_name:
            flags += ['-Wl,-install_name,@rpath/' + Path(output_name).name]
    if openmp:
        if target == 'darwin':
            if openmp_prefix is None:
                raise ValueError('Darwin solver builds require the pinned build-time OpenMP prefix')
            prefix = Path(openmp_prefix).resolve(strict=True)
            header = prefix / 'include/omp.h'
            runtime = prefix / 'lib/libomp.dylib'
            if not header.is_file() or not runtime.is_file():
                raise ValueError('OpenMP prefix must contain include/omp.h and lib/libomp.dylib')
            # Apple Clang supports OpenMP parsing, but does not ship the runtime.
            flags += ['-Xpreprocessor', '-fopenmp', '-I' + str(prefix / 'include')]
            link_flags += [str(runtime), '-Wl,-rpath,' + str(prefix / 'lib')]
        else:
            flags += ['-fopenmp']
    link_flags += ['-lm']
    evidence = dict(architecture=architecture, pointer_bits=64, abi='cdecl:64',
                    deployment_target=deployment_target,
                    toolchain_recipe_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
    return flags, link_flags, evidence
