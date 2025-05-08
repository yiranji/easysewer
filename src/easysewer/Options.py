"""
Calculation Options Module

This module manages simulation options and parameters for the drainage model,
including time steps, routing methods, and other calculation settings.
"""
from .utils import *


class CalculationInformation:
    """
    Controls simulation parameters and calculation options.
    
    Manages various settings that control how the drainage simulation is performed,
    including time steps, routing methods, and reporting options.

    Attributes:
        start_date (str): Simulation start date
        start_time (str): Simulation start time
        report_start_date (str): Report period start date
        report_start_time (str): Report period start time
        end_date (str): Simulation end date
        end_time (str): Simulation end time
        sweep_start (str): Street sweeping start date
        sweep_end (str): Street sweeping end date
        dry_days (float): Days with no rain prior to simulation
        report_step (str): Reporting time step
        wet_step (str): Wet weather time step
        dry_step (str): Dry weather time step
        routing_step (str): Flow routing time step
        allow_ponding (bool): Whether ponding is allowed
        inertial_damping (str): Type of inertial damping
        normal_flow_limited (str): Normal flow limitation method
        force_main_equation (str): Force main equation selection
        variable_step (float): Variable step for dynamic wave routing
        lengthening_step (float): Lengthening step for dynamic wave routing
        min_surface_area (float): Minimum surface area for nodes
        max_trials (int): Maximum trials per time step
        head_tolerance (float): Head difference tolerance
        sys_flow_tolerance (float): System flow tolerance
        lat_flow_tolerance (float): Lateral flow tolerance
    """
    def __init__(self):
        # general option
        self.flow_unit = 'CMS'
        self.infiltration_method = 'HORTON'
        self.flow_routing_method = 'KINWAVE'
        self.link_offsets_type = 'DEPTH'
        self.force_main_equation = 'H-W'

        self.ignore_rainfall = False
        self.ignore_snow_melt = False
        self.ignore_ground_water = False
        self.ignore_RDII = False
        self.ignore_routing = False
        self.ignore_water_quality = False

        self.allow_ponding = True
        self.skip_steady_state = False
        self.system_flow_tol = 5
        self.lateral_flow_tol = 5

        self.simulation_start = {"year": 2023, "month": 4, "day": 28, "hour": 8, "minute": 0}
        self.simulation_end = {"year": 2023, "month": 4, "day": 28, "hour": 17, "minute": 0}
        self.report_start = {"year": 2023, "month": 4, "day": 28, "hour": 8, "minute": 0}
        self.sweep_start = {"month": 1, "day": 1}
        self.sweep_end = {"month": 12, "day": 31}
        self.dry_days = 0

        self.report_step = {"hour": 0, "minute": 15, "second": 0}
        self.wet_step = {"hour": 0, "minute": 5, "second": 0}  # runoff
        self.dry_step = {"hour": 1, "minute": 0, "second": 0}  # runoff
        self.routing_step = 600  # in seconds
        self.lengthening_step = 0  # in seconds
        self.variable_step = 0
        self.minimum_step = 0.5  # in seconds

        self.inertial_damping = 'PARTIAL'
        self.normal_flow_limited = 'BOTH'

        self.minimum_surface_area = 0
        self.minimum_slope = 0
        self.max_trials = 8
        self.head_tolerance = 0.0015  # in meters

        self.threads = 1
        self.temp_directory = None

        # report section information
        self.report_input = False
        self.report_check_continuity = True
        self.report_flow_statistics = True
        self.report_controls = False
        self.report_subcatchments = 'ALL'
        self.report_nodes = 'ALL'
        self.report_links = 'ALL'

        # map section information
        self.map_dimensions = [0, 0, 1000, 1000]
        self.map_units = 'None'

        # evaporation section information
        self.evaporation_constant = 0
        self.evaporation_dry_only = False

    def __repr__(self):
        return f'unit: {self.flow_unit}'

    def write_to_swmm_inp(self, filename):
        """

        :param filename:
        """
        with open(filename, 'a', encoding='utf-8') as f:
            f.write('[OPTIONS]\n')
            f.write(f'FLOW_UNITS             {self.flow_unit}\n')
            f.write(f'INFILTRATION           {self.infiltration_method}\n')
            f.write(f'FLOW_ROUTING           {self.flow_routing_method}\n')
            f.write(f'LINK_OFFSETS           {self.link_offsets_type}\n')
            f.write(f'FORCE_MAIN_EQUATION    {self.force_main_equation}\n')
            f.write('\n')
            f.write('IGNORE_RAINFALL        ' + ('YES' if self.ignore_rainfall else 'NO') + '\n')
            f.write('IGNORE_SNOWMELT        ' + ('YES' if self.ignore_snow_melt else 'NO') + '\n')
            f.write('IGNORE_GROUNDWATER     ' + ('YES' if self.ignore_ground_water else 'NO') + '\n')
            f.write('IGNORE_RDII            ' + ('YES' if self.ignore_RDII else 'NO') + '\n')
            f.write('IGNORE_ROUTING         ' + ('YES' if self.ignore_routing else 'NO') + '\n')
            f.write('IGNORE_QUALITY         ' + ('YES' if self.ignore_water_quality else 'NO') + '\n')
            f.write('\n')
            f.write('ALLOW_PONDING          ' + ('YES' if self.allow_ponding else 'NO') + '\n')
            f.write('SKIP_STEADY_STATE      ' + ('YES' if self.skip_steady_state else 'NO') + '\n')
            f.write(f'SYS_FLOW_TOL           {self.system_flow_tol}\n')
            f.write(f'LAT_FLOW_TOL           {self.lateral_flow_tol}\n')

            f.write('\n')
            f.write('START_DATE             ')
            f.write(str(self.simulation_start['month']).zfill(2) + '/')
            f.write(str(self.simulation_start['day']).zfill(2) + '/')
            f.write(str(self.simulation_start['year']) + '\n')
            f.write('START_TIME             ')
            f.write(str(self.simulation_start['hour']).zfill(2) + ':')
            f.write(str(self.simulation_start['minute']).zfill(2) + '\n')
            f.write('END_DATE               ')
            f.write(str(self.simulation_end['month']).zfill(2) + '/')
            f.write(str(self.simulation_end['day']).zfill(2) + '/')
            f.write(str(self.simulation_end['year']) + '\n')
            f.write('END_TIME               ')
            f.write(str(self.simulation_end['hour']).zfill(2) + ':')
            f.write(str(self.simulation_end['minute']).zfill(2) + '\n')
            f.write('REPORT_START_DATE      ')
            f.write(str(self.report_start['month']).zfill(2) + '/')
            f.write(str(self.report_start['day']).zfill(2) + '/')
            f.write(str(self.report_start['year']) + '\n')
            f.write('REPORT_START_TIME      ')
            f.write(str(self.report_start['hour']).zfill(2) + ':')
            f.write(str(self.report_start['minute']).zfill(2) + '\n')
            f.write('SWEEP_START            ')
            f.write(str(self.sweep_start['month']).zfill(2) + '/')
            f.write(str(self.sweep_start['day']).zfill(2) + '\n')
            f.write('SWEEP_END              ')
            f.write(str(self.sweep_end['month']).zfill(2) + '/')
            f.write(str(self.sweep_end['day']).zfill(2) + '\n')

            f.write('\n')
            f.write(f'DRY_DAYS               {self.dry_days}\n')
            f.write('REPORT_STEP            ')
            f.write(str(self.report_step['hour']).zfill(2) + ':')
            f.write(str(self.report_step['minute']).zfill(2) + ':')
            f.write(str(self.report_step['second']).zfill(2) + '\n')
            f.write('WET_STEP               ')
            f.write(str(self.wet_step['hour']).zfill(2) + ':')
            f.write(str(self.wet_step['minute']).zfill(2) + ':')
            f.write(str(self.wet_step['second']).zfill(2) + '\n')
            f.write('DRY_STEP               ')
            f.write(str(self.dry_step['hour']).zfill(2) + ':')
            f.write(str(self.dry_step['minute']).zfill(2) + ':')
            f.write(str(self.dry_step['second']).zfill(2) + '\n')

            f.write('\n')
            f.write(f'ROUTING_STEP           {self.routing_step}\n')
            f.write(f'LENGTHENING_STEP       {self.lengthening_step}\n')
            f.write(f'VARIABLE_STEP          {self.variable_step}\n')
            f.write(f'MINIMUM_STEP           {self.minimum_step}\n')

            f.write('\n')
            f.write(f'INERTIAL_DAMPING       {self.inertial_damping}\n')
            f.write(f'NORMAL_FLOW_LIMITED    {self.normal_flow_limited}\n')
            f.write(f'MIN_SURFAREA           {self.minimum_surface_area}\n')
            f.write(f'MIN_SLOPE              {self.minimum_slope}\n')
            f.write(f'MAX_TRIALS             {self.max_trials}\n')
            f.write(f'HEAD_TOLERANCE         {self.head_tolerance}\n')
            f.write(f'THREADS                {self.threads}\n')
            if self.temp_directory is not None:
                f.write(f'TEMPDIR                {self.temp_directory}\n')

            f.write('\n\n[REPORT]\n')
            f.write('INPUT                  ' + ('YES' if self.report_input else 'NO') + '\n')
            f.write('CONTINUITY             ' + ('YES' if self.report_check_continuity else 'NO') + '\n')
            f.write('FLOWSTATS              ' + ('YES' if self.report_flow_statistics else 'NO') + '\n')
            f.write('CONTROLS               ' + ('YES' if self.report_controls else 'NO') + '\n')
            f.write(f'SUBCATCHMENTS          {self.report_subcatchments}\n')
            f.write(f'NODES                  {self.report_nodes}\n')
            f.write(f'LINKS                  {self.report_links}\n')

            f.write('\n\n[MAP]\n')
            f.write(
                f'DIMENSIONS  {self.map_dimensions[0]}  {self.map_dimensions[1]}  {self.map_dimensions[2]}  {self.map_dimensions[3]}\n')
            f.write(f'Units  {self.map_units}\n')

            f.write('\n\n[EVAPORATION]\n')
            f.write(f'CONSTANT  {self.evaporation_constant}\n')
            f.write('DRY_ONLY  ' + ('YES' if self.evaporation_dry_only else 'NO') + '\n')

    def read_from_swmm_inp(self, filename):
        """

        :param filename:
        :return:
        """
        contents = get_swmm_inp_content(filename, '[OPTIONS]')
        for line in contents:
            pair = line.split()
            match pair[0]:
                case 'FLOW_UNITS':
                    self.flow_unit = pair[1]
                case 'INFILTRATION':
                    self.infiltration_method = pair[1]
                case 'FLOW_ROUTING':
                    self.flow_routing_method = pair[1]
                case 'LINK_OFFSETS':
                    self.link_offsets_type = pair[1]
                case 'FORCE_MAIN_EQUATION':
                    self.force_main_equation = pair[1]
                case 'IGNORE_RAINFALL':
                    self.ignore_rainfall = True if pair[1] == 'YES' else False
                case 'IGNORE_SNOWMELT':
                    self.ignore_snow_melt = True if pair[1] == 'YES' else False
                case 'IGNORE_GROUNDWATER':
                    self.ignore_ground_water = True if pair[1] == 'YES' else False
                case 'IGNORE_RDII':
                    self.ignore_RDII = True if pair[1] == 'YES' else False
                case 'IGNORE_ROUTING':
                    self.ignore_routing = True if pair[1] == 'YES' else False
                case 'IGNORE_QUALITY':
                    self.ignore_water_quality = True if pair[1] == 'YES' else False
                case 'ALLOW_PONDING':
                    self.allow_ponding = True if pair[1] == 'YES' else False
                case 'SKIP_STEADY_STATE':
                    self.skip_steady_state = True if pair[1] == 'YES' else False
                case 'SYS_FLOW_TOL':
                    self.system_flow_tol = int(pair[1])
                case 'LAT_FLOW_TOL':
                    self.lateral_flow_tol = int(pair[1])
                case 'START_DATE':
                    keys = [int(i) for i in pair[1].split('/')]
                    self.simulation_start['year'] = keys[2]
                    self.simulation_start['month'] = keys[0]
                    self.simulation_start['day'] = keys[1]
                case 'START_TIME':
                    keys = [int(i) for i in pair[1].split(':')]
                    self.simulation_start['hour'] = keys[0]
                    self.simulation_start['minute'] = keys[1]
                case 'END_DATE':
                    keys = [int(i) for i in pair[1].split('/')]
                    self.simulation_end['year'] = keys[2]
                    self.simulation_end['month'] = keys[0]
                    self.simulation_end['day'] = keys[1]
                case 'END_TIME':
                    keys = [int(i) for i in pair[1].split(':')]
                    self.simulation_end['hour'] = keys[0]
                    self.simulation_end['minute'] = keys[1]
                case 'REPORT_START_DATE':
                    keys = [int(i) for i in pair[1].split('/')]
                    self.report_start['year'] = keys[2]
                    self.report_start['month'] = keys[0]
                    self.report_start['day'] = keys[1]
                case 'REPORT_START_TIME':
                    keys = [int(i) for i in pair[1].split(':')]
                    self.report_start['hour'] = keys[0]
                    self.report_start['minute'] = keys[1]
                case 'SWEEP_START':
                    keys = [int(i) for i in pair[1].split('/')]
                    self.sweep_start['month'] = keys[0]
                    self.sweep_start['day'] = keys[1]
                case 'SWEEP_END':
                    keys = [int(i) for i in pair[1].split('/')]
                    self.sweep_end['month'] = keys[0]
                    self.sweep_end['day'] = keys[1]
                case 'DRY_DAYS':
                    self.dry_days = int(pair[1])
                case 'REPORT_STEP':
                    keys = [int(i) for i in pair[1].split(':')]
                    self.report_step['hour'] = keys[0]
                    self.report_step['minute'] = keys[1]
                    self.report_step['second'] = keys[2]
                case 'WET_STEP':
                    keys = [int(i) for i in pair[1].split(':')]
                    self.wet_step['hour'] = keys[0]
                    self.wet_step['minute'] = keys[1]
                    self.wet_step['second'] = keys[2]
                case 'DRY_STEP':
                    keys = [int(i) for i in pair[1].split(':')]
                    self.dry_step['hour'] = keys[0]
                    self.dry_step['minute'] = keys[1]
                    self.dry_step['second'] = keys[2]
                case 'ROUTING_STEP':
                    keys = [int(i) for i in pair[1].split(':')]
                    if len(keys) == 1:
                        self.routing_step = keys[0]
                    elif len(keys) == 2:
                        self.routing_step = keys[-1] + keys[-2] * 60
                    elif len(keys) == 3:
                        self.routing_step = keys[-1] + keys[-2] * 60 + keys[-3] * 3600
                    else:
                        pass
                case 'LENGTHENING_STEP':
                    keys = [int(i) for i in pair[1].split(':')]
                    if len(keys) == 1:
                        self.lengthening_step = keys[0]
                    elif len(keys) == 2:
                        self.lengthening_step = keys[-1] + keys[-2] * 60
                    elif len(keys) == 3:
                        self.lengthening_step = keys[-1] + keys[-2] * 60 + keys[-3] * 3600
                    else:
                        pass
                case 'VARIABLE_STEP':
                    self.variable_step = float(pair[1])
                case 'MINIMUM_STEP':
                    self.minimum_step = float(pair[1])
                case 'INERTIAL_DAMPING':
                    self.inertial_damping = pair[1]
                case 'NORMAL_FLOW_LIMITED':
                    self.normal_flow_limited = pair[1]
                case 'MIN_SURFAREA':
                    self.minimum_surface_area = float(pair[1])
                case 'MIN_SLOPE':
                    self.minimum_slope = float(pair[1])
                case 'MAX_TRIALS':
                    self.max_trials = int(pair[1])
                case 'HEAD_TOLERANCE':
                    self.head_tolerance = float(pair[1])
                case 'THREADS':
                    self.threads = int(pair[1])
                case 'TEMPDIR':
                    self.temp_directory = pair[1]
                case _:
                    pass
        contents = get_swmm_inp_content(filename, '[REPORT]')
        for line in contents:
            pair = line.split()
            match pair[0]:
                case 'INPUT':
                    self.report_input = True if pair[1] == 'YES' else False
                case 'CONTINUITY':
                    self.report_check_continuity = True if pair[1] == 'YES' else False
                case 'FLOWSTATS':
                    self.report_flow_statistics = True if pair[1] == 'YES' else False
                case 'CONTROLS':
                    self.report_controls = True if pair[1] == 'YES' else False
                case 'SUBCATCHMENTS':
                    self.report_subcatchments = pair[1]
                case 'NODES':
                    self.report_nodes = pair[1]
                case 'LINKS':
                    self.report_links = pair[1]
        contents = get_swmm_inp_content(filename, '[MAP]')
        for line in contents:
            pair = line.split()
            match pair[0]:
                case 'DIMENSIONS':
                    self.map_dimensions = [float(pair[1]), float(pair[2]), float(pair[3]), float(pair[4])]
                case 'Units':
                    self.map_units = pair[1]
        return 0
