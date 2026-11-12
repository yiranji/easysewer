"""Explicit SWMM Accelerated Solver discovery; importing never loads native code."""

from dataclasses import dataclass, replace

from .native import StandardBackend, _standard_execution_info
from .._native_registry import NativeRegistryError, select_library

ACCELERATED_BUILD_POLICY = 'easysewer:accelerated:5.2.4:1'
_ACCELERATION_CAPABILITY = 'easysewer:hydraulic-acceleration:1'


@dataclass(frozen=True, kw_only=True)
class AcceleratedBackend(StandardBackend):
    """An optional solver with ordered hydraulic aggregation over standard 16.

    The acceleration marker establishes the native interface, not provenance or
    performance. Only bytes recognized by the verified installed registry gain
    correction and checkpoint capabilities. Explicit unrecognized libraries
    remain unverified, even when their marker and caller-supplied digest match.
    """
    key = 'swmm:accelerated'
    display_name = 'SWMM Accelerated Solver'
    worker_kind = 'accelerated'
    library_name = 'swmm-accelerated'

    def _loaded(self, metadata):
        if not metadata.get('abi', '').endswith(':accelerated:1'):
            return self._unavailable('SWMM Accelerated Solver requires acceleration ABI 1', metadata=metadata)
        return super()._loaded(metadata)

    def execution_info(self, info):
        capabilities = tuple(value for value in info.capabilities
            if value not in ('swmm:standard', self.key)
            and not value.startswith('easysewer:hydraulic-acceleration:')) + (self.key,)
        info = replace(info, capabilities=capabilities)
        recognized = False
        try:
            artifact = select_library(self.library_name, verify=True)
            recognized = (artifact.record['sha256'] == info.sha256
                          and artifact.record['numerical_policy'] == ACCELERATED_BUILD_POLICY)
        except NativeRegistryError:
            pass
        info = _standard_execution_info(info, 16 if recognized else None)
        if recognized:
            return replace(info, numerical_policy=ACCELERATED_BUILD_POLICY,
                           capabilities=info.capabilities+(_ACCELERATION_CAPABILITY,))
        return info
