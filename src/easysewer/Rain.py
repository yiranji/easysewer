"""
Rainfall Data Management Module

This module handles rainfall data input and processing, including rain gages,
time series data, and rainfall patterns for the drainage model.
"""
from typing import List, Optional, Union
from datetime import datetime
from pathlib import Path
from warnings import warn
import os
import shlex
from .utils import *


class NamedList:
    """A list-like collection that allows access by index or name.
    
    This class implements common list methods and adds the ability to access items
    by their name attribute.
    
    Attributes:
        data (List): The underlying list of items
    """
    
    def __init__(self, data: Optional[List] = None) -> None:
        self.data: List = data if data is not None else []
    
    def __len__(self) -> int:
        return len(self.data)
    
    def __getitem__(self, key: Union[int, str]):
        if isinstance(key, int):
            return self.data[key]
        elif isinstance(key, str):
            for item in self.data:
                if item.name == key:
                    return item
            raise KeyError(f"No item found with name '{key}'")
        else:
            raise TypeError("Key must be an integer or a string")
    
    def __iter__(self):
        return iter(self.data)
    
    def __contains__(self, item) -> bool:
        return item in self.data
    
    def append(self, item) -> None:
        """Add an item to the collection.
        
        Args:
            item: The item to add
        """
        self.data.append(item)


class TimeSeriesList(NamedList):
    """A specialized collection for TimeSeries objects.
    
    Inherits all functionality from NamedList and may add TimeSeries-specific
    methods in the future.
    """
    pass


class RainGageList(NamedList):
    """A specialized collection for RainGage objects.
    
    Inherits all functionality from NamedList and may add RainGage-specific
    methods in the future.
    """
    pass


def parse_swmm_datetime(date_str: Optional[str] = None, time_str: Optional[str] = None) -> int:
    """Convert SWMM date and time strings to minutes since start of day

    Args:
        date_str: Optional date string in format 'MM/DD/YYYY'
        time_str: Time string in format 'HH:MM' or 'H:MM'

    Returns:
        minutes: Integer minutes since start of day
    """
    if date_str is None:
        # Just parse time when no date provided
        hours, minutes = time_str.split(':')
        return int(minutes) + 60 * int(hours)

    # Parse date and time when both provided
    from datetime import datetime
    
    dt_str = f"{date_str} {time_str}"
    for fmt in ["%m/%d/%Y %H:%M", "%m/%d/%Y %H:%M:%S"]:
        try:
            dt = datetime.strptime(dt_str, fmt)
            return dt.hour * 60 + dt.minute
        except ValueError:
            continue
            
    # If all fails, raise error with the last attempted format or a generic message
    raise ValueError(f"Time data '{dt_str}' does not match any supported format")


class TimeSeries:
    """Time series data container for rainfall measurements
    
    Attributes:
        name (str): Identifier for the time series
        time (List[int]): Time values in minutes
        value (List[float]): Rainfall values in mm
        has_date (bool): Whether the timeseries includes date information
        start_datetime (Optional[datetime]): Store the start datetime if available
        source_type (str): INLINE data or an external FILE reference
        file_path (Optional[str]): External file path, relative to the input INP
    """
    
    def __init__(self) -> None:
        self.name: str = ''
        self.time: List[int] = []  # in minutes
        self.value: List[float] = []  # in mm
        self.has_date: bool = False  # whether the timeseries includes date information
        self.start_datetime: Optional[datetime] = None  # store the start datetime if available
        self.source_type: str = 'INLINE'
        self.file_path: Optional[str] = None
        self._source_directory: Optional[Path] = None

    def __repr__(self) -> str:
        if self.source_type == 'FILE':
            return f'{self.name}: FILE "{self.file_path}"'
        elif len(self.time) == 0:
            return 'None'
        else:
            interval = self.time[1] - self.time[0]
            total = sum([(v * interval / 60) for v in self.value])
            total = round(total, 2)
            return f'{self.name}: {self.time[-1]}min - {total}mm'

    def _file_path_for_output(self, filename: str) -> str:
        """Keep external references pointing to the same file after Save As."""
        if not self.file_path:
            raise ValueError(f'Time series {self.name!r} has no external file path')
        path = Path(self.file_path)
        output_directory = Path(filename).resolve().parent
        if (path.is_absolute() or self._source_directory is None
                or output_directory == self._source_directory):
            return self.file_path
        target = (self._source_directory / path).resolve()
        try:
            return os.path.relpath(target, output_directory)
        except ValueError:
            # Windows cannot express a relative path between different drives.
            return str(target)


