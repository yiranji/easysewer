"""Read-only platform/ABI and final-artifact registry; never loads native code.

The manifest is part of the installed package, not a claim read from an arbitrary
user library's adjacent files. Its hashes describe final, dependency-repaired
artifacts. A missing manifest is normal for the pure profile; an invalid present
manifest is never replaced by a guessed filename or another architecture.
"""
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import struct
import sys
import sysconfig

SCHEMA = 'easysewer:native-runtime:1'
POLICIES = {
    'swmm5': 'easysewer:standard:5.2.4:16',
    'swmm-output': 'easysewer:output-io:1',
    'flexible_ponding': 'easysewer:flexible-ponding:incremental-volume:2:native:14',
}
TARGETS = {
    'windows-x86_64': ('Windows', 'x86_64'),
    'linux-x86_64': ('Linux', 'x86_64'),
    'linux-aarch64': ('Linux', 'aarch64'),
    'darwin-arm64': ('Darwin', 'arm64'),
}
_SHA = re.compile(r'[0-9a-f]{64}\Z')
_VERSION = re.compile(r'\d+(?:\.\d+)*\Z')


class NativeRegistryError(OSError):
    """A stable diagnostic category, independent of human-readable wording."""
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def runtime_identity():
    """Describe the executing interpreter, not a translated host CPU alone.

    Windows sysconfig identifies native ARM versus emulated x64 Python. Darwin's
    uname machine describes the active slice (including Rosetta); a universal2
    build tag alone cannot identify that slice. Linux SOABI identifies the built
    interpreter when available, including an emulated interpreter on an ARM host.
    """
    system, machine = platform.system(), platform.machine()
    python_platform = sysconfig.get_platform()
    architecture = machine.lower()
    if system == 'Windows':
        architecture = {'win-amd64': 'x86_64', 'win-arm64': 'arm64', 'win32': 'x86'}.get(
            python_platform, 'unknown')
    elif system == 'Linux':
        soabi = sysconfig.get_config_var('SOABI') or ''
        # Only use the architecture portion of a Linux interpreter ABI tag.
        if '-x86_64-linux-' in soabi:
            architecture = 'x86_64'
        elif '-aarch64-linux-' in soabi:
            architecture = 'aarch64'
    architecture = {'amd64': 'x86_64', 'x64': 'x86_64'}.get(architecture, architecture)
    if system == 'Linux' and architecture == 'arm64':
        architecture = 'aarch64'
    elif system == 'Darwin' and architecture == 'aarch64':
        architecture = 'arm64'
    bits = struct.calcsize('P') * 8
    target = next((key for key, value in TARGETS.items()
                   if value == (system, architecture) and bits == 64), None)
    return dict(system=system, host_machine=machine, architecture=architecture,
                pointer_bits=bits, abi=f'cdecl:{bits}', python_platform=python_platform,
                target=target)


def backend_architecture():
    """Use process architecture while retaining existing Windows checkpoint labels."""
    runtime = runtime_identity()
    if runtime['system'] == 'Windows':
        return {'x86_64': 'AMD64', 'arm64': 'ARM64'}.get(runtime['architecture'], runtime['architecture'])
    return runtime['architecture']


def platform_error():
    runtime = runtime_identity()
    if runtime['target'] is None:
        return ('Packaged native libraries require supported 64-bit Python: Windows x86-64, '
                'Linux x86-64/aarch64, or macOS arm64; current runtime is '
                f"{runtime['system']} {runtime['host_machine']} "
                f"({runtime['python_platform']}, {runtime['pointer_bits']}-bit Python, "
                f"process architecture {runtime['architecture']})")
    return None


def _package_roots():
    roots = []
    if getattr(sys, 'frozen', False):
        base = Path(getattr(sys, '_MEIPASS', Path(sys.executable).parent)).resolve()
        roots.extend((base / 'easysewer', base))
    roots.append(Path(__file__).resolve().parent)
    return tuple(dict.fromkeys(roots))


def _relative(value):
    if (type(value) is not str or not value or '\\' in value or '\0' in value
            or ':' in value or value.startswith('/')
            or any(part in ('', '.', '..') for part in value.split('/'))):
        raise ValueError('Artifact paths must be confined package-relative POSIX paths')
    if PurePosixPath(value).parts[0] != 'libs':
        raise ValueError('Native artifacts must be inside the package libs directory')
    return value


