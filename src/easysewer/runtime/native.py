"""Standard backend discovery, without importing ctypes or spawning on import."""

from dataclasses import dataclass, replace
from pathlib import Path
import platform
import re
import struct
import sys

from .backend import BackendInfo, NativeFailure, SessionError
from ..utils import _packaged_native_platform_error, probe_library_path


_LEGACY_HASHES = {
    'Windows': 'b2f181dd8f1b6cca231922ea9d364477ed9dbc369cb78d15ed5292232c861d71',
    'Linux': '2eecac082c3b8405f49c21d4a40256345f500d0eee3745a04699ccccfbba64dc',
}
_CHECKPOINT_HASHES = {
    'Windows': '4fb2d52562f348bdf344573ed3b05baef42840a10e255e7945b8ac5383891d8e',
    'Linux': '9ddcf2ef6303cbc1307fbaa6d2bf6030631a73ce3bfc65750053972a40f45421',
}
_PATH_HASHES = {
    'Windows': 'f8463c1c0df07e4332ddc94b6a5384ada579cf24ee89fce1efa71f843afee333',
    'Linux': 'f2ba9f1ef0c9e7f1374199ae18b977068f6060c239ecfbdc6607ccc528ced0af',
}
_HORTON_HASHES = {'Windows': '87e8caa0f9a1c949ee3db500a59b78360f9eb8b1385a6ee385bec00c12cb64a0', 'Linux': '7d127009aa513973caa355248fea6cd003cb45d24ad526e759e935f1f7dd3ce3'}
_HORTON_OUTFALL_HASHES = {'Windows': '8a164d4611870561e1e205f1a04d155c964d6ea8015212c36f9677f472e2cb2c', 'Linux': 'f59cf3b394616e5964dd15cafe3bfeda8233b29ebe7d39bca7ca0531f7bd0e17'}
_PACKAGED_HASHES = _HORTON_OUTFALL_HASHES
STANDARD_BUILD_POLICY = 'easysewer:standard:5.2.4:16'
_IO_CAPABILITIES = tuple('easysewer:'+name+':1' for name in (
    'runoff-physics', 'runoff-rain-clock', 'rdii-io', 'routing-io',
    'solver-output-io', 'climate-io', 'timeseries-io', 'report-io', 'lid-report-io'))
_CORRECTION_PREFIXES = tuple(value.rsplit(':', 1)[0]+':' for value in _IO_CAPABILITIES) + (
    'easysewer:standard-fixes:', 'easysewer:native-io-fixes:', 'easysewer:checkpoint:',
    'easysewer:path-io:', 'easysewer:horton-capacity:', 'easysewer:horton-state:', 'easysewer:outfall-gate:')
_FEATURES = ('swmm:standard', 'swmm:engine-identities', 'swmm:continuity',
             'runtime:process-isolation', 'runtime:cancellation', 'runtime:call-timeout')