class RainGage:
    """
    Represents a rain gage station in the model.
    
    Defines characteristics of rainfall measurement points including data format,
    time intervals, and data source.

    Attributes:
        name (str): Unique identifier for the rain gage
        form (str): Format of the rainfall data (INTENSITY/VOLUME/CUMULATIVE)
        interval (Union[str, float]): Recording time interval
        SCF (Union[int, float]): Snow catch deficiency correction factor
        source (str): Timeseries name or file name
        source_type (str): Source type (TIMESERIES or FILE)
        station_id (Optional[str]): Station ID for FILE source
        unit (Optional[str]): Unit for FILE source (e.g., mm)
    """
    
    def __init__(self) -> None:
        self.name: str = ''
        self.form: str = ''  # INTENSIFY: mm/h
        self.interval: Union[str, float] = ''
        self.SCF: Union[int, float] = 1  # snow catch deficiency correction factor (use 1.0 for no adjustment)
        self.source: str = ''  # timeseries name or file name
        self.source_type: str = 'TIMESERIES'  # TIMESERIES or FILE
        self.station_id: Optional[str] = None  # Only for FILE source
        self.unit: Optional[str] = None  # Only for FILE source

    def __repr__(self) -> str:
        return f'RainGage<{self.name}>: {self.source} ({self.source_type})'


