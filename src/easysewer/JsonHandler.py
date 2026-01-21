"""JSON configuration handler for SWMM model

This module provides JSON-based configuration management for SWMM models,
including rain data processing, model configuration, and simulation setup.
"""

import os
import json
import logging
from typing import Optional, Dict, Any, List
import datetime
import csv
from .Rain import RainGage, TimeSeries

logger = logging.getLogger(__name__)


class JsonHandler:
    """
    Handler for JSON-based model configuration and rain data processing.
    
    This class provides utilities for loading JSON configurations,
    processing CSV rain data, and applying configurations to SWMM models.
    """
    
    @staticmethod
    def load_json_config(json_file: str) -> Dict[str, Any]:
        """
        Load JSON configuration file.
        
        Args:
            json_file: JSON file path
            
        Returns:
            Configuration dictionary
        """
        with open(json_file, 'r', encoding='utf-8') as f:
            config = json.load(f)
        
        # Handle CSV file path in rain configuration (if exists)
        if 'rain' in config and 'csv_file' in config['rain']:
            csv_file = config['rain']['csv_file']
            # If csv_file is empty string, remove the entire rain configuration
            if csv_file == "":
                del config['rain']
            elif csv_file and not os.path.isabs(csv_file):
                # If it's a relative path, relative to JSON file directory
                json_dir = os.path.dirname(json_file)
                full_csv_path = os.path.join(json_dir, csv_file)
                # Only update the path if we're not in a test scenario (file exists)
                if os.path.exists(full_csv_path):
                    config['rain']['csv_file'] = os.path.abspath(full_csv_path)
        
        return config
    
    @staticmethod
    def _clear_current_rain(model) -> None:
        """
        Clear all current rain settings from the model.
        
        Args:
            model: SWMM model instance
        """
        # Raingage
        model.rain.gage_list = []
        # Rain series
        model.rain.ts_list = []
        # Subcatchment raingage
        for area in model.area:
            area.rain_gage = None
    
    @staticmethod
    def _read_csv_data(source_file: str) -> Dict[str, List[str]]:
        """
        Read CSV file using built-in csv module.
        
        Args:
            source_file: Path to the CSV file
            
        Returns:
            Dictionary with column names as keys and lists of values
        """
        data = {}
        with open(source_file, 'r', encoding='utf-8-sig') as csvfile:  # Use utf-8-sig to handle BOM
            reader = csv.DictReader(csvfile)
            
            # Initialize columns
            for fieldname in reader.fieldnames:
                data[fieldname] = []
            
            # Read data
            for row in reader:
                for fieldname in reader.fieldnames:
                    data[fieldname].append(row[fieldname])
        
        return data
    
    @staticmethod
    def _parse_datetime_string(date_str: str, time_str: str) -> datetime.datetime:
        """
        Parse date and time strings to datetime object.
        
        Args:
            date_str: Date string (e.g., '2023-01-01' or '01/01/2023')
            time_str: Time string (e.g., '12:30' or '12:30:00')
            
        Returns:
            datetime object
        """
        # Try different date formats
        date_formats = ['%Y-%m-%d', '%m/%d/%Y', '%d/%m/%Y', '%Y/%m/%d']
        
        parsed_date = None
        for fmt in date_formats:
            try:
                parsed_date = datetime.datetime.strptime(date_str, fmt).date()
                break
            except ValueError:
                continue
        
        if parsed_date is None:
            raise ValueError(f"Unable to parse date: {date_str}")
        
        # Parse time
        time_parts = time_str.split(':')
        if len(time_parts) == 2:
            hours, minutes = map(int, time_parts)
            seconds = 0
        elif len(time_parts) == 3:
            hours, minutes, seconds = map(int, time_parts)
        else:
            raise ValueError(f"Unable to parse time: {time_str}")
        
        return datetime.datetime.combine(parsed_date, datetime.time(hours, minutes, seconds))
    
    @staticmethod
    def _load_rain_from_csv(model, source_file: str) -> None:
        """
        Add rain data from CSV file to the model.
        
        Args:
            model: SWMM model instance
            source_file: Path to the rain CSV file
        """
        rain_name_with_ext = os.path.basename(source_file)
        rain_name = os.path.splitext(rain_name_with_ext)[0]
        
        # Read CSV data using built-in csv module
        data = JsonHandler._read_csv_data(source_file)

        # Check if Date column exists
        has_date = 'Date' in data

        # Parse time strings to minutes
        def parse_time_to_minutes(time_str: str, date_str: Optional[str] = None) -> int:
            try:
                if date_str:
                    # If we have a date, parse it and add to total minutes
                    date_time = JsonHandler._parse_datetime_string(date_str, time_str)
                    # Get minutes since start of first day
                    if not hasattr(parse_time_to_minutes, 'start_date'):
                        parse_time_to_minutes.start_date = JsonHandler._parse_datetime_string(data['Date'][0], data['Time'][0])
                    delta = date_time - parse_time_to_minutes.start_date
                    return int(delta.total_seconds() / 60)
                else:
                    # Original time-only parsing
                    hours, minutes = map(int, time_str.split(':'))
                    return hours * 60 + minutes
            except Exception as e:
                logger.warning(f"Error parsing time: {e}")
                return 0

        # Extract time values based on whether Date column exists
        if has_date:
            time_list = [parse_time_to_minutes(t, d) for t, d in zip(data['Time'], data['Date'])]
        else:
            time_list = [parse_time_to_minutes(t) for t in data['Time']]

        # Convert string values to float
        rainfall_list = [float(value) for value in data['Value']]

        # Create new rain gage and time series
        new_gage = RainGage()
        new_ts = TimeSeries()

        # Set up new time series
        new_ts.name = rain_name
        new_ts.time = time_list
        new_ts.value = rainfall_list
        new_ts.has_date = has_date
        
        # Set start_datetime if date information is available
        if has_date and data['Date']:
            first_date_str = data['Date'][0]
            try:
                # Parse the first date and set as start_datetime
                first_datetime = JsonHandler._parse_datetime_string(first_date_str, data['Time'][0])
                new_ts.start_datetime = first_datetime
            except Exception as e:
                logger.warning(f"Unable to parse start datetime '{first_date_str}': {e}")
                new_ts.start_datetime = None
        else:
            new_ts.start_datetime = None
        
        # Set up new gage
        new_gage.name = f'rg-{rain_name}'
        interval = time_list[1] - time_list[0]
        new_gage.interval = f"{interval // 60:02d}:{interval % 60:02d}"
        new_gage.SCF = 1  # snow catch deficiency correction factor (use 1.0 for no adjustment)
        new_gage.source = rain_name  # timeseries name

        # Add to model
        model.rain.add_gage(new_gage)
        model.rain.add_ts(new_ts)

        # Assign rain gage to all subcatchments
        for area in model.area:
            area.rain_gage = new_gage.name
    
    @staticmethod
    def _add_hours_to_datetime(start_date: Dict[str, int], hours_to_add: int) -> Dict[str, int]:
        """
        Add hours to a date dictionary.
        
        Args:
            start_date: Dictionary with year, month, day, hour, minute keys
            hours_to_add: Number of hours to add
            
        Returns:
            New date dictionary with added hours
        """
        # Convert start time to datetime
        dt = datetime.datetime(
            year=start_date["year"],
            month=start_date["month"],
            day=start_date["day"],
            hour=start_date["hour"],
            minute=start_date["minute"]
        )

        # Add hours
        dt += datetime.timedelta(hours=hours_to_add)

        # Convert to dict
        end_date = {
            "year": dt.year,
            "month": dt.month,
            "day": dt.day,
            "hour": dt.hour,
            "minute": dt.minute
        }

        return end_date
    
    @staticmethod
    def _apply_calc_config(model, calc_config: Dict[str, Any]) -> None:
        """
        Apply calc configuration to model.
        
        Args:
            model: SWMM model instance
            calc_config: calc configuration dictionary
        """
        if not calc_config or not hasattr(model, 'calc'):
            return
        
        # Dynamically set calc object attributes
        for attr_name, attr_value in calc_config.items():
            if hasattr(model.calc, attr_name):
                try:
                    setattr(model.calc, attr_name, attr_value)
                except Exception as e:
                    logger.warning(f"Unable to set calc.{attr_name} = {attr_value}, error: {e}")
            else:
                logger.warning(f"calc object does not have attribute '{attr_name}'")
    
    @staticmethod
    def _apply_rain_config(model, rain_config: Dict[str, Any]) -> None:
        """
        Apply rain configuration to model.
        
        Args:
            model: SWMM model instance
            rain_config: rain configuration dictionary
        """
        if not rain_config:
            return
        
        # Validate required fields
        if 'csv_file' in rain_config:
            csv_file = rain_config['csv_file']
            if not os.path.exists(csv_file):
                raise FileNotFoundError(f"CSV file does not exist: {csv_file}")

        # 1) Clear existing rainfall and rain gage
        JsonHandler._clear_current_rain(model)

        # 2) Add new rainfall and rain gage
        JsonHandler._load_rain_from_csv(model, rain_config['csv_file'])

        # Set rain form
        rain_form = rain_config.get('form', 'INTENSITY')
        if hasattr(model, 'rain') and hasattr(model.rain, 'gage_list'):
            for gage in model.rain.gage_list:
                gage.form = rain_form

        # Set simulation end time (based on duration)
        if 'simulation_duration_hour' in rain_config:
            duration_hours = rain_config['simulation_duration_hour']
            if hasattr(model, 'calc') and hasattr(model.calc, 'simulation_start'):
                model.calc.simulation_end = JsonHandler._add_hours_to_datetime(
                    model.calc.simulation_start, duration_hours
                )

        # Set labels
        if hasattr(model, 'label'):
            if 'original_name' in rain_config:
                model.label['rain_name'] = rain_config['original_name']
            if 'return_period' in rain_config:
                model.label['return_period'] = rain_config['return_period']
    
    @staticmethod
    def apply_json_config(model, config: Dict[str, Any]) -> None:
        """
        Apply JSON configuration to model.
        
        Args:
            model: SWMM model instance
            config: Configuration dictionary
        """
        # Apply calc configuration
        calc_config = config.get('calc', {})
        JsonHandler._apply_calc_config(model, calc_config)
        
        # Apply rain configuration
        rain_config = config.get('rain', {})
        JsonHandler._apply_rain_config(model, rain_config)
    
    @staticmethod
    def generate_json_template(model, output_path: str) -> None:
        """
        Generate example JSON file containing all configurable values of current model.
        
        Args:
            model: SWMM model instance
            output_path: Output JSON file path
        """
        # Dynamically get all attributes of calc object
        calc_dict = {}
        if hasattr(model, 'calc'):
            for attr_name in dir(model.calc):
                # Skip private attributes and methods
                if not attr_name.startswith('_') and not callable(getattr(model.calc, attr_name)):
                    try:
                        attr_value = getattr(model.calc, attr_name)
                        calc_dict[attr_name] = attr_value
                    except Exception:
                        # Skip this attribute if exception occurs when getting attribute value
                        continue
        
        template = {
            "rain": {
                "csv_file": "",  # User needs to fill in
                "original_name": getattr(model.label, 'rain', "") if hasattr(model, 'label') else "",
                "return_period": getattr(model.label, 'RP', "") if hasattr(model, 'label') else "",
                "form": "INTENSITY",  # Default value
                "simulation_duration_hour": 24  # Default value
            },
            "calc": calc_dict
        }
        
        # Remove fields with None values to keep JSON file concise
        def remove_none_values(d):
            if isinstance(d, dict):
                return {k: remove_none_values(v) for k, v in d.items() if v is not None}
            return d
        
        clean_template = remove_none_values(template)
        
        # Write JSON file
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(clean_template, f, indent=2, ensure_ascii=False)
        
        logger.info(f"JSON template file generated: {output_path}")
