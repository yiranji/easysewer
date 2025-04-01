"""
Rainfall Data Management Module

This module handles rainfall data input and processing, including rain gages,
time series data, and rainfall patterns for the drainage model.
"""

from .utils import *


class NamedList:
    """A list-like collection that allows access by index or name.
    
    This class implements common list methods and adds the ability to access items
    by their name attribute.
    
    Attributes:
        data (list): The underlying list of items
    """
    
    def __init__(self, data=None):
        self.data = data if data is not None else []
    
    def __len__(self):
        return len(self.data)
    
    def __getitem__(self, key):
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
    
    def __contains__(self, item):
        return item in self.data
    
    def append(self, item):
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


def parse_swmm_datetime(date_str=None, time_str=None):
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
    dt = datetime.strptime(f"{date_str} {time_str}", "%m/%d/%Y %H:%M")
    return dt.hour * 60 + dt.minute


class TimeSeries:
    def __init__(self):
        self.name = ''
        self.time = []  # in minutes
        self.value = []  # in mm
        self.has_date = False  # whether the timeseries includes date information
        self.start_date = None  # store the start date if available

    def __repr__(self):
        if len(self.time) == 0:
            return 'None'
        else:
            interval = self.time[1] - self.time[0]
            total = sum([(v * interval / 60) for v in self.value])
            total = round(total, 2)
            return f'{self.name}: {self.time[-1]}min - {total}mm'


class RainGage:
    """
    Represents a rain gage station in the model.
    
    Defines characteristics of rainfall measurement points including data format,
    time intervals, and data source.

    Attributes:
        name (str): Unique identifier for the rain gage
        format (str): Format of the rainfall data (INTENSITY/VOLUME/CUMULATIVE)
        interval (float): Recording time interval
        snow_catch (float): Snow catch deficiency correction
        data_source (str): Source of the rainfall data
        time_series (str): Name of associated time series data
    """
    def __init__(self):
        self.name = ''
        self.form = ''  # INTENSIFY: mm/h
        self.interval = ''
        self.SCF = 1  # snow catch deficiency correction factor (use 1.0 for no adjustment)
        self.source = ''  # timeseries name

    def __repr__(self):
        return f'RainGage<{self.name}>: {self.source}'


class Rain:
    """
    Container class for managing rainfall data.
    
    Manages collection of rain gages and their associated time series data
    for the drainage model.

    Attributes:
        gage_list (list): Collection of RainGage objects
        ts_list (list): Collection of time series data
    """
    def __init__(self):
        self.ts_list = TimeSeriesList()
        self.gage_list = RainGageList()

    def __repr__(self):
        if len(self.gage_list) == 0:
            return 'None'
        elif len(self.gage_list) == 1:
            return f'{self.gage_list[0]}'
        else:
            return 'Gages'

    def add_ts(self, new_ts):
        self.ts_list.append(new_ts)

    def add_gage(self, new_gage):
        self.gage_list.append(new_gage)

    def read_from_swmm_inp(self, filename):
        from datetime import datetime

        content = get_swmm_inp_content(filename, '[TIMESERIES]')
        this_timeseries = TimeSeries()
        this_timeseries.name = 'initial'

        for line in content:
            parts = line.split()

            # Skip empty lines or invalid formats
            if len(parts) < 3:
                continue

            # Determine if this line has date information
            has_date = len(parts) == 4

            if has_date:
                name, date, time, value = parts
                minutes = parse_swmm_datetime(date_str=date, time_str=time)
            else:
                name, time, value = parts
                minutes = parse_swmm_datetime(time_str=time)

            value = float(value)

            # Handle first timeseries
            if this_timeseries.name == 'initial':
                this_timeseries.name = name
                this_timeseries.has_date = has_date
                if has_date:
                    this_timeseries.start_date = datetime.strptime(date, "%m/%d/%Y").date()

            # If we encounter a new timeseries name
            if this_timeseries.name != name:
                # Save the current timeseries
                self.add_ts(this_timeseries)

                # Start a new timeseries
                this_timeseries = TimeSeries()
                this_timeseries.name = name
                this_timeseries.has_date = has_date
                if has_date:
                    this_timeseries.start_date = datetime.strptime(date, "%m/%d/%Y").date()

            # Add the data point
            this_timeseries.time.append(minutes)
            this_timeseries.value.append(value)

        # Add the last timeseries if it exists
        if this_timeseries.name != 'initial':
            self.add_ts(this_timeseries)
        # rain gage section
        content = get_swmm_inp_content(filename, '[RAINGAGES]')
        for line in content:
            name, form, interval, SCF, _, tise = line.split()
            this_gage = RainGage()
            this_gage.name = name
            this_gage.form = form
            this_gage.interval = interval
            this_gage.SCF = SCF
            this_gage.source = tise
            self.add_gage(this_gage)
        return 0

    def write_to_swmm_inp(self, filename):
        from datetime import datetime, timedelta

        def time_minute2text(minutes):
            minutes = int(minutes)
            hours, left = divmod(minutes, 60)
            text = f'{hours}:{left:02}'
            return text

        def get_date_for_minutes(start_date, minutes):
            """Convert minutes to date string, handling day rollovers"""
            days, remaining_minutes = divmod(minutes, 24 * 60)
            date = start_date + timedelta(days=days)
            return date.strftime("%m/%d/%Y")

        with open(filename, 'a', encoding='utf-8') as f:
            f.write('\n\n[TIMESERIES]\n')
            if any(ts.has_date for ts in self.ts_list):
                f.write(';;Name           Date       Time       Value\n')
                f.write(';;-------------- ---------- ---------- ----------\n')
            else:
                f.write(';;Name       Time       Value\n')
                f.write(';;---------- ---------- ----------\n')

            for ts in self.ts_list:
                for time, value in zip(ts.time, ts.value):
                    if ts.has_date:
                        date_str = get_date_for_minutes(ts.start_date, time)
                        time_str = time_minute2text(time % (24 * 60))  # Get time within the day
                        f.write(f'{ts.name:<14} {date_str}  {time_str}  {value:>.2f}\n')
                    else:
                        f.write(f'{ts.name}  {time_minute2text(time)}  {value:>.2f}\n')
                f.write(';;\n')

            f.write('\n\n[RAINGAGES]\n')
            f.write(';;Name  Format   Interval  SCF  Source    \n')
            f.write(';;----- -------- --------- ---- ----------\n')
            for gage in self.gage_list:
                f.write(f'{gage.name}  {gage.form}  {gage.interval}  {gage.SCF}  TIMESERIES  {gage.source}\n')
            return 0