class Rain:
    """
    Container class for managing rainfall data.
    
    Manages collection of rain gages and their associated time series data
    for the drainage model.

    Attributes:
        ts_list (TimeSeriesList): Collection of TimeSeries objects
        gage_list (RainGageList): Collection of RainGage objects
    """
    
    def __init__(self) -> None:
        self.ts_list: TimeSeriesList = TimeSeriesList()
        self.gage_list: RainGageList = RainGageList()

    def __repr__(self) -> str:
        if len(self.gage_list) == 0:
            return 'None'
        elif len(self.gage_list) == 1:
            return f'{self.gage_list[0]}'
        else:
            return 'Gages'

    def add_ts(self, new_ts: TimeSeries) -> None:
        """Add a TimeSeries object to the collection
        
        Args:
            new_ts: TimeSeries object to add
        """
        self.ts_list.append(new_ts)

    def add_gage(self, new_gage: RainGage) -> None:
        """Add a RainGage object to the collection
        
        Args:
            new_gage: RainGage object to add
        """
        self.gage_list.append(new_gage)

    def read_from_swmm_inp(self, filename: str) -> int:
        """Read rainfall data from SWMM input file
        
        Args:
            filename: Path to the SWMM input file
            
        Returns:
            0 on success
        """
        from datetime import datetime

        content = get_swmm_inp_content(filename, '[TIMESERIES]')
        this_timeseries = TimeSeries()
        this_timeseries.name = 'initial'

        for line in content:
            # Non-POSIX mode preserves Windows backslashes and quoted paths.
            lexer = shlex.shlex(line, posix=False)
            lexer.whitespace_split = True
            lexer.commenters = ';'
            lexer.quotes = '"'
            parts = list(lexer)

            if len(parts) >= 2 and parts[1].upper() == 'FILE':
                if len(parts) != 3 or not parts[2].strip('"'):
                    raise ValueError(f'Invalid TIMESERIES FILE reference: {line!r}')
                if this_timeseries.name != 'initial':
                    self.add_ts(this_timeseries)
                external_timeseries = TimeSeries()
                external_timeseries.name = parts[0]
                external_timeseries.source_type = 'FILE'
                external_timeseries.file_path = parts[2].strip('"')
                external_timeseries._source_directory = Path(filename).resolve().parent
                self.add_ts(external_timeseries)
                this_timeseries = TimeSeries()
                this_timeseries.name = 'initial'
                continue

            # Skip empty lines or invalid formats
            if len(parts) < 3:
                continue

            # Determine if this line has date information
            has_date = len(parts) == 4

            if has_date:
                name, date, time, value = parts
                dt_str = f"{date} {time}"
                current_datetime = None
                
                # Define supported formats in priority order
                formats = ["%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M"]
                
                for fmt in formats:
                    try:
                        current_datetime = datetime.strptime(dt_str, fmt)
                        break
                    except ValueError:
                        continue
                
                if current_datetime is None:
                    raise ValueError(f"Time data '{dt_str}' does not match any supported formats: {formats}")
            else:
                name, time, value = parts
                minutes = parse_swmm_datetime(time_str=time)

            value = float(value)

            # Handle first timeseries
            if this_timeseries.name == 'initial':
                this_timeseries.name = name
                this_timeseries.has_date = has_date
                if has_date:
                    this_timeseries.start_datetime = current_datetime

            # If we encounter a new timeseries name
            if this_timeseries.name != name:
                # Save the current timeseries
                self.add_ts(this_timeseries)

                # Start a new timeseries
                this_timeseries = TimeSeries()
                this_timeseries.name = name
                this_timeseries.has_date = has_date
                if has_date:
                    this_timeseries.start_datetime = current_datetime

            # Add the data point
            if has_date:
                # Calculate minutes from start_datetime
                time_diff = current_datetime - this_timeseries.start_datetime
                minutes = int(time_diff.total_seconds() / 60)
            this_timeseries.time.append(minutes)
            this_timeseries.value.append(value)

        # Add the last timeseries if it exists
        if this_timeseries.name != 'initial':
            self.add_ts(this_timeseries)
        # rain gage section
        content = get_swmm_inp_content(filename, '[RAINGAGES]')
        for line in content:
            parts = line.split()
            if len(parts) < 6:
                continue  # skip malformed lines
            source_type = parts[4]
            if source_type == 'TIMESERIES' and len(parts) == 6:
                name, form, interval, SCF, source_type, tise = parts
                this_gage = RainGage()
                this_gage.name = name
                this_gage.form = form
                this_gage.interval = interval
                this_gage.SCF = SCF
                this_gage.source_type = source_type
                this_gage.source = tise
                # unit remains default
            elif source_type == 'FILE' and len(parts) == 8:
                name, form, interval, SCF, source_type, filepath, station_id, unit = parts
                this_gage = RainGage()
                this_gage.name = name
                this_gage.form = form
                this_gage.interval = interval
                this_gage.SCF = SCF
                this_gage.source_type = source_type
                this_gage.source = filepath
                this_gage.station_id = station_id
                this_gage.unit = unit
            else:
                warn(f'Failed to add rain gauge for content "{line}".')
                continue  # skip malformed lines
            self.add_gage(this_gage)
        return 0

    def write_to_swmm_inp(self, filename: str) -> int:
        """Write rainfall data to SWMM input file without rounding rainfall values
        
        Args:
            filename: Path to the output SWMM input file
            
        Returns:
            0 on success
        """
        from datetime import datetime, timedelta

        def time_minute2text(minutes):
            minutes = int(minutes)
            hours, left = divmod(minutes, 60)
            text = f'{hours}:{left:02}'
            return text

        def get_datetime_for_minutes(start_datetime, minutes):
            """Convert minutes to datetime, handling day rollovers"""
            target_datetime = start_datetime + timedelta(minutes=minutes)
            return target_datetime

        with open(filename, 'a', encoding='utf-8') as f:
            f.write('\n\n[TIMESERIES]\n')
            if any(ts.has_date for ts in self.ts_list):
                f.write(';;Name           Date       Time       Value\n')
                f.write(';;-------------- ---------- ---------- ----------\n')
            else:
                f.write(';;Name       Time       Value\n')
                f.write(';;---------- ---------- ----------\n')

            for ts in self.ts_list:
                if ts.source_type == 'FILE':
                    f.write(f'{ts.name} FILE "{ts._file_path_for_output(filename)}"\n')
                    continue
                for time, value in zip(ts.time, ts.value):
                    if ts.has_date:
                        target_datetime = get_datetime_for_minutes(ts.start_datetime, time)
                        date_str = target_datetime.strftime("%m/%d/%Y")
                        # Check if original data had seconds (by checking if any time component has seconds)
                        # For now, default to HH:MM unless we want to track precision
                        time_str = target_datetime.strftime("%H:%M") 
                        f.write(f'{ts.name:<14} {date_str}  {time_str}  {value}\n')
                    else:
                        f.write(f'{ts.name}  {time_minute2text(time)}  {value}\n')
                f.write(';;\n')

            f.write('\n\n[RAINGAGES]\n')
            f.write(';;Name  Format   Interval  SCF  SourceType  Source    [Unit]\n')
            f.write(';;----- -------- --------- ---- ----------  ---------- -------\n')
            for gage in self.gage_list:
                if gage.source_type == 'TIMESERIES':
                    f.write(f'{gage.name}  {gage.form}  {gage.interval}  {gage.SCF}  TIMESERIES  {gage.source}\n')
                elif gage.source_type == 'FILE':
                    f.write(f'{gage.name}  {gage.form}  {gage.interval}  {gage.SCF}  FILE  {gage.source}  {gage.station_id}  {gage.unit}\n')
                else:
                    # fallback for unknown type
                    raise ValueError(f"Unknown source type: {gage.source_type}")
            return 0
