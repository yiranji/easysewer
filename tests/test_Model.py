import unittest
import os
import tempfile
import shutil
from pathlib import Path
from easysewer.ModelAPI import Model
from easysewer.SolverAPI import FlexiblePondingSolverAPI


class TestModelFunctionality(unittest.TestCase):
    """
    Test class for Model functionality
    """
    
    @classmethod
    def setUpClass(cls):
        """
        Test class initialization, set up test data paths and temporary directories
        """
        # Get test data directory path
        cls.test_data_dir = Path(__file__).parent / "test_data"
        cls.test_data_dir.mkdir(exist_ok=True)
        
        # Set test file paths
        cls.test_inp_file = cls.test_data_dir / "Model" / "cubic.inp"
        cls.test_json_file = cls.test_data_dir / "Model" / "rain_event_1.json"
        cls.test_json_with_date_file = cls.test_data_dir / "Model" / "rain_event_1_with_date.json"
        
        # Create temporary output directory
        cls.temp_output_dir = Path(tempfile.mkdtemp())
    
    @classmethod
    def tearDownClass(cls):
        """
        Test class cleanup, delete temporary files and directories
        """
        # Clean up temporary output directory
        if cls.temp_output_dir.exists():
            shutil.rmtree(cls.temp_output_dir)
    
    def setUp(self):
        """
        Initialization before each test method execution
        """
        # Check if test data file exists
        if not self.test_inp_file.exists():
            self.skipTest(f"Test data file does not exist: {self.test_inp_file}")
    
    def test_model_initialization_with_file(self):
        """
        Test Model initialization with inp file
        """
        model = Model(str(self.test_inp_file))
        self.assertIsNotNone(model)
        # Verify that the model correctly loaded basic components
        self.assertTrue(hasattr(model, 'area'))
        self.assertTrue(hasattr(model, 'node'))
        self.assertTrue(hasattr(model, 'link'))
    
    def test_model_initialization_empty(self):
        """
        Test creating empty Model
        """
        model = Model()
        self.assertIsNotNone(model)
    
    def test_simulation_normal_mode(self):
        """
        Test normal simulation mode (with progress bar)
        """
        model = Model(str(self.test_inp_file))
        
        # Specify output file paths
        inp_file = str(self.temp_output_dir / "test_normal.inp")
        rpt_file = str(self.temp_output_dir / "test_normal.rpt")
        out_file = str(self.temp_output_dir / "test_normal.out")
        
        # Execute normal simulation
        result_inp, result_rpt, result_out = model.simulation(
            inp_file=inp_file,
            rpt_file=rpt_file,
            out_file=out_file,
            mode="normal"
        )
        
        # Verify returned file paths
        self.assertEqual(result_inp, inp_file)
        self.assertEqual(result_rpt, rpt_file)
        self.assertEqual(result_out, out_file)
        
        # Verify output files are generated
        self.assertTrue(os.path.exists(inp_file))
        self.assertTrue(os.path.exists(rpt_file))
        self.assertTrue(os.path.exists(out_file))
    
    def test_simulation_with_json(self):
        """
        Test simulation with JSON configuration file
        """
        # Skip test if JSON configuration file does not exist
        if not self.test_json_file.exists():
            self.skipTest(f"JSON configuration file does not exist: {self.test_json_file}")
        
        model = Model(str(self.test_inp_file))
        
        # Execute JSON configuration simulation
        inp_file, rpt_file, out_file = model.simulation_with_json(
            json_file=str(self.test_json_file),
            out_folder=str(self.temp_output_dir),
        )
        
        # Verify output files are generated
        self.assertTrue(os.path.exists(inp_file))
        self.assertTrue(os.path.exists(rpt_file))
        self.assertTrue(os.path.exists(out_file))
        
        # Verify files are in the specified output directory
        self.assertTrue(str(self.temp_output_dir) in inp_file)

    def test_simulation_with_json_and_custom_file_name(self):
        """
        Test simulation with JSON configuration file
        """
        # Skip test if JSON configuration file does not exist
        if not self.test_json_file.exists():
            self.skipTest(f"JSON configuration file does not exist: {self.test_json_file}")

        model = Model(str(self.test_inp_file))

        # Execute JSON configuration simulation
        inp_file, rpt_file, out_file = model.simulation_with_json(
            json_file=str(self.test_json_file),
            out_folder=str(self.temp_output_dir),
            file_name="output",
        )

        # Verify output files are generated
        self.assertTrue(os.path.exists(inp_file))
        self.assertTrue(os.path.exists(rpt_file))
        self.assertTrue(os.path.exists(out_file))

        # Verify files are in the specified output directory
        self.assertTrue(str(self.temp_output_dir) in inp_file)
        
        # Verify output file names contain 'output' as specified
        from pathlib import Path
        self.assertIn("output", Path(inp_file).stem, "inp file name should contain 'output'")
        self.assertIn("output", Path(rpt_file).stem, "rpt file name should contain 'output'")
        self.assertIn("output", Path(out_file).stem, "out file name should contain 'output'")
    
    def test_generate_json_template(self):
        """
        Test JSON template generation functionality
        """
        model = Model(str(self.test_inp_file))
        
        # Generate JSON template
        template_file = str(self.temp_output_dir / "template.json")
        model.generate_json_template(template_file)
        
        # Verify template file is generated
        self.assertTrue(os.path.exists(template_file))
        
        # Verify file content is valid JSON
        import json
        with open(template_file, 'r', encoding='utf-8') as f:
            try:
                json_data = json.load(f)
                self.assertIsInstance(json_data, dict)
            except json.JSONDecodeError:
                self.fail("Generated JSON template format is invalid")
    
    def test_print_model_summary(self):
        """
        Test print model summary functionality
        """
        model = Model(str(self.test_inp_file))
        
        # Test if method can execute normally (without throwing exceptions)
        try:
            model.print_model_summary()
        except Exception as e:
            self.fail(f"print_model_summary method execution failed: {e}")
    
    def test_simulation_output_directory_creation(self):
        """
        Test automatic output directory creation during simulation
        """
        model = Model(str(self.test_inp_file))
        
        # Use non-existent directory path
        non_existent_dir = self.temp_output_dir / "new_directory"
        inp_file = str(non_existent_dir / "test.inp")
        rpt_file = str(non_existent_dir / "test.rpt")
        out_file = str(non_existent_dir / "test.out")
        
        # Execute simulation
        model.simulation(
            inp_file=inp_file,
            rpt_file=rpt_file,
            out_file=out_file,
        )
        
        # Verify directory was created
        self.assertTrue(non_existent_dir.exists())
        
        # Verify files are generated
        self.assertTrue(os.path.exists(inp_file))
        self.assertTrue(os.path.exists(rpt_file))
        self.assertTrue(os.path.exists(out_file))
    
    def test_simulation_with_json_date_config(self):
        """
        Test simulation with JSON configuration file containing date information
        """
        # Skip test if JSON configuration file does not exist
        if not self.test_json_with_date_file.exists():
            self.skipTest(f"JSON configuration file with date does not exist: {self.test_json_with_date_file}")
        
        model = Model(str(self.test_inp_file))
        
        # Execute JSON configuration simulation with date
        inp_file, rpt_file, out_file = model.simulation_with_json(
            json_file=str(self.test_json_with_date_file),
            out_folder=str(self.temp_output_dir),
        )
        
        # Verify output files are generated
        self.assertTrue(os.path.exists(inp_file))
        self.assertTrue(os.path.exists(rpt_file))
        self.assertTrue(os.path.exists(out_file))
        
        # Verify files are in the specified output directory
        self.assertTrue(str(self.temp_output_dir) in inp_file)

        # Load the generated inp file and verify rain configuration
        generated_model = Model(inp_file)

        # Verify rain time series configuration
        self.assertGreater(len(generated_model.rain.ts_list), 0, "Rain time series list should not be empty")

        first_ts = generated_model.rain.ts_list[0]
        self.assertTrue(hasattr(first_ts, 'has_date'), "Rain time series should have has_date attribute")
        self.assertTrue(hasattr(first_ts, 'start_datetime'), "Rain time series should have start_datetime attribute")

        # Verify has_date is True for date configuration
        self.assertTrue(first_ts.has_date, "Rain time series should have has_date=True for date configuration")

        # Verify specific start_datetime value
        from datetime import datetime
        expected_datetime = datetime(2025, 1, 1, 23, 0)
        self.assertEqual(first_ts.start_datetime, expected_datetime,
                         "Rain time series start_datetime should be 2025-01-01 23:00")
    
    def test_model_with_different_rain_data(self):
        """
        Test model functionality with different rain data configurations
        """
        model = Model(str(self.test_inp_file))
        
        # Test with both available JSON configurations
        json_files = [self.test_json_file, self.test_json_with_date_file]
        
        for json_file in json_files:
            if json_file.exists():
                with self.subTest(json_config=json_file.name):
                    # Execute simulation with different JSON configs
                    inp_file, rpt_file, out_file = model.simulation_with_json(
                        json_file=str(json_file),
                        out_folder=str(self.temp_output_dir / json_file.stem)
                    )
                    
                    # Verify output files are generated
                    self.assertTrue(os.path.exists(inp_file))
                    self.assertTrue(os.path.exists(rpt_file))
                    self.assertTrue(os.path.exists(out_file))
    
    def test_model_file_validation(self):
        """
        Test validation of model input files
        """
        # Test with valid inp file
        self.assertTrue(self.test_inp_file.exists(), "Test inp file should exist")
        
        # Test file size (should not be empty)
        self.assertGreater(self.test_inp_file.stat().st_size, 0, "Test inp file should not be empty")
        
        # Test JSON configuration files
        if self.test_json_file.exists():
            import json
            with open(self.test_json_file, 'r', encoding='utf-8') as f:
                try:
                    json_data = json.load(f)
                    self.assertIsInstance(json_data, dict, "JSON file should contain valid JSON object")
                    # Check for required sections
                    self.assertIn('rain', json_data, "JSON should contain 'rain' section")
                    self.assertIn('calc', json_data, "JSON should contain 'calc' section")
                except json.JSONDecodeError:
                    self.fail("JSON configuration file is not valid JSON")
    
    def test_generate_and_use_json_template(self):
        """
        Test generating JSON template and then using it for simulation
        """
        model = Model(str(self.test_inp_file))
        
        # Generate JSON template file
        template_file = str(self.temp_output_dir / "temp.json")
        model.generate_json_template(template_file)
        
        # Verify template file is generated
        self.assertTrue(os.path.exists(template_file), "JSON template file should be generated")
        
        # Verify template file content is valid JSON
        import json
        with open(template_file, 'r', encoding='utf-8') as f:
            try:
                json_data = json.load(f)
                self.assertIsInstance(json_data, dict, "Generated JSON template should be a dictionary")
            except json.JSONDecodeError:
                self.fail("Generated JSON template format is invalid")
        
        # Use the generated JSON template for simulation
        output_folder = str(self.temp_output_dir / "out")
        inp_file, rpt_file, out_file = model.simulation_with_json(
            json_file=template_file,
            out_folder=output_folder,
        )
        
        # Verify simulation output files are generated
        self.assertTrue(os.path.exists(inp_file), f"inp file not generated: {inp_file}")
        self.assertTrue(os.path.exists(rpt_file), f"rpt file not generated: {rpt_file}")
        self.assertTrue(os.path.exists(out_file), f"out file not generated: {out_file}")
        
        # Verify files are in the specified output directory
        self.assertTrue(output_folder in inp_file, "inp file should be in specified output folder")
        self.assertTrue(output_folder in rpt_file, "rpt file should be in specified output folder")
        self.assertTrue(output_folder in out_file, "out file should be in specified output folder")
        

    def test_simulation_with_flexible_ponding_solver(self):
        """
        Test simulation using FlexiblePondingSolverAPI solver instance
        """
        model = Model(str(self.test_inp_file))
        model.calc.allow_ponding = True
        model.calc.flow_routing_method = "DYNWAVE"
        
        # Specify output file paths
        inp_file = str(self.temp_output_dir / "test_flexible_ponding.inp")
        rpt_file = str(self.temp_output_dir / "test_flexible_ponding.rpt")
        out_file = str(self.temp_output_dir / "test_flexible_ponding.out")
        
        # Execute simulation using FlexiblePondingSolverAPI
        # Create a FlexiblePondingSolverAPI instance with the model
        solver_instance = FlexiblePondingSolverAPI(model)
        result_inp, result_rpt, result_out = model.simulation(
            inp_file=inp_file,
            rpt_file=rpt_file,
            out_file=out_file,
            solver=solver_instance
        )
        
        # Verify returned file paths
        self.assertEqual(result_inp, inp_file)
        self.assertEqual(result_rpt, rpt_file)
        self.assertEqual(result_out, out_file)
        
        # Verify output files are generated
        self.assertTrue(os.path.exists(inp_file))
        self.assertTrue(os.path.exists(rpt_file))
        self.assertTrue(os.path.exists(out_file))
    
    def test_simulation_with_json_and_flexible_ponding_solver(self):
        """
        Test simulation using JSON configuration file and FlexiblePondingSolverAPI solver instance
        """
        # Skip test if JSON configuration file does not exist
        if not self.test_json_file.exists():
            self.skipTest(f"JSON configuration file does not exist: {self.test_json_file}")
        
        model = Model(str(self.test_inp_file))
        model.calc.allow_ponding = True
        model.calc.flow_routing_method = "DYNWAVE"
        
        # Execute simulation using JSON configuration and FlexiblePondingSolverAPI
        # Create a FlexiblePondingSolverAPI instance with the model
        solver_instance = FlexiblePondingSolverAPI(model)
        inp_file, rpt_file, out_file = model.simulation_with_json(
            json_file=str(self.test_json_file),
            out_folder=str(self.temp_output_dir / "flexible_ponding"),
            solver=solver_instance
        )
        
        # Verify output files are generated
        self.assertTrue(os.path.exists(inp_file))
        self.assertTrue(os.path.exists(rpt_file))
        self.assertTrue(os.path.exists(out_file))
        
        # Verify files are in the specified output directory
        self.assertTrue(str(self.temp_output_dir) in inp_file)
    
    def test_simulation_with_json_date_and_flexible_ponding_solver(self):
        """
        Test simulation using JSON configuration file with date and FlexiblePondingSolverAPI solver instance
        """
        # Skip test if JSON configuration file with date does not exist
        if not self.test_json_with_date_file.exists():
            self.skipTest(f"JSON configuration file with date does not exist: {self.test_json_with_date_file}")
        
        model = Model(str(self.test_inp_file))
        model.calc.allow_ponding = True
        model.calc.flow_routing_method = "DYNWAVE"
        
        # Execute simulation using JSON configuration with date and FlexiblePondingSolverAPI
        # Create a FlexiblePondingSolverAPI instance with the model
        solver_instance = FlexiblePondingSolverAPI(model)
        inp_file, rpt_file, out_file = model.simulation_with_json(
            json_file=str(self.test_json_with_date_file),
            out_folder=str(self.temp_output_dir / "flexible_ponding_with_date"),
            solver=solver_instance
        )
        
        # Verify output files are generated
        self.assertTrue(os.path.exists(inp_file))
        self.assertTrue(os.path.exists(rpt_file))
        self.assertTrue(os.path.exists(out_file))
        
        # Verify files are in the specified output directory
        self.assertTrue(str(self.temp_output_dir) in inp_file)
        
        # Load the generated inp file and verify rain configuration
        generated_model = Model(inp_file)

        # Verify rain time series configuration
        self.assertGreater(len(generated_model.rain.ts_list), 0, "Rain time series list should not be empty")

        first_ts = generated_model.rain.ts_list[0]
        self.assertTrue(hasattr(first_ts, 'has_date'), "Rain time series should have has_date attribute")
        self.assertTrue(hasattr(first_ts, 'start_datetime'), "Rain time series should have start_datetime attribute")

        # Verify has_date is True for date configuration
        self.assertTrue(first_ts.has_date, "Rain time series should have has_date=True for date configuration")

        # Verify specific start_datetime value
        from datetime import datetime
        expected_datetime = datetime(2025, 1, 1, 23, 0)
        self.assertEqual(first_ts.start_datetime, expected_datetime,
                         "Rain time series start_datetime should be 2025-01-01 23:00")

    def test_simulation_with_json_and_custom_file_name_flexible_ponding(self):
        """
        Test simulation using JSON configuration file, custom file name and FlexiblePondingSolverAPI solver instance
        """
        # Skip test if JSON configuration file does not exist
        if not self.test_json_file.exists():
            self.skipTest(f"JSON configuration file does not exist: {self.test_json_file}")

        model = Model(str(self.test_inp_file))
        model.calc.allow_ponding = True
        model.calc.flow_routing_method = "DYNWAVE"

        # Execute simulation using JSON configuration, custom file name and FlexiblePondingSolverAPI
        # Create a FlexiblePondingSolverAPI instance with the model
        solver_instance = FlexiblePondingSolverAPI(model)
        inp_file, rpt_file, out_file = model.simulation_with_json(
            json_file=str(self.test_json_file),
            out_folder=str(self.temp_output_dir / "flexible_ponding_custom"),
            file_name="flexible_ponding_output",
            solver=solver_instance
        )

        # Verify output files are generated
        self.assertTrue(os.path.exists(inp_file))
        self.assertTrue(os.path.exists(rpt_file))
        self.assertTrue(os.path.exists(out_file))

        # Verify files are in the specified output directory
        self.assertTrue(str(self.temp_output_dir) in inp_file)
        
        # Verify output file names contain specified 'flexible_ponding_output'
        from pathlib import Path
        self.assertIn("flexible_ponding_output", Path(inp_file).stem, "inp file name should contain 'flexible_ponding_output'")
        self.assertIn("flexible_ponding_output", Path(rpt_file).stem, "rpt file name should contain 'flexible_ponding_output'")
        self.assertIn("flexible_ponding_output", Path(out_file).stem, "out file name should contain 'flexible_ponding_output'")


if __name__ == '__main__':
    unittest.main()
