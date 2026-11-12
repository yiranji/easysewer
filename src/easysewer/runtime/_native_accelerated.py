"""Acceleration ABI qualification inside an isolated native worker."""

import ctypes as c

from ._native_solver import NativeSolver


class NativeAcceleratedSolver(NativeSolver):
    backend_key = 'swmm:accelerated'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if hasattr(self.lib, 'swmm_execRouting'):
            raise ValueError('SWMM Accelerated Solver cannot use a custom routing library')
        marker = getattr(self.lib, 'swmm_getEasySewerAccelerationVersion', None)
        if marker is None:
            raise ValueError('SWMM Accelerated Solver requires its acceleration marker')
        marker.argtypes = []
        marker.restype = c.c_int
        if marker() != 1:
            raise ValueError('SWMM Accelerated Solver requires acceleration ABI 1')
        self.metadata['abi'] += ':accelerated:1'
