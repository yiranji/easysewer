"""Internal SMO adapter with explicit ownership; 2.0 code can use OutputReader."""

import ctypes
import os

from ..utils import NativeCapabilityError, require_native_capability


class SWMMOutputError(RuntimeError):
    """An SMO return code, retained alongside the operation that failed."""

    def __init__(self, operation, code):
        self.operation = operation
        self.code = code
        super().__init__(f'SWMM output {operation} failed (SMO code {code})')


class SWMMOutputAPI:
    """Own one native handle. Use a context manager or call :meth:`close`.

    Series ranges are end-exclusive and nonempty, matching the legacy C API.
    Instances and their native handles must not be used concurrently. The
    native library must implement easysewer output I/O revision 1.
    """

    def __init__(self):
        self.handle = ctypes.c_void_p()
        lib_path = require_native_capability('SWMM output reader', 'swmm-output')
        self.lib = ctypes.CDLL(lib_path)
        marker = getattr(self.lib, 'swmm_getEasySewerOutputIO', None)
        if marker is None:
            raise NativeCapabilityError('The SWMM output library requires easysewer output I/O revision 1')
        marker.argtypes = []
        marker.restype = ctypes.c_int
        if marker() != 1:
            raise NativeCapabilityError('Unsupported easysewer output I/O revision')
        self._set_prototypes()
        self._check('initialize', self.lib.SMO_init(ctypes.byref(self.handle)))

    def _set_prototypes(self):
        pointer = ctypes.POINTER
        signatures = {
            'init': [pointer(ctypes.c_void_p)],
            'close': [pointer(ctypes.c_void_p)],
            'open': [ctypes.c_void_p, ctypes.c_char_p],
            'getVersion': [ctypes.c_void_p, pointer(ctypes.c_int)],
            'getFlowUnits': [ctypes.c_void_p, pointer(ctypes.c_int)],
            'getStartDate': [ctypes.c_void_p, pointer(ctypes.c_double)],
            'getTimes': [ctypes.c_void_p, ctypes.c_int, pointer(ctypes.c_int)],
        }
        for name in ('Subcatch', 'Node', 'Link', 'System'):
            count = 3 if name == 'System' else 4
            signatures['get' + name + 'Series'] = [ctypes.c_void_p, *([ctypes.c_int] * count),
                                                  pointer(ctypes.c_void_p), pointer(ctypes.c_int)]
        for name, args in signatures.items():
            fn = getattr(self.lib, 'SMO_' + name)
            fn.argtypes = args
            fn.restype = ctypes.c_int
        self.lib.SMO_free.argtypes = [pointer(ctypes.c_void_p)]
        self.lib.SMO_free.restype = None

    @property
    def initialized(self):
        return bool(self.handle.value)

    @property
    def closed(self):
        return not self.initialized

    @staticmethod
    def _check(operation, code):
        if code:
            raise SWMMOutputError(operation, code)

    def _require_handle(self):
        if self.closed:
            raise ValueError('SWMM output reader is closed')

    def __enter__(self):
        self._require_handle()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            self.close()
        except Exception:
            if exc_type is None:
                raise
        return False

    def close(self):
        if self.initialized:
            self._check('close', self.lib.SMO_close(ctypes.byref(self.handle)))

    def __del__(self):
        # Best effort for legacy callers. Explicit close reports I/O failures.
        try:
            self.close()
        except Exception:
            pass

    def open(self, file_path):
        self._require_handle()
        path = os.fsencode(file_path)
        if b'\0' in path:
            raise ValueError('OUT path contains a NUL byte')
        self._check('open', self.lib.SMO_open(self.handle, path))

    def _scalar(self, name, kind, *args):
        self._require_handle()
        value = kind()
        self._check(name, getattr(self.lib, 'SMO_' + name)(self.handle, *args, ctypes.byref(value)))
        return value.value

    def get_flow_units(self):
        return self._scalar('getFlowUnits', ctypes.c_int)

    def get_start_date(self):
        return self._scalar('getStartDate', ctypes.c_double)

    def get_times(self, code):
        return self._scalar('getTimes', ctypes.c_int, self._integer(code))

    @staticmethod
    def _integer(value):
        if type(value) is not int or not -(2**31) <= value < 2**31:
            raise ValueError('SMO indexes and codes must be 32-bit integers')
        return value

    def _series(self, name, *args):
        self._require_handle()
        args = tuple(self._integer(value) for value in args)
        value = ctypes.c_void_p()
        length = ctypes.c_int()
        try:
            self._check(name, getattr(self.lib, 'SMO_' + name)(self.handle, *args,
                        ctypes.byref(value), ctypes.byref(length)))
            array = ctypes.cast(value, ctypes.POINTER(ctypes.c_float))
            return [array[i] for i in range(length.value)]
        finally:
            self.lib.SMO_free(ctypes.byref(value))

    def get_subcatch_series(self, subcatch_index, attr, start_period, end_period):
        return self._series('getSubcatchSeries', subcatch_index, attr, start_period, end_period)

    def get_node_series(self, node_index, attr, start_period, end_period):
        return self._series('getNodeSeries', node_index, attr, start_period, end_period)

    def get_link_series(self, link_index, attr, start_period, end_period):
        return self._series('getLinkSeries', link_index, attr, start_period, end_period)

    def get_system_series(self, attr, start_period, end_period):
        return self._series('getSystemSeries', attr, start_period, end_period)
