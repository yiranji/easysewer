"""
API extensions for Urban Drainage Modeling system

This module enhances the base UrbanDrainageModel with:
- Advanced simulation control with progress tracking
- Automated output file management
- Enhanced error checking and rpting
"""

import datetime
import uuid
import os
import copy
import logging
from typing import Callable, Optional
from .UDM import UrbanDrainageModel
from .SolverAPI import SWMMSolverAPI, FlexiblePondingSolverAPI
from .JsonHandler import JsonHandler
from .Node import Divider

logger = logging.getLogger(__name__)


def default_progress_callback(current: float, total: float, percent: float) -> None:
    """
    Default progress callback that prints a progress bar to stdout.
    """
    bar_length = 50
    filled_length = int(bar_length * percent / 100)
    bar = '=' * filled_length + '+' * (bar_length - filled_length)
    
    if percent >= 100:
        print(f"\r[{'=' * bar_length}] 100%")
    else:
        print(f"\r[{bar}] {int(percent)}%", end='', flush=True)


class Model(UrbanDrainageModel):
    """
    Enhanced SWMM model with advanced simulation capabilities.

    This class extends the base easysewer.Model (UrbanDrainageModel) with additional
    functionality for running simulations with progress tracking and error rpting.
    It provides both a fast simulation mode and a detailed step-by-step simulation
    with visual progress feedback.

    Attributes:
        Inherits all attributes from easysewer.Model (UrbanDrainageModel)
    """

    def __init__(self, model_path: str | None = None):
        """
        Initialize a Model instance with optional inp file.

        Args:
            model_path (str | None): Path to SWMM .inp file to load. If None,
                creates an empty model. Defaults to None.
        """
        super().__init__(model_path)

    def simulation(
            self,
            inp_file: str | None = None,
            rpt_file: str | None = None,
            out_file: str | None = None,
            solver: SWMMSolverAPI | FlexiblePondingSolverAPI = SWMMSolverAPI(),
            progress_callback: Optional[Callable[[float, float, float], None]] = None,
            **kwargs
    ) -> tuple[str, str, str]:
        """
        Execute SWMM simulation with progress tracking and error checking.

        Args:
            solver:
            inp_file: Path for inp .inp file. Auto-generated if None.
            rpt_file: Path for rpt .rpt file. Auto-generated if None.
            out_file: Path for output .out file. Auto-generated if None.
            progress_callback: Optional callback for progress updates.
                               Signature: (current_seconds, total_seconds, percent)

        Returns:
            tuple: Paths to generated (inp_file, rpt_file, out_file)

        Raises:
            RuntimeError: If fatal errors occur during simulation setup/execution
        """

        # Get current datetime as a filename-safe string
        now = datetime.datetime.now()
        date_string = now.strftime("%Y%m%d_%H%M%S")  # Format: YYYYMMDD_HHMMSS
        # Generate a UUID
        unique_id = str(uuid.uuid4())
        # Define output directory
        output_dir = 'simulation_output'

        # Combine datetime and UUID for a unique filename
        model_name = f"{date_string}_{unique_id}"
        # Set default file paths if not provided
        if inp_file is None:
            inp_file = os.path.join(output_dir, f"{model_name}.inp")
            # Check if directory exists, create it if not
            if not os.path.exists(output_dir):
                os.makedirs(output_dir)
        if rpt_file is None:
            rpt_file = os.path.join(output_dir, f"{model_name}.rpt")
            # Check if directory exists, create it if not
            if not os.path.exists(output_dir):
                os.makedirs(output_dir)
        if out_file is None:
            out_file = os.path.join(output_dir, f"{model_name}.out")
            # Check if directory exists, create it if not
            if not os.path.exists(output_dir):
                os.makedirs(output_dir)

        # Pre-export checks
        self._pre_export_checks()

        # Export model to desired path
        self.to_inp(inp_file)

        # Open the model
        err = solver.open(inp_file, rpt_file, out_file)
        if err:
            logger.error(f"Error opening SWMM project: {err}")
            logger.error(solver.get_error())
            raise RuntimeError(f"Error opening SWMM project: {err}")

        # Start simulation
        err = solver.start(1)
        if err:
            logger.error(f"Error starting SWMM simulation: {err}")
            logger.error(solver.get_error())
            raise RuntimeError(f"Error starting SWMM simulation: {err}")

        # Prepare for simulation progress
        if progress_callback is None:
            print("Simulation progress:")
            progress_callback = default_progress_callback

        bar_length = 50  # Length of progress bar
        last_progress_int = -1
        start_time = datetime.datetime(
            year=self.calc.simulation_start['year'],
            month=self.calc.simulation_start['month'],
            day=self.calc.simulation_start['day'],
            hour=self.calc.simulation_start['hour'],
            minute=self.calc.simulation_start['minute']
        )
        end_time = datetime.datetime(
            year=self.calc.simulation_end['year'],
            month=self.calc.simulation_end['month'],
            day=self.calc.simulation_end['day'],
            hour=self.calc.simulation_end['hour'],
            minute=self.calc.simulation_end['minute']
        )
        total_seconds = (end_time - start_time).total_seconds()

        # Simulation iteration
        while True:
            result, elapsed_time = solver.step()
            if elapsed_time <= 0:
                break

            # Progress bar - Convert elapsed time to percentage of total simulation time
            # Multiply by 24*60*60 to convert days to seconds (SWMM uses days as time unit)
            current_seconds = elapsed_time * 24 * 60 * 60
            progress = (current_seconds / total_seconds) * 100
            progress_int = int(progress)

            # Only update progress when it changes by at least 1%
            if progress_int > last_progress_int:
                progress_callback(current_seconds, total_seconds, progress)
                last_progress_int = progress_int

        # Complete the progress bar when finished
        progress_callback(total_seconds, total_seconds, 100.0)

        # End the simulation
        err = solver.end()
        if err:
            logger.error(f"Error ending SWMM simulation: {err}")
            logger.error(solver.get_error())

        # Check simulation mass balance errors (continuity errors)
        # These errors indicate the accuracy of the simulation results
        # Values under 5% are generally acceptable for most applications
        runoff_error_percent, flow_error_percent, quality_error_percent = solver.get_mass_bal_err()

        def _check_error(error_type: str, error_percent: float) -> None:
            """
            Validate simulation error percentages against acceptability threshold.

            Args:
                error_type: Category of error being checked from:
                    - Runoff: Rainfall runoff calculation errors
                    - Flow: Hydraulic flow continuity errors
                    - Quality: Water quality simulation errors
                error_percent: Calculated percentage error (positive/negative)

            Note:
                Logs warning message when exceeding 5% threshold
                Does not interrupt simulation execution
            """
            ERROR_THRESHOLD = 5
            if abs(error_percent) > ERROR_THRESHOLD:
                logger.warning(f"{error_type} error percentage ({error_percent:.2f}%) exceeds {ERROR_THRESHOLD}%")

        # Check for errors over 5%
        _check_error("Runoff", runoff_error_percent)
        _check_error("Flow", flow_error_percent)
        _check_error("Quality", quality_error_percent)

        # Close the solver
        err = solver.close()
        if err:
            logger.error(f"Error closing SWMM project: {err}")
            logger.error(solver.get_error())
        return inp_file, rpt_file, out_file

    def simulation_with_json(
            self,
            json_file: str,
            out_folder: str,
            file_name: str | None = None,
            solver: SWMMSolverAPI | FlexiblePondingSolverAPI = SWMMSolverAPI(),
            progress_callback: Optional[Callable[[float, float, float], None]] = None,
            **kwargs
    ) -> tuple[str, str, str]:
        """
        Create a configured model copy using JSON configuration file and run simulation.
        
        Args:
            solver:
            json_file: JSON configuration file path
            out_folder: Output folder for simulation files
            file_name: File name for simulation files
            progress_callback: Optional callback for progress updates.
        Returns:
            tuple: Paths to generated (inp_file, rpt_file, out_file)
        """
        # Load JSON configuration
        config = JsonHandler.load_json_config(json_file)

        # Create deep copy of current model
        model_copy = copy.deepcopy(self)

        # Apply JSON configuration (including calc and rain configurations)
        JsonHandler.apply_json_config(model_copy, config)

        # Print model summary
        model_copy.print_model_summary()

        # Simulation
        # Ensure output directory exists
        if not os.path.exists(out_folder):
            os.makedirs(out_folder, exist_ok=True)

        # Get current datetime as a filename-safe string
        if file_name is None:
            now = datetime.datetime.now()
            date_string = now.strftime("%Y%m%d_%H%M%S")  # Format: YYYYMMDD_HHMMSS
            # Generate a UUID
            unique_id = str(uuid.uuid4())
            file_name = f"{date_string}_{unique_id}"

        inp_file = os.path.join(out_folder, f"{file_name}.inp")
        rpt_file = os.path.join(out_folder, f"{file_name}.rpt")
        out_file = os.path.join(out_folder, f"{file_name}.out")

        model_copy.simulation(inp_file, rpt_file, out_file, solver, progress_callback=progress_callback)

        return inp_file, rpt_file, out_file

    def generate_json_template(self, output_path: str) -> None:
        """
        Generate example JSON file containing all configurable values of current model.
        
        Args:
            output_path: Output JSON file path
        """
        JsonHandler.generate_json_template(self, output_path)

    def print_model_summary(self) -> None:
        """
        Print summary information of model's rainfall and simulation configuration.
        
        Including:
        1. Traverse all areas and count the number of gages used
        2. Print rainfall duration and total rainfall of gages used by current model
        3. Print simulation start time, simulation duration, and report step
        """
        logger.info("=" * 50)
        logger.info("Model Configuration Summary")
        logger.info("=" * 50)

        # 1. Count the number of gages used
        used_gages = set()
        if hasattr(self, 'area'):
            for area in self.area:
                if hasattr(area, 'rain_gage') and area.rain_gage:
                    used_gages.add(area.rain_gage)

        logger.info(f"Number of subcatchments: {len(self.area) if hasattr(self, 'area') else 0}")
        logger.info(f"Number of rain gages used: {len(used_gages)}")
        logger.info(f"Rain gages used: {', '.join(used_gages) if used_gages else 'None'}")

        # 2. Print rainfall information
        logger.info("Rainfall Information:")
        if hasattr(self, 'rain') and hasattr(self.rain, 'gage_list'):
            for i, gage in enumerate(self.rain.gage_list, 1):
                logger.info(f"  Rain gage {i}: {gage.name}")
                logger.info(f"    Form: {getattr(gage, 'form', 'Unknown')}")

                # Calculate rainfall duration and total amount
                # Find corresponding time_series through gage.source
                time_series = None
                if hasattr(gage, 'source') and gage.source:
                    # Search for corresponding time_series in ts_list
                    for ts in self.rain.ts_list:
                        if ts.name == gage.source:
                            time_series = ts
                            break

                if time_series:
                    if hasattr(time_series, 'time') and hasattr(time_series,
                                                                'value') and time_series.time and time_series.value:
                        time_list = time_series.time  # Time in minutes
                        value_list = time_series.value  # Rainfall intensity values

                        if len(time_list) > 1 and len(value_list) > 1:
                            # Calculate duration (convert from minutes to hours)
                            start_time_minutes = time_list[0]
                            end_time_minutes = time_list[-1]
                            duration_hours = (end_time_minutes - start_time_minutes) / 60.0

                            # Calculate time interval (minutes)
                            time_interval_minutes = time_list[1] - time_list[0] if len(time_list) > 1 else 0
                            time_interval_hours = time_interval_minutes / 60.0

                            # Calculate total rainfall
                            total_rainfall = 0
                            if getattr(gage, 'form', '') == 'INTENSITY':
                                # Reference correct calculation method from TimeSeries.__repr__
                                # Rainfall (mm) = Rainfall intensity (mm/h) × Time interval (minutes) / 60
                                total_rainfall = sum([(v * time_interval_minutes / 60) for v in value_list])
                            else:
                                # If it's cumulative amount, take maximum value
                                total_rainfall = max(value_list)

                            logger.info(f"    Rainfall duration: {duration_hours:.2f} hours")
                            logger.info(f"    Time interval: {time_interval_minutes:.0f} minutes")
                            logger.info(f"    Total rainfall: {total_rainfall:.2f} mm")
                        else:
                            logger.info(f"    Rainfall data: Time or value list is empty")
                    elif hasattr(time_series, 'data_list') and time_series.data_list:
                        # Compatible with old data_list format
                        data_list = time_series.data_list
                        if data_list and len(data_list) > 1:
                            # Calculate duration (assume data is sorted by time, time in seconds)
                            start_time = data_list[0][0] if len(data_list[0]) > 0 else 0
                            end_time = data_list[-1][0] if len(data_list[-1]) > 0 else 0
                            duration_hours = (end_time - start_time) / 3600 if end_time > start_time else 0

                            # Calculate time interval (seconds to hours)
                            time_interval_seconds = data_list[1][0] - data_list[0][0] if len(data_list) > 1 else 0
                            time_interval_hours = time_interval_seconds / 3600.0

                            # Calculate total rainfall
                            total_rainfall = 0
                            if len(data_list[0]) > 1:
                                if getattr(gage, 'form', '') == 'INTENSITY':
                                    # If it's intensity, calculate total by multiplying intensity with time interval
                                    for row in data_list:
                                        if len(row) > 1:
                                            intensity = row[1]
                                            total_rainfall += intensity * time_interval_hours
                                else:
                                    # If it's cumulative amount, take maximum value
                                    total_rainfall = max(row[1] for row in data_list if len(row) > 1)

                            logger.info(f"    Rainfall duration: {duration_hours:.2f} hours")
                            logger.info(f"    Time interval: {time_interval_seconds / 60:.0f} minutes")
                            logger.info(f"    Total rainfall: {total_rainfall:.2f} mm")
                        else:
                            logger.info(f"    Rainfall data: No data")
                    else:
                        logger.info(f"    Rainfall data: No time series data")
                else:
                    logger.info(f"    Rainfall data: No time series")
        else:
            logger.info("  No rain gage data")

        # 3. Print simulation configuration
        logger.info("Simulation Configuration:")
        if hasattr(self, 'calc'):
            # Simulation start time
            if hasattr(self.calc, 'simulation_start'):
                start = self.calc.simulation_start
                if isinstance(start, dict):
                    logger.info(
                        f"  Simulation start time: {start.get('year', '?')}-{start.get('month', '?'):02d}-{start.get('day', '?'):02d} {start.get('hour', '?'):02d}:{start.get('minute', 0):02d}")
                else:
                    logger.info(f"  Simulation start time: {start}")
            else:
                logger.info(f"  Simulation start time: Not set")

            # Simulation duration
            if hasattr(self.calc, 'simulation_start') and hasattr(self.calc, 'simulation_end'):
                start = self.calc.simulation_start
                end = self.calc.simulation_end
                if isinstance(start, dict) and isinstance(end, dict):
                    try:
                        start_dt = datetime.datetime(start.get('year', 2000), start.get('month', 1),
                                                     start.get('day', 1),
                                                     start.get('hour', 0), start.get('minute', 0))
                        end_dt = datetime.datetime(end.get('year', 2000), end.get('month', 1), end.get('day', 1),
                                                   end.get('hour', 0), end.get('minute', 0))
                        duration = end_dt - start_dt
                        logger.info(f"  Simulation duration: {duration.total_seconds() / 3600:.2f} hours")
                    except:
                        logger.info(f"  Simulation duration: Unable to calculate")
                else:
                    logger.info(f"  Simulation duration: Unable to calculate")
            else:
                logger.info(f"  Simulation duration: End time not set")

            # Report step
            if hasattr(self.calc, 'report_step'):
                step = self.calc.report_step
                if isinstance(step, dict):
                    hours = step.get('hour', 0)
                    minutes = step.get('minute', 0)
                    seconds = step.get('second', 0)
                    total_minutes = hours * 60 + minutes + seconds / 60
                    logger.info(f"  Report step: {hours:02d}:{minutes:02d}:{seconds:02d} ({total_minutes:.1f} minutes)")
                else:
                    logger.info(f"  Report step: {step}")
            else:
                logger.info(f"  Report step: Not set")
        else:
            logger.info("  No calc configuration")

        logger.info("=" * 50)

    def _pre_export_checks(self) -> None:

        # Divider
        has_divider = any(isinstance(n, Divider) for n in self.node)
        if has_divider and str(self.calc.flow_routing_method).upper() == 'DYNWAVE':
            logger.warning("Divider nodes are inactive under DYNWAVE and behave as junction nodes.")
