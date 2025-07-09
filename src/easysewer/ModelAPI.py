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
from .UDM import UrbanDrainageModel
from .SolverAPI import SWMMSolverAPI


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
        mode: str = "normal"
    ) -> tuple[str, str, str]:
        """
        Execute SWMM simulation with progress tracking and error checking.

        Args:
            inp_file: Path for inp .inp file. Auto-generated if None.
            rpt_file: Path for rpt .rpt file. Auto-generated if None.
            out_file: Path for output .out file. Auto-generated if None.
            mode: Execution mode ("fast" for quick run, "normal" for progress tracking)

        Returns:
            tuple: Paths to generated (inp_file, rpt_file, out_file)

        Raises:
            SystemExit: If fatal errors occur during simulation setup/execution

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

        # Export model to desired path
        self.to_inp(inp_file)

        # Initialize swmm solver
        solver = SWMMSolverAPI()

        # If using fast mode, then use run() method to execute the simulation
        if mode == "fast":
            solver.run(inp_file, rpt_file, out_file)
            solver.close()
            return inp_file, rpt_file, out_file

        # Open the model
        err = solver.open(inp_file, rpt_file, out_file)
        if err:
            print(f"Error opening SWMM project: {err}")
            print(solver.get_error())
            exit(1)

        # Start simulation
        err = solver.start(1)
        if err:
            print(f"Error starting SWMM simulation: {err}")
            print(solver.get_error())
            exit(1)

        # Prepare for simulation progress
        print("Simulation progress:")
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
            progress = (elapsed_time * 24 * 60 * 60 / total_seconds) * 100
            progress_int = int(progress)

            # Only update progress when it changes by at least 1%
            if progress_int > last_progress_int:
                # Calculate the number of characters to fill
                filled_length = int(bar_length * progress / 100)
                bar = '=' * filled_length + '+' * (bar_length - filled_length)

                # Print the entire progress bar each time (overwriting previous one)
                print(f"\r[{bar}] {progress_int}%", end='', flush=True)
                last_progress_int = progress_int

        # Complete the progress bar when finished
        print(f"\r[{'=' * bar_length}] 100%")

        # End the simulation
        err = solver.end()
        if err:
            print(f"Error ending SWMM simulation: {err}")
            print(solver.get_error())

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
                Prints warning message to stderr when exceeding 5% threshold
                Does not interrupt simulation execution
            """
            ERROR_THRESHOLD = 5
            if abs(error_percent) > ERROR_THRESHOLD:
                print(f"WARNING: {error_type} error percentage ({error_percent:.2f}%) exceeds {ERROR_THRESHOLD}%")

        # Check for errors over 5%
        _check_error("Runoff", runoff_error_percent)
        _check_error("Flow", flow_error_percent)
        _check_error("Quality", quality_error_percent)

        # Close the solver
        err = solver.close()
        if err:
            print(f"Error closing SWMM project: {err}")
            print(solver.get_error())
        return inp_file, rpt_file, out_file
