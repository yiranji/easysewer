"""
pass
"""
import platform
import os
import sys
from ctypes import CDLL, c_char_p, c_int, c_double, c_float, byref, POINTER, create_string_buffer


class SWMMSolverAPI:
    swmm_NODE_TYPE = 300
    swmm_NODE_ELEV = 301
    swmm_NODE_MAXDEPTH = 302
    swmm_NODE_DEPTH = 303
    swmm_NODE_HEAD = 304
    swmm_NODE_VOLUME = 305
    swmm_NODE_LATFLOW = 306
    swmm_NODE_INFLOW = 307
    swmm_NODE_OVERFLOW = 308
    swmm_NODE_RPTFLAG = 309

    def __init__(self):
        #
        if getattr(sys, 'frozen', False):
            if hasattr(sys, '_MEIPASS'):
                base_path = sys._MEIPASS
            else:
                base_path = os.path.dirname(sys.executable)
        else:
            # Develop environment
            base_path = os.path.dirname(os.path.abspath(__file__))

        system = platform.system()
        if system == 'Windows':
            possible_paths = [
                os.path.join(base_path, 'libs', 'win', 'swmm5.dll.esdll'),
                os.path.join(base_path, 'easysewer', 'libs', 'win', 'swmm5.dll.esdll'),
                os.path.join(os.path.dirname(__file__), 'libs', 'win', 'swmm5.dll.esdll')
            ]
            lib_path = None
            for path in possible_paths:
                if os.path.exists(path):
                    lib_path = path
                    break
            if lib_path is None:
                raise FileNotFoundError(f"Could not find swmm-output.dll in any of these locations: {possible_paths}")

        elif system == 'Linux':
            possible_paths = [
                os.path.join(base_path, 'libs', 'linux', 'libswmm5.so.esso'),
                os.path.join(base_path, 'easysewer', 'libs', 'linux', 'libswmm5.so.esso'),
                os.path.join(os.path.dirname(__file__), 'libs', 'linux', 'libswmm5.so.esso')
            ]
            lib_path = None
            for path in possible_paths:
                if os.path.exists(path):
                    lib_path = path
                    break
            if lib_path is None:
                raise FileNotFoundError(f"Could not find swmm-output.so in any of these locations: {possible_paths}")

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


