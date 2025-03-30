"""
pass
"""
import platform
import os
from ctypes import CDLL, c_char_p, c_int, c_double, c_float, byref, POINTER, create_string_buffer


class SWMMSolverAPI:
    def __init__(self):
        #
        system = platform.system()
        if system == 'Windows':
            lib_path = os.path.join(os.path.dirname(__file__), 'libs', 'win', 'swmm5.dll')
        elif system == 'Linux':
            lib_path = os.path.join(os.path.dirname(__file__), 'libs', 'linux', 'libswmm5.so')
        else:
            raise OSError('Unsupported operating system')

        self.swmm = CDLL(lib_path)
        self._set_prototypes()

    def _set_prototypes(self):
        self.swmm.swmm_run.argtypes = [c_char_p, c_char_p, c_char_p]
        self.swmm.swmm_run.restype = c_int

        self.swmm.swmm_open.argtypes = [c_char_p, c_char_p, c_char_p]
        self.swmm.swmm_open.restype = c_int

        self.swmm.swmm_start.argtypes = [c_int]
        self.swmm.swmm_start.restype = c_int

        self.swmm.swmm_step.argtypes = [POINTER(c_double)]
        self.swmm.swmm_step.restype = c_int

        self.swmm.swmm_end.argtypes = []
        self.swmm.swmm_end.restype = c_int

        self.swmm.swmm_report.argtypes = []
        self.swmm.swmm_report.restype = c_int

        self.swmm.swmm_close.argtypes = []
        self.swmm.swmm_close.restype = c_int

        self.swmm.swmm_getMassBalErr.argtypes = [POINTER(c_float), POINTER(c_float), POINTER(c_float)]
        self.swmm.swmm_getMassBalErr.restype = c_int

        self.swmm.swmm_getVersion.argtypes = []
        self.swmm.swmm_getVersion.restype = c_int

        self.swmm.swmm_getError.argtypes = [c_char_p, c_int]
        self.swmm.swmm_getError.restype = c_int

        self.swmm.swmm_getWarnings.argtypes = []
        self.swmm.swmm_getWarnings.restype = c_int

        self.swmm.swmm_getCount.argtypes = [c_int]
        self.swmm.swmm_getCount.restype = c_int

        self.swmm.swmm_getName.argtypes = [c_int, c_int, c_char_p, c_int]
        self.swmm.swmm_getName.restype = None

        self.swmm.swmm_getIndex.argtypes = [c_int, c_char_p]
        self.swmm.swmm_getIndex.restype = c_int

        self.swmm.swmm_getValue.argtypes = [c_int, c_int]
        self.swmm.swmm_getValue.restype = c_double

        self.swmm.swmm_setValue.argtypes = [c_int, c_int, c_double]
        self.swmm.swmm_setValue.restype = None

        self.swmm.swmm_getSavedValue.argtypes = [c_int, c_int, c_int]
        self.swmm.swmm_getSavedValue.restype = c_double

        self.swmm.swmm_writeLine.argtypes = [c_char_p]
        self.swmm.swmm_writeLine.restype = None

        self.swmm.swmm_decodeDate.argtypes = [c_double, POINTER(c_int), POINTER(c_int), POINTER(c_int), POINTER(c_int), POINTER(c_int), POINTER(c_int), POINTER(c_int)]
        self.swmm.swmm_decodeDate.restype = None

    def run(self, input_file, report_file, output_file):
        return self.swmm.swmm_run(input_file.encode('utf-8'), report_file.encode('utf-8'), output_file.encode('utf-8'))

    def open(self, input_file, report_file, output_file):
        return self.swmm.swmm_open(input_file.encode('utf-8'), report_file.encode('utf-8'), output_file.encode('utf-8'))

    def start(self, save_flag):
        return self.swmm.swmm_start(save_flag)

    def step(self):
        elapsed_time = c_double()
        result = self.swmm.swmm_step(byref(elapsed_time))
        return result, elapsed_time.value

    def end(self):
        return self.swmm.swmm_end()

    def report(self):
        return self.swmm.swmm_report()

    def close(self):
        return self.swmm.swmm_close()

    def get_mass_bal_err(self):
        runoff_err = c_float()
        flow_err = c_float()
        qual_err = c_float()
        self.swmm.swmm_getMassBalErr(byref(runoff_err), byref(flow_err), byref(qual_err))
        return runoff_err.value, flow_err.value, qual_err.value

    def get_version(self):
        return self.swmm.swmm_getVersion()

    def get_error(self, msg_len=256):
        err_msg = create_string_buffer(msg_len)
        self.swmm.swmm_getError(err_msg, msg_len)
        return err_msg.value.decode('utf-8')

    def get_warnings(self):
        return self.swmm.swmm_getWarnings()

    def get_count(self, obj_type):
        return self.swmm.swmm_getCount(obj_type)

    def get_name(self, obj_type, index, size=256):
        name = create_string_buffer(size)
        self.swmm.swmm_getName(obj_type, index, name, size)
        return name.value.decode('utf-8')

    def get_index(self, obj_type, name):
        return self.swmm.swmm_getIndex(obj_type, name.encode('utf-8'))

    def get_value(self, property, index):
        return self.swmm.swmm_getValue(property, index)

    def set_value(self, property, index, value):
        self.swmm.swmm_setValue(property, index, value)

    def get_saved_value(self, property, index, period):
        return self.swmm.swmm_getSavedValue(property, index, period)

    def write_line(self, line):
        self.swmm.swmm_writeLine(line.encode('utf-8'))

    def decode_date(self, date):
        year = c_int()
        month = c_int()
        day = c_int()
        hour = c_int()
        minute = c_int()
        second = c_int()
        day_of_week = c_int()
        self.swmm.swmm_decodeDate(date, byref(year), byref(month), byref(day), byref(hour), byref(minute), byref(second), byref(day_of_week))
        return (year.value, month.value, day.value, hour.value, minute.value, second.value, day_of_week.value)