def _file_record(record):
    if type(record) is not dict:
        raise ValueError('Expected an artifact record')
    _relative(record.get('path'))
    if type(record.get('sha256')) is not str or not _SHA.fullmatch(record['sha256']):
        raise ValueError('Expected a lowercase SHA-256 digest')
    if type(record.get('size')) is not int or record['size'] <= 0:
        raise ValueError('Expected a positive artifact size')


def _unique(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError('Duplicate manifest field: ' + name)
        result[name] = value
    return result


def _validate(data):
    if (type(data) is not dict or set(data) != {'schema', 'targets'}
            or data.get('schema') != SCHEMA or type(data.get('targets')) is not dict
            or not data['targets']):
        raise ValueError('Unsupported or empty native runtime manifest')
    for key, target in data['targets'].items():
        if key not in TARGETS or type(target) is not dict:
            raise ValueError('Unknown native runtime target: ' + key)
        if set(target) != {'system', 'architecture', 'pointer_bits', 'abi', 'minimum_os', 'libc', 'libraries'}:
            raise ValueError('Invalid native runtime target fields')
        if ((target['system'], target['architecture']) != TARGETS[key]
                or type(target['pointer_bits']) is not int or target['pointer_bits'] != 64
                or target['abi'] != 'cdecl:64'):
            raise ValueError('Target key and native ABI disagree')
        minimum = target['minimum_os']
        if minimum is not None and (type(minimum) is not str or not _VERSION.fullmatch(minimum)):
            raise ValueError('Invalid deployment target')
        libc = target['libc']
        if target['system'] == 'Linux':
            if (type(libc) is not dict or set(libc) != {'name', 'minimum_version'}
                    or libc['name'] != 'glibc' or type(libc['minimum_version']) is not str
                    or not _VERSION.fullmatch(libc['minimum_version'])):
                raise ValueError('Linux targets require an explicit glibc baseline')
        elif libc is not None:
            raise ValueError('Non-Linux targets cannot declare a glibc baseline')
        libraries = target['libraries']
        if (type(libraries) is not dict or not {'swmm5', 'swmm-output'} <= set(libraries)
                or not set(libraries) <= set(POLICIES)):
            raise ValueError('A native target requires the standard solver and output reader')
        paths = set()
        for name, record in libraries.items():
            _file_record(record)
            if set(record) != {'path', 'sha256', 'size', 'numerical_policy', 'build_record',
                               'build_record_sha256', 'required', 'dependencies', 'external_dependencies'}:
                raise ValueError('Invalid native library fields')
            if record['path'] in paths:
                raise ValueError('Native library paths must be distinct')
            paths.add(record['path'])
            if record['numerical_policy'] != POLICIES[name]:
                raise ValueError('Unrecognized numerical policy for ' + name)
            if type(record['required']) is not bool or record['required'] != (name != 'flexible_ponding'):
                raise ValueError('Invalid required/experimental library designation')
            _relative(record['build_record'])
            if (type(record['build_record_sha256']) is not str
                    or not _SHA.fullmatch(record['build_record_sha256'])):
                raise ValueError('Invalid build-record digest')
            if type(record['dependencies']) is not list:
                raise ValueError('Expected a bundled dependency list')
            dependencies = set()
            for dependency in record['dependencies']:
                _file_record(dependency)
                if set(dependency) != {'path', 'sha256', 'size'} or dependency['path'] in dependencies:
                    raise ValueError('Invalid or duplicate bundled dependency')
                dependencies.add(dependency['path'])
            if (type(record['external_dependencies']) is not list
                    or any(type(value) is not str or not value or '/' in value or '\\' in value
                           for value in record['external_dependencies'])):
                raise ValueError('Expected external dependency names')
    return data


def read_manifest():
    for root in _package_roots():
        path = root / 'libs' / 'runtime-manifest.json'
        if not path.exists() and not path.is_symlink():
            continue
        try:
            if not path.resolve().is_relative_to(root.resolve()):
                raise ValueError('Native manifest escapes the package')
            with path.open('rb') as stream:
                raw = stream.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise ValueError('Native manifest exceeds its size limit')
            data = _validate(json.loads(raw, object_pairs_hook=_unique))
            return root, data
        except (OSError, ValueError, TypeError) as error:
            raise NativeRegistryError('manifest_invalid', 'Invalid native runtime manifest: ' + str(error)) from error
    raise NativeRegistryError('native_absent',
        'Native runtime manifest is absent. The pure package does not include native libraries; '
        'install the native wheel matching this Python platform, or select a compatible solver explicitly')


def _confined(root, relative):
    path = root / relative
    if not path.resolve().is_relative_to(root.resolve()):
        raise NativeRegistryError('artifact_invalid', 'Native artifact escapes the package: ' + relative)
    return path


def _digest(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


@dataclass(frozen=True)
class NativeArtifact:
    name: str
    root: Path
    target_key: str
    target: dict
    record: dict

    @property
    def path(self):
        return _confined(self.root, self.record['path'])

    def check_baseline(self):
        """Reject a known incompatible libc/deployment baseline without loading."""
        if self.target['system'] == 'Linux':
            family, version = platform.libc_ver()
            expected = self.target['libc']
            if family and family != expected['name']:
                raise NativeRegistryError('runtime_incompatible',
                    f"This native artifact requires glibc {expected['minimum_version']} or newer; "
                    f'current libc is {family} {version}')
            minimum = expected['minimum_version']
        elif self.target['system'] == 'Darwin':
            version = platform.mac_ver()[0]
            minimum = self.target['minimum_os']
        else:
            return
        if minimum and version and _VERSION.fullmatch(version):
            parts = lambda value: tuple(int(piece) for piece in value.split('.'))
            actual, required = parts(version), parts(minimum)
            length = max(len(actual), len(required))
            if actual + (0,) * (length-len(actual)) < required + (0,) * (length-len(required)):
                label = 'glibc' if self.target['system'] == 'Linux' else 'macOS'
                raise NativeRegistryError('runtime_incompatible',
                    f'This native artifact requires {label} {minimum} or newer; current version is {version}')

    def verify(self):
        """Check final bytes, bundled dependencies and their provenance record."""
        self.check_baseline()
        try:
            for record in (self.record, *self.record['dependencies']):
                path = _confined(self.root, record['path'])
                if not path.is_file():
                    raise ValueError('Native artifact is absent or not a regular file: ' + record['path'])
                if path.stat().st_size != record['size'] or _digest(path) != record['sha256']:
                    raise ValueError('Native artifact SHA-256/size does not match the selected artifact: ' + record['path'])
            evidence = _confined(self.root, self.record['build_record'])
            if not evidence.is_file() or _digest(evidence) != self.record['build_record_sha256']:
                raise ValueError('Native build-record SHA-256 does not match the selected artifact')
            if evidence.stat().st_size > 1024 * 1024:
                raise ValueError('Native build record exceeds its size limit')
            build = json.loads(evidence.read_bytes())
            if build.get('sha256') != self.record['sha256'] or build.get('size') != self.record['size']:
                raise ValueError('Native build record does not describe the final artifact bytes')
        except (OSError, ValueError, TypeError, AttributeError) as error:
            if isinstance(error, NativeRegistryError):
                raise
            raise NativeRegistryError('artifact_invalid', str(error)) from error
        return self


def select_library(name, *, verify=False):
    if name not in POLICIES:
        raise NativeRegistryError('library_unknown', 'Unknown packaged native library: ' + str(name))
    runtime = runtime_identity()
    reason = platform_error()
    if reason:
        raise NativeRegistryError('unsupported_abi', reason)
    root, manifest = read_manifest()
    key = runtime['target']
    if key not in manifest['targets']:
        raise NativeRegistryError('target_absent',
            f"No packaged native libraries for {runtime['system']} {runtime['architecture']} "
            f"({runtime['pointer_bits']}-bit Python). Install a native wheel for {key}; "
            'libraries for another architecture are never substituted')
    target = manifest['targets'][key]
    if name not in target['libraries']:
        label = 'Experimental FlexiblePonding replacement-backend example' if name == 'flexible_ponding' else name
        raise NativeRegistryError('library_absent', f'{label} is not included for {key}; no fallback is performed')
    artifact = NativeArtifact(name, root, key, target, target['libraries'][name])
    if not os.path.isfile(artifact.path):
        raise NativeRegistryError('artifact_missing', 'Packaged native artifact is absent or not a regular file: ' + str(artifact.path))
    return artifact.verify() if verify else artifact


def policy_for_digest(name, digest):
    """Recognize installed artifacts only; never inspect user-adjacent JSON."""
    try:
        artifact = select_library(name)
    except NativeRegistryError:
        return None
    return artifact.record['numerical_policy'] if artifact.record['sha256'] == digest else None
