"""
OutputAPI module for easysewer package.

This module provides interfaces to the SWMM output library for reading simulation results.
"""
import ctypes
from .utils import require_native_capability


class SWMMOutputAPI:
    def __init__(self):
        lib_path = require_native_capability("SWMM output reader", "swmm-output")

        # Load the shared library
        self.lib = ctypes.CDLL(lib_path)
        
        # Set up function prototypes
        self._set_prototypes()

    def _set_prototypes(self):
        # Define the function prototypes
        self.lib.SMO_init.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
        self.lib.SMO_init.restype = ctypes.c_int

        self.lib.SMO_close.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
        self.lib.SMO_close.restype = ctypes.c_int

        self.lib.SMO_open.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        self.lib.SMO_open.restype = ctypes.c_int

        self.lib.SMO_getVersion.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)]
        self.lib.SMO_getVersion.restype = ctypes.c_int

        self.lib.SMO_getFlowUnits.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)]
        self.lib.SMO_getFlowUnits.restype = ctypes.c_int

        self.lib.SMO_getStartDate.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_double)]
        self.lib.SMO_getStartDate.restype = ctypes.c_int

        self.lib.SMO_getTimes.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
        self.lib.SMO_getTimes.restype = ctypes.c_int

        self.lib.SMO_getSubcatchSeries.argtypes = [
            ctypes.c_void_p,  # Handle
            ctypes.c_int,  # Subcatch index
            ctypes.c_int,  # Subcatch attribute
            ctypes.c_int,  # Start period
            ctypes.c_int,  # End period
            ctypes.POINTER(ctypes.POINTER(ctypes.c_float)),  # Output value array
            ctypes.POINTER(ctypes.c_int)  # Length
        ]
        self.lib.SMO_getSubcatchSeries.restype = ctypes.c_int

        self.lib.SMO_getNodeSeries.argtypes = [
            ctypes.c_void_p,  # Handle
            ctypes.c_int,  # Node index
            ctypes.c_int,  # Node attribute
            ctypes.c_int,  # Start period
            ctypes.c_int,  # End period
            ctypes.POINTER(ctypes.POINTER(ctypes.c_float)),  # Output value array
            ctypes.POINTER(ctypes.c_int)  # Length
        ]
        self.lib.SMO_getNodeSeries.restype = ctypes.c_int

        self.lib.SMO_getLinkSeries.argtypes = [
            ctypes.c_void_p,  # Handle
            ctypes.c_int,  # Link index
            ctypes.c_int,  # Link attribute
            ctypes.c_int,  # Start period
            ctypes.c_int,  # End period
            ctypes.POINTER(ctypes.POINTER(ctypes.c_float)),  # Output value array
            ctypes.POINTER(ctypes.c_int)  # Length
        ]
        self.lib.SMO_getLinkSeries.restype = ctypes.c_int

        self.lib.SMO_getSystemSeries.argtypes = [
            ctypes.c_void_p,  # Handle
            ctypes.c_int,  # System attribute
            ctypes.c_int,  # Start period
            ctypes.c_int,  # End period
            ctypes.POINTER(ctypes.POINTER(ctypes.c_float)),  # Output value array
            ctypes.POINTER(ctypes.c_int)  # Length
        ]
        self.lib.SMO_getSystemSeries.restype = ctypes.c_int

        self.lib.SMO_free.argtypes = [ctypes.POINTER(ctypes.POINTER(ctypes.c_float))]
        self.lib.SMO_free.restype = None

        # Initialize the API
        self.handle = ctypes.c_void_p()
        if self.lib.SMO_init(ctypes.byref(self.handle)) != 0:
            raise Exception("Failed to initialize SWMM Output API")

    def __enter__(self):
        if self.lib.SMO_init(ctypes.byref(self.handle)) != 0:
            raise Exception("Failed to initialize SWMM Output API")
        self.initialized = True
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if self.initialized and self.handle:
            if self.lib.SMO_close(ctypes.byref(self.handle)) != 0:
                raise Exception("Failed to close SWMM Output API")
            self.initialized = False

    def open(self, file_path):
        if self.lib.SMO_open(self.handle, file_path.encode('utf-8')) != 0:
            raise Exception("Failed to open SWMM output file")

    def get_flow_units(self):
        unit_flag = ctypes.c_int()
        if self.lib.SMO_getFlowUnits(self.handle, ctypes.byref(unit_flag)) != 0:
            raise Exception("Failed to get flow units")
        return unit_flag.value

    def get_start_date(self):
        start_date = ctypes.c_double()
        if self.lib.SMO_getStartDate(self.handle, ctypes.byref(start_date)) != 0:
            raise Exception("Failed to get start date")
        return start_date.value

    def get_times(self, code):
        time = ctypes.c_int()
        if self.lib.SMO_getTimes(self.handle, code, ctypes.byref(time)) != 0:
            raise Exception(f"Failed to get times for code {code}")
        return time.value

    def get_subcatch_series(self, subcatch_index, attr, start_period, end_period):
        value_array = ctypes.POINTER(ctypes.c_float)()
        length = ctypes.c_int()
        if self.lib.SMO_getSubcatchSeries(
                self.handle, subcatch_index, attr, start_period, end_period,
                ctypes.byref(value_array), ctypes.byref(length)
        ) != 0:
            raise Exception("Failed to get subcatch series")
        values = [value_array[i] for i in range(length.value)]
        self.lib.SMO_free(ctypes.byref(value_array))
        return values

    def get_node_series(self, node_index, attr, start_period, end_period):
        value_array = ctypes.POINTER(ctypes.c_float)()
        length = ctypes.c_int()
        if self.lib.SMO_getNodeSeries(
                self.handle, node_index, attr, start_period, end_period,
                ctypes.byref(value_array), ctypes.byref(length)
        ) != 0:
            raise Exception("Failed to get node series")
        values = [value_array[i] for i in range(length.value)]
        self.lib.SMO_free(ctypes.byref(value_array))
        return values

    def get_link_series(self, link_index, attr, start_period, end_period):
        value_array = ctypes.POINTER(ctypes.c_float)()
        length = ctypes.c_int()
        if self.lib.SMO_getLinkSeries(
                self.handle, link_index, attr, start_period, end_period,
                ctypes.byref(value_array), ctypes.byref(length)
        ) != 0:
            raise Exception("Failed to get link series")
        values = [value_array[i] for i in range(length.value)]
        self.lib.SMO_free(ctypes.byref(value_array))
        return values

    def get_system_series(self, attr, start_period, end_period):
        value_array = ctypes.POINTER(ctypes.c_float)()
        length = ctypes.c_int()
        if self.lib.SMO_getSystemSeries(
                self.handle, attr, start_period, end_period,
                ctypes.byref(value_array), ctypes.byref(length)
        ) != 0:
            raise Exception("Failed to get system series")
        values = [value_array[i] for i in range(length.value)]
        self.lib.SMO_free(ctypes.byref(value_array))
        return values