@dataclass(frozen=True, kw_only=True)
class StandardBackend:
    """SWMM 5.2.4 C ABI, with one isolated process for each explicit session.

    A user library is trusted executable code, just like an installed Python
    backend. Supplying its SHA-256 pins its bytes; this is not a sandbox or a
    claim that the binary was built from the official source tag.
    """
    library: str | None = None
    expected_sha256: str | None = None
    key = 'swmm:standard'
    worker_kind = 'standard'
    supported_extensions = ()

    def __post_init__(self):
        if self.library is not None:
            if not isinstance(self.library, str) or not Path(self.library).is_absolute():
                raise ValueError('An explicit native library requires an absolute path')
        if self.expected_sha256 is not None and not re.fullmatch('[0-9a-fA-F]{64}', self.expected_sha256):
            raise ValueError('Expected a SHA-256 hex digest')

    def _selection(self):
        path = self.library or probe_library_path('swmm5')
        expected = self.expected_sha256 or (None if self.library else _PACKAGED_HASHES.get(platform.system()))
        return path, expected.lower() if expected else None

    def _unavailable(self, reason, *, metadata=None):
        metadata = metadata or {}
        path, _ = self._selection()
        return BackendInfo(key=self.key, available=False, reason=reason,
            library=metadata.get('library', path), sha256=metadata.get('sha256'),
            engine_version=metadata.get('engine_version'), platform=metadata.get('platform', platform.system()),
            architecture=metadata.get('architecture', platform.machine()),
            abi=metadata.get('abi', f'cdecl:{struct.calcsize("P")*8}'), profiles=(), capabilities=(),
            isolation='process', origin='user-library' if self.library else 'packaged-bytes')

    def _loaded(self, metadata):
        path, expected = self._selection()
        reason = None
        if Path(metadata['library']).resolve() != Path(path).resolve():
            reason = 'Worker loaded a different library path'
        elif expected and metadata['sha256'] != expected:
            reason = 'Native library SHA-256 does not match the selected artifact'
        elif metadata['engine_version'] != 52004:
            reason = 'This backend profile requires SWMM 5.2.4 (52004)'
        if reason:
            return self._unavailable(reason, metadata=metadata)
        return self.execution_info(BackendInfo(key=self.key, available=True, reason=None, **metadata,
            profiles=('epa-swmm:5.2.4',), capabilities=_FEATURES, isolation='process',
            origin='user-library' if self.library else 'packaged-bytes'))

    def execution_info(self, info):
        # A version number (or user-provided expected digest) does not prove
        # which source corrections and compiler policy produced a library.
        capabilities = tuple(c for c in info.capabilities if not c.startswith(_CORRECTION_PREFIXES))
        revision = (16 if info.sha256 in _HORTON_OUTFALL_HASHES.values() else
                    15 if info.sha256 in _HORTON_HASHES.values() else
                    14 if info.sha256 in _PATH_HASHES.values() else
                    13 if info.sha256 in _CHECKPOINT_HASHES.values() else
                    12 if info.sha256 in _LEGACY_HASHES.values() else None)
        if revision is not None:
            return replace(info, numerical_policy=f'easysewer:standard:5.2.4:{revision}',
                capabilities=capabilities+(f'easysewer:standard-fixes:{revision}',)+_IO_CAPABILITIES+
                    (('easysewer:checkpoint:2',) if revision>=13 else ())+
                    (('easysewer:path-io:1',) if revision>=14 else ())+
                    (('easysewer:horton-capacity:1',) if revision>=15 else ())+
                    (('easysewer:horton-state:1','easysewer:outfall-gate:1') if revision>=16 else ()),
                output_semantics=(('swmm:nws-rainfall-arithmetic', 'reference'),
                                  ('swmm:runoff-replay', 'easysewer:runoff-physics:1'),
                                  ('swmm:runoff-rain-clock', 'easysewer:runoff-rain-clock:1'))+
                    ((('swmm:modified-horton','easysewer:horton-state:1'),
                      ('swmm:outfall-gate','easysewer:outfall-gate:1')) if revision>=16 else
                     (('swmm:modified-horton','easysewer:horton-capacity:1'),) if revision>=15 else ()))
        return replace(info, numerical_policy='user-library:unverified', output_semantics=(),
            capabilities=capabilities)

    def _check_runtime(self):
        if self.library is None:
            reason = _packaged_native_platform_error()
            if reason:
                raise SessionError(NativeFailure(stage='load', code=None, message=reason))
        path, _ = self._selection()
        if not path or not Path(path).is_file():
            reason = (f'Selected native library is absent or not a regular file: {path}' if self.library else
                      'Native SWMM library is absent from this package. The pure package does not '
                      'include a solver; install the native package on a supported platform or '
                      'select a compatible library explicitly')
            raise SessionError(NativeFailure(stage='load', code=None, message=reason))
        if sys.platform in ('emscripten', 'wasi') or getattr(sys, 'frozen', False):
            raise SessionError(NativeFailure(stage='launch', code=None,
                message='This runtime cannot launch the Python SWMM worker'))
        return path

    def validate_profile(self, profile):
        from ..schema import EPA_SWMM_5_2_4
        if profile != EPA_SWMM_5_2_4:
            raise ValueError('The standard backend requires the complete fixed EPA SWMM 5.2.4 profile')

    def probe(self, *, call_timeout=30, cancel_event=None, poll_interval=.05):
        """Load the real library and call its version ABI in a disposable worker."""
        try:
            path = self._check_runtime()
            with self.session(working_directory=Path(path).parent, call_timeout=call_timeout,
                              cancel_event=cancel_event, poll_interval=poll_interval) as session:
                return session.info
        except SessionError as error:
            return self._unavailable(str(error), metadata=getattr(error, 'backend_metadata', None))

    def session(self, *, working_directory, call_timeout=30, cancel_event=None, poll_interval=.05,
                _execution_guard=None):
        path = self._check_runtime()
        try:
            from ._process_session import ProcessSession
        except ModuleNotFoundError as error:
            if error.name != 'easysewer.runtime._process_session':
                raise
            raise SessionError(NativeFailure(stage='launch', code=None,
                message='The process backend is not included in this package profile')) from error
        try:
            return ProcessSession(self, path, working_directory=working_directory,
                                  call_timeout=call_timeout, cancel_event=cancel_event, poll_interval=poll_interval,
                                  _execution_guard=_execution_guard)
        except SessionError as error:
            # Keep the original loader evidence. These requirements belong to
            # bundled Linux bytes, not arbitrary compatible user libraries.
            hint = None
            if self.library is None and platform.system() == 'Linux' and type(error) is SessionError and error.failure.stage == 'load':
                message = error.failure.message
                if 'libgomp.so.1' in message and 'cannot open shared object file' in message:
                    hint = ('Packaged Linux solvers require the OpenMP runtime libgomp.so.1 '
                            '(libgomp1 on Debian/Ubuntu, libgomp on Fedora/RHEL)')
                elif re.search(r'GLIBC_\d+\.\d+', message) and 'not found' in message:
                    hint = ('Packaged Linux solvers require glibc 2.33 or newer; use a compatible '
                            'runtime or rebuild a solver for the target system')
            if hint is None:
                raise
            failure = replace(error.failure, message=f'{error.failure.message}. {hint}')
            enriched = SessionError(failure, cleanup=error.cleanup, stderr=error.stderr, returncode=error.returncode)
            if hasattr(error, 'backend_metadata'):
                enriched.backend_metadata = error.backend_metadata
            raise enriched from error


# Capture the participating implementation, not a subsequently overridden factory.
_NATIVE_SESSION_FACTORY = StandardBackend.session