class FlexiblePondingSolverAPI(SWMMSolverAPI):
    """
    A flexible ponding solver API that extends the SWMM solver API with additional functionality.
    This class inherits from SWMMSolverAPI but overrides certain methods to provide custom behavior.
    """

    swmm_NODE_PONDDEPTH = 310

    def __init__(self, model):
        """
        Initialize the FlexiblePondingSolverAPI with a model.
        
        Args:
            model: The model to be used with this solver
        """
        # Store the model reference
        self.model = model

        # Set up the library path for flexible ponding solver DLL
        system = platform.system()
        if getattr(sys, 'frozen', False):
            if hasattr(sys, '_MEIPASS'):
                base_path = sys._MEIPASS
            else:
                base_path = os.path.dirname(sys.executable)
        else:
            # Develop environment
            base_path = os.path.dirname(os.path.abspath(__file__))

        if system == 'Windows':
            possible_paths = [
                os.path.join(base_path, 'libs', 'win', 'flexible_ponding.dll.esdll'),
                os.path.join(base_path, 'easysewer', 'libs', 'win', 'flexible_ponding.dll.esdll'),
                os.path.join(os.path.dirname(__file__), 'libs', 'win', 'flexible_ponding.dll.esdll')
            ]
            lib_path = None
            for path in possible_paths:
                if os.path.exists(path):
                    lib_path = path
                    break
            if lib_path is None:
                raise FileNotFoundError(
                    f"Could not find flexibleponding.dll in any of these locations: {possible_paths}")

        else:
            raise OSError('Unsupported operating system')

        # Load the flexible ponding solver library
        self.swmm = CDLL(lib_path)
        self._set_prototypes()

        # Prepare
        self.ponding_nodes_index = None
        self.ponding_nodes_depth = None
        self.ponding_nodes_volume = None
        self.model_prepare()

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

        self.swmm.swmm_decodeDate.argtypes = [c_double, POINTER(c_int), POINTER(c_int), POINTER(c_int), POINTER(c_int),
                                              POINTER(c_int), POINTER(c_int), POINTER(c_int)]
        self.swmm.swmm_decodeDate.restype = None

        # New API functions for exposing internal functions
        self.swmm.swmm_execRouting.argtypes = []
        self.swmm.swmm_execRouting.restype = c_int

        self.swmm.swmm_saveResults.argtypes = []
        self.swmm.swmm_saveResults.restype = c_int

        self.swmm.swmm_getCurrentTime.argtypes = []
        self.swmm.swmm_getCurrentTime.restype = c_double

        self.swmm.swmm_getRoutingDuration.argtypes = []
        self.swmm.swmm_getRoutingDuration.restype = c_double

    def exec_routing(self):
        """
        Routes flow & WQ through drainage system over a single time step.
        This method exposes the internal execRouting function as an API.

        Returns:
            int: Error code (0 for success)
        """
        return self.swmm.swmm_execRouting()

    def save_results(self):
        """
        Saves current results to binary output file.
        This method exposes the internal saveResults function as an API.

        Returns:
            int: Error code (0 for success)
        """
        return self.swmm.swmm_saveResults()

    def get_current_time(self):
        """
        Retrieves the current elapsed simulation time.

        Returns:
            float: Current simulation time in decimal days
        """
        return self.swmm.swmm_getCurrentTime()

    def get_routing_duration(self):
        """
        Retrieves the total routing duration of the simulation.

        Returns:
            float: Total routing duration in milliseconds
        """
        return self.swmm.swmm_getRoutingDuration()

    def model_prepare(self):
        # Check allow ponding
        if self.model.calc.allow_ponding is not True:
            raise Exception("FlexiblePondingSolverAPI only apply to model allowing ponding.")
        # Check Solver
        if self.model.calc.flow_routing_method != "DYNWAVE":
            raise Exception("FlexiblePondingSolverAPI only apply to 'DYNWAVE' solver.")

        # Prepare ponding nodes
        self.ponding_nodes_index = self.get_ponding_junctions()
        self.ponding_nodes_depth = [0] * len(self.ponding_nodes_index)  # Create a list to store ponding depth
        self.ponding_nodes_volume = [0] * len(self.ponding_nodes_index)

    def get_ponding_junctions(self):
        junctions_index_list = []
        for index, node in enumerate(self.model.node):
            if hasattr(node, "surface_ponding_area"):
                if node.surface_ponding_area > 0:  # Nodes with 0 pondedArea are considered to not allow ponding in SWMM
                    junctions_index_list.append(index)
        return tuple(junctions_index_list)

    def step(self):
        """
        Override the step method to provide custom behavior for flexible ponding solver.
        
        This method extends the standard SWMM step function by adding custom ponding depth
        calculations for nodes that have ponding areas defined. For each simulation time step,
        it retrieves the current ponding depth from the SWMM engine, applies custom ponding
        logic through the update_ponding_depth method, and then updates the SWMM model with
        the modified ponding depth values.
        
        Returns:
            tuple: A tuple containing the result code and elapsed time value
                  - result (int): The status code returned by the SWMM engine
                  - elapsed_time.value (float): The elapsed simulation time in decimal days
        
        Note:
            The property code 310 is used to get/set the ponding depth value in the SWMM engine.
        """
        current_routing_time = self.get_current_time() * 86400.0 * 1000.0  # Change to millisecond
        routing_duration = self.get_routing_duration()
        if current_routing_time >= routing_duration:
            return 0, 0.0

        # Routing
        error_code = self.exec_routing()

        # Process each node that has ponding enabled
        for i, (node_index, last_ponding_depth, last_node_volume) in enumerate(
                zip(self.ponding_nodes_index, self.ponding_nodes_depth, self.ponding_nodes_volume)):

            # Get the current ponding depth calculated by the SWMM engine
            current_ponding_depth = self.get_value(self.swmm_NODE_PONDDEPTH, node_index)
            current_overflow = self.get_value(self.swmm_NODE_OVERFLOW, node_index)
            current_volume = self.get_value(self.swmm_NODE_VOLUME, node_index)

            if current_ponding_depth < 0.001:
                continue
            if current_overflow < 0.001:
                continue
            if current_ponding_depth < last_ponding_depth:
                continue

            # Apply custom ponding depth logic
            updated_ponding_depth, updated_overflow, updated_volume = (
                self.update_ponding_status(
                    last_ponding_depth, last_node_volume,
                    current_ponding_depth, current_overflow, current_volume))

            # Store the updated ponding depth for use in the next time step
            self.ponding_nodes_depth[i] = updated_ponding_depth
            self.ponding_nodes_volume[i] = updated_volume

            # Update the SWMM engine with the modified ponding depth
            # DynamicWave first uses depth to update volume
            self.set_value(self.swmm_NODE_PONDDEPTH, node_index, updated_ponding_depth)
            self.set_value(self.swmm_NODE_OVERFLOW, node_index, updated_overflow)
            self.set_value(self.swmm_NODE_VOLUME, node_index, updated_volume)

        # Save results
        if error_code == 0:
            error_code = self.save_results()
        elapsed_time = self.get_current_time()

        return error_code, elapsed_time

    def update_ponding_status(self,
                              last_ponding_depth, last_node_volume,
                              current_ponding_depth, current_overflow, current_volume):

        delta_time = (current_volume - last_node_volume) / current_overflow
        delta_depth = current_ponding_depth - last_ponding_depth
        node_surface_area = (current_volume - last_node_volume) / delta_depth

        # Let 50% ponding water to disappear
        updated_ponding_depth = current_ponding_depth - delta_depth / 2

        if updated_ponding_depth > current_ponding_depth:
            print(delta_depth)
            raise Exception("updated_ponding_depth should be lower than current_ponding_depth")
        updated_overflow = (current_ponding_depth - updated_ponding_depth) * node_surface_area / delta_time
        updated_volume = current_volume - (current_ponding_depth - updated_ponding_depth) * node_surface_area

        return updated_ponding_depth, updated_overflow, updated_volume

    # Explicitly not inheriting the run method by overriding it to raise NotImplementedError
    def run(self, input_file, report_file, output_file):
        """
        This method is intentionally not implemented as per requirements.
        
        Raises:
            NotImplementedError: This method is not supported in FlexiblePondingSolverAPI
        """
        raise NotImplementedError("The run method is not supported in FlexiblePondingSolverAPI")
