"""
Subcatchment Area Management Module

This module handles subcatchment areas in the drainage network, including their
physical characteristics, infiltration parameters, and routing behavior.
"""
import logging
from .utils import *

logger = logging.getLogger(__name__)


class InfiltrationHorton:
    """
    Horton infiltration model parameters.
    
    Represents the Horton infiltration method which models infiltration rate
    decay from a maximum to minimum value over time.
    
    Attributes:
        maximum_rate (float): Maximum infiltration rate (mm/h)
        minimum_rate (float): Minimum infiltration rate (mm/h)
        decay_rate (float): Rate of decay from max to min (1/h)
        dry_time (float): Time needed for soil to fully dry (days)
        maximum_infiltration_volume (float): Maximum infiltration volume (mm), 0 if not applicable
    """
    # Default parameters for Horton model
    MAX_RATE_DEFAULT = 50  # mm/h
    MIN_RATE_DEFAULT = 5  # mm/h
    DECAY_RATE_DEFAULT = 5  # 1/h
    DRY_TIME_DEFAULT = 7  # days
    MAX_INFIL_VOLUME_DEFAULT = 0  # mm

    def __init__(self):
        self.maximum_rate = self.MAX_RATE_DEFAULT
        self.minimum_rate = self.MIN_RATE_DEFAULT
        self.decay_rate = self.DECAY_RATE_DEFAULT
        self.dry_time = self.DRY_TIME_DEFAULT
        self.maximum_infiltration_volume = self.MAX_INFIL_VOLUME_DEFAULT


class InfiltrationGreenAmpt:
    """
    Green-Ampt infiltration model parameters.
    
    Represents the Green-Ampt infiltration method which models infiltration based on
    soil physics and hydraulic principles.
    
    Attributes:
        soil_capillary_suction (float): Soil capillary suction head (mm)
        soil_saturated_hydraulic_conductivity (float): Saturated hydraulic conductivity (mm/h)
        initial_soil_moisture_deficit (float): Initial soil moisture deficit (fraction)
    """
    # Default parameters for sandy loam soil
    CAPILLARY_SUCTION_DEFAULT = 110.0  # mm
    HYDRAULIC_CONDUCTIVITY_DEFAULT = 10.0  # mm/h
    MOISTURE_DEFICIT_DEFAULT = 0.3  # fraction

    def __init__(self):
        # Default values for sandy loam soil
        self.soil_capillary_suction = self.CAPILLARY_SUCTION_DEFAULT
        self.soil_saturated_hydraulic_conductivity = self.HYDRAULIC_CONDUCTIVITY_DEFAULT
        self.initial_soil_moisture_deficit = self.MOISTURE_DEFICIT_DEFAULT


class InfiltrationCurveNumber:
    """
    SCS Curve Number infiltration model parameters.
    
    Represents the SCS Curve Number method which models infiltration based on
    soil classification and land use characteristics.
    
    Attributes:
        curve_number (float): SCS curve number
        dry_time (float): Time for fully saturated soil to completely dry (days)
        soil_saturated_hydraulic_conductivity (float): Saturated hydraulic conductivity (mm/h)
    """
    # Default parameters for HSG-B soil
    CURVE_NUMBER_DEFAULT = 79
    CN_DRY_TIME_DEFAULT = 7  # days
    CN_HYDRAULIC_CONDUCTIVITY_DEFAULT = 8.0  # mm/h

    def __init__(self):
        # Default values for open space (poor condition)
        self.curve_number = self.CURVE_NUMBER_DEFAULT
        self.dry_time = self.CN_DRY_TIME_DEFAULT
        self.soil_saturated_hydraulic_conductivity = self.CN_HYDRAULIC_CONDUCTIVITY_DEFAULT


class Infiltration:
    """
    Container for different infiltration model parameters.
    
    Holds instances of all supported infiltration models, allowing the appropriate
    model to be selected and used based on simulation requirements.
    
    Attributes:
        horton (InfiltrationHorton): Horton infiltration model parameters
        green_ampt (InfiltrationGreenAmpt): Green-Ampt infiltration model parameters
        curve_number (InfiltrationCurveNumber): SCS Curve Number infiltration model parameters
    """
    def __init__(self):
        self.horton = InfiltrationHorton()
        self.green_ampt = InfiltrationGreenAmpt()
        self.curve_number = InfiltrationCurveNumber()


class Polygon:
    """
    Geometric representation of a subcatchment area.
    
    Stores the polygon vertices that define the boundary of a subcatchment area.
    
    Attributes:
        area_name (str): Name of the associated subcatchment area
        x (list): List of x-coordinates of polygon vertices
        y (list): List of y-coordinates of polygon vertices
    """
    def __init__(self):
        self.area_name = None
        self.x = []
        self.y = []


class Area:
    """
    Represents a subcatchment area in the drainage system.
    
    Models a land area that generates runoff and routes it to a specific outlet point.
    Includes properties for surface characteristics, infiltration, and routing.

    Attributes:
        name (str): Unique identifier for the subcatchment
        rain_gage (str): Associated rain gage name
        outlet (str): Outlet node name
        area (float): Subcatchment area
        impervious_ratio (float): Fraction of impervious area
        width (float): Characteristic width of overland flow
        slope (float): Average surface slope
        curb_length (float): Length of curbs (for pollutant buildup)
        snow_pack (str): Name of snow pack parameter set
        manning_impervious (float): Manning's n for impervious area
        manning_pervious (float): Manning's n for pervious area
        depression_impervious (float): Depression storage for impervious area
        depression_pervious (float): Depression storage for pervious area
        impervious_without_depression (float): % of impervious area with no depression storage
        route_type (str): Internal routing method
        route_type_ratio (float): Fraction routed between subareas
        infiltration (dict): Infiltration parameters
    """
    def __init__(self):
        self.name = ''
        self.rain_gage = ''
        self.outlet = ''
        #
        self.area = 0.0
        self.impervious_ratio = 0
        self.width = 0
        self.slope = 0
        #
        self.curb_length = 0
        self.snow_pack = ''
        #
        self.manning_impervious = 0
        self.manning_pervious = 0
        self.depression_impervious = 0
        self.depression_pervious = 0
        self.impervious_without_depression = 0
        #
        self.route_type = 'OUTLET'
        self.route_type_ratio = 100
        #
        self.infiltration = Infiltration()
        #
        self.polygon = Polygon()

    def __repr__(self):
        return f'Subcatchment<{self.name}>'


class AreaList:
    """
    A collection class for managing subcatchment areas in a drainage network.
    
    This class provides storage and management for subcatchment areas,
    with methods for adding, accessing, and processing areas. It also handles
    reading from and writing to SWMM input files.
    
    Attributes:
        data (list): List containing all Area objects
    """
    def __init__(self):
        self.data = []

    def __repr__(self):
        """
        Returns a string representation of the AreaList.
        
        Returns:
            str: A string showing the count of areas in the list
        """
        return f'{len(self.data)} Areas'

    def __len__(self):
        """
        Returns the number of areas in the list.
        
        Returns:
            int: Number of areas in the list
        """
        return len(self.data)

    def __getitem__(self, key):
        """
        Gets an area by index or name.
        
        Args:
            key (int|str): Index or name of area to retrieve
            
        Returns:
            Area: The requested area
            
        Raises:
            KeyError: If area name not found
            TypeError: If key is not int or str
        """
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
        """
        Returns an iterator over the areas in the list.
        
        Returns:
            iterator: Iterator over the areas
        """
        return iter(self.data)

    def __contains__(self, item):
        """
        Checks if an area is in the list.
        
        Args:
            item (Area): Area to check for
            
        Returns:
            bool: True if the area is in the list, False otherwise
        """
        return item in self.data

    def _generate_default_name(self):
        """
        Generate a default name for an area based on existing areas count.
        
        Returns:
            str: Generated name in format 'Area##' where ## is sequential number
        """
        # Use 'Area' as the prefix for all generated area names
        prefix = 'Area'
        
        # Get count of areas with the same prefix
        existing_count = sum(1 for area in self.data if hasattr(area, 'name') and 
                             area.name and area.name.startswith(prefix))

        # Generate name with next number
        return f"{prefix}{existing_count + 1}"

    def add_area(self, area_information, check=False):
        """
        Creates and adds a new Area object to the list based on provided information.
        
        Args:
            check: if check
            area_information (dict): Dictionary containing area properties
            
        Returns:
            Area: The newly created and added area object
            
        Raises:
            ValueError: If an area with the same name already exists
        """
        if check:
            # Check if a name is provided and if it already exists in the collection
            if 'name' in area_information:
                requested_name = area_information['name']
                if any(area.name == requested_name for area in self.data):
                    raise ValueError(f"Area with name '{requested_name}' already exists")
                
        new_area = Area()
        
        # Set identification attributes
        new_area.name = area_information['name'] if 'name' in area_information else self._generate_default_name()
        new_area.rain_gage = area_information.get('rain_gage', '*')
        new_area.outlet = area_information.get('outlet', '*')
        
        # Set physical attributes
        # Validate and set physical attributes
        area_value = area_information.get('area', 0.0)
        if area_value <= 0:
            logger.warning(f"Area must be a positive number, got {area_value}")
        new_area.area = area_value
        
        impervious_ratio_value = area_information.get('impervious_ratio', 0)
        if not (0 <= impervious_ratio_value <= 100):
            logger.warning(f"Impervious ratio must be between 0 and 100, got {impervious_ratio_value}")
        new_area.impervious_ratio = impervious_ratio_value
        
        width_value = area_information.get('width', 0)
        if width_value <= 0:
            logger.warning(f"Width must be positive number, got {width_value}")
        new_area.width = width_value
        
        slope_value = area_information.get('slope', 0)
        if slope_value <= 0:
            logger.warning(f"Slope must be positive number, got {slope_value}")
        new_area.slope = slope_value
        
        # Set surface attributes
        new_area.curb_length = area_information.get('curb_length', 0)
        new_area.snow_pack = area_information.get('snow_pack', '')
        
        # Set hydraulic attributes
        # Validate hydraulic parameters
        manning_impervious = area_information.get('manning_impervious', 0)
        if not (0.01 <= manning_impervious <= 0.5):
            logger.warning(f"Manning's n for impervious area must be between 0.01-0.5, got {manning_impervious}")
        new_area.manning_impervious = manning_impervious
        
        manning_pervious = area_information.get('manning_pervious', 0)
        if not (0.01 <= manning_pervious <= 0.8):
            logger.warning(f"Manning's n for pervious area must be between 0.01-0.8, got {manning_pervious}")
        new_area.manning_pervious = manning_pervious
        
        depression_impervious = area_information.get('depression_impervious', 0)
        if depression_impervious < 0:
            logger.warning(f"Depression storage for impervious area cannot be negative, got {depression_impervious}")
        new_area.depression_impervious = depression_impervious
        
        depression_pervious = area_information.get('depression_pervious', 0)
        if depression_pervious < 0:
            logger.warning(f"Depression storage for pervious area cannot be negative, got {depression_pervious}")
        new_area.depression_pervious = depression_pervious
        
        impervious_without_depression = area_information.get('impervious_without_depression', 0)
        if not (0 <= impervious_without_depression <= 100):
            logger.warning(f"Impervious without depression must be 0-100%, got {impervious_without_depression}")
        new_area.impervious_without_depression = impervious_without_depression
        
        # Set routing attributes
        new_area.route_type = area_information.get('route_type', 'OUTLET')
        new_area.route_type_ratio = area_information.get('route_type_ratio', 100)
        
        # Handle infiltration parameters
        if 'infiltration' in area_information:
            new_area.infiltration = area_information['infiltration']
        else:
            # Set values for Horton infiltration
            if 'horton_maximum_rate' in area_information:
                new_area.infiltration.horton.maximum_rate = area_information.get('horton_maximum_rate')
            if 'horton_minimum_rate' in area_information:
                new_area.infiltration.horton.minimum_rate = area_information.get('horton_minimum_rate')
            if 'horton_decay_rate' in area_information:
                new_area.infiltration.horton.decay_rate = area_information.get('horton_decay_rate')
            if 'horton_dry_time' in area_information:
                new_area.infiltration.horton.dry_time = area_information.get('horton_dry_time')
            if 'horton_maximum_infiltration_volume' in area_information:
                new_area.infiltration.horton.maximum_infiltration_volume = area_information.get('horton_maximum_infiltration_volume')
            
            # Set values for Green-Ampt infiltration
            if 'green_ampt_soil_capillary_suction' in area_information:
                new_area.infiltration.green_ampt.soil_capillary_suction = area_information.get('green_ampt_soil_capillary_suction')
            if 'green_ampt_soil_saturated_hydraulic_conductivity' in area_information:
                new_area.infiltration.green_ampt.soil_saturated_hydraulic_conductivity = area_information.get('green_ampt_soil_saturated_hydraulic_conductivity')
            if 'green_ampt_initial_soil_moisture_deficit' in area_information:
                new_area.infiltration.green_ampt.initial_soil_moisture_deficit = area_information.get('green_ampt_initial_soil_moisture_deficit')
            
            # Set values for Curve Number infiltration
            if 'curve_number' in area_information:
                new_area.infiltration.curve_number.curve_number = area_information.get('curve_number')
            if 'curve_number_dry_time' in area_information:
                new_area.infiltration.curve_number.dry_time = area_information.get('curve_number_dry_time')
            if 'curve_number_soil_saturated_hydraulic_conductivity' in area_information:
                new_area.infiltration.curve_number.soil_saturated_hydraulic_conductivity = area_information.get('curve_number_soil_saturated_hydraulic_conductivity')
        
        # Check if the generated default name is unique (when name wasn't explicitly provided)
        if 'name' not in area_information and any(area.name == new_area.name for area in self.data):
            raise ValueError(f"Generated default name '{new_area.name}' already exists")
            
        # Add the new area to the collection
        self.data.append(new_area)
        return new_area

    @staticmethod
    def _prepare_section_contents(filename):
        """
        Prepares and combines section contents from SWMM input file.
        
        Args:
            filename (str): Path to the SWMM input file
            
        Returns:
            tuple: Combined content, polygon content
        """
        try:
            # Get subcatchments section and normalize
            sub_contents = get_swmm_inp_content(filename, '[SUBCATCHMENTS]')
            for index, line in enumerate(sub_contents):
                if len(line.split()) == 8:
                    sub_contents[index] += '  VOID'
            
            # Get subareas section and normalize
            subarea_contents = get_swmm_inp_content(filename, '[SUBAREAS]')
            for index, line in enumerate(subarea_contents):
                if len(line.split()) == 7:
                    subarea_contents[index] += '  100'
            
            # Combine contents
            content = combine_swmm_inp_contents(sub_contents, subarea_contents)
            
            # Add infiltration data
            infiltration_contents = get_swmm_inp_content(filename, '[INFILTRATION]')
            content = combine_swmm_inp_contents(content, infiltration_contents)
            
            # Get polygon data separately
            polygon_contents = get_swmm_inp_content(filename, '[Polygons]')
            
            return content, polygon_contents
        except Exception as e:
            raise IOError(f"Error preparing SWMM input sections: {str(e)}")

    @staticmethod
    def _parse_infiltration_data(pair, infiltration_type):
        """
        Parses infiltration data based on the specified infiltration type.
        
        Args:
            pair (list): Split line from the input file
            infiltration_type (str): Type of infiltration model
            
        Returns:
            dict: Dictionary containing infiltration parameters
        """
        try:
            infiltration_data = {}
            match infiltration_type:
                case 'Horton':
                    infiltration_data['horton_maximum_rate'] = float(pair[16])
                    infiltration_data['horton_minimum_rate'] = float(pair[17])
                    infiltration_data['horton_decay_rate'] = float(pair[18])
                    infiltration_data['horton_dry_time'] = float(pair[19])
                    infiltration_data['horton_maximum_infiltration_volume'] = float(pair[20])
                case 'GreenAmpt':
                    infiltration_data['green_ampt_soil_capillary_suction'] = float(pair[16])
                    infiltration_data['green_ampt_soil_saturated_hydraulic_conductivity'] = float(pair[17])
                    infiltration_data['green_ampt_initial_soil_moisture_deficit'] = float(pair[18])
                case 'CurveNumber':
                    infiltration_data['curve_number'] = float(pair[16])
                    infiltration_data['curve_number_soil_saturated_hydraulic_conductivity'] = float(pair[17])
                    infiltration_data['curve_number_dry_time'] = float(pair[18])
                case _:
                    raise ValueError(f"Unsupported infiltration type: {infiltration_type}")
            return infiltration_data
        except (IndexError, ValueError) as e:
            raise ValueError(f"Error parsing infiltration data: {str(e)}")
    
    def _process_polygon_data(self, polygon_contents):
        """
        Processes polygon data and assigns to corresponding areas.
        
        Args:
            polygon_contents (list): Lines from the Polygons section
        """
        for line in polygon_contents:
            try:
                pair = line.split()
                area_name = pair[0]
                x_coord = float(pair[1])
                y_coord = float(pair[2])
                
                for area in self.data:
                    if area.name == area_name:
                        area.polygon.x.append(x_coord)
                        area.polygon.y.append(y_coord)
                        area.polygon.area_name = area_name
            except (IndexError, ValueError) as e:
                # Log warning but continue processing other polygons
                logger.warning(f"Error processing polygon data: {str(e)}")
    
    def read_from_swmm_inp(self, filename):
        """
        Reads subcatchment area data from a SWMM .inp file and populates the area list.
        
        Args:
            filename (str): Path to the input .inp file
        
        Returns:
            int: 0 on success, raises exceptions on failure
            
        Raises:
            IOError: If file operations fail
            ValueError: If data parsing fails
        """
        try:
            # First, get the infiltration method from the OPTIONS section
            options_contents = get_swmm_inp_content(filename, '[OPTIONS]')
            infiltration_type = 'Horton'  # Default fallback
            
            # Extract the infiltration method from OPTIONS section
            for line in options_contents:
                pair = line.split()
                if len(pair) >= 2 and pair[0] == 'INFILTRATION':
                    # Convert SWMM infiltration method to our internal format
                    if pair[1].upper() in ['HORTON', 'MODIFIED_HORTON']:
                        infiltration_type = 'Horton'
                    elif pair[1].upper() in ['GREEN_AMPT', 'MODIFIED_GREEN_AMPT']:
                        infiltration_type = 'GreenAmpt'
                    elif pair[1].upper() == 'CURVE_NUMBER':
                        infiltration_type = 'CurveNumber'
                    break
            
            # Prepare and get section contents
            content, polygon_contents = self._prepare_section_contents(filename)
            
            # Process each line of combined content
            for line in content:
                pair = line.split()
                
                # Create basic area information dictionary
                dic = {
                    'name': pair[0],
                    'rain_gage': pair[1],
                    'outlet': pair[2],
                    'area': float(pair[3]),
                    'impervious_ratio': float(pair[4]),
                    'width': float(pair[5]),
                    'slope': float(pair[6]),
                    'curb_length': float(pair[7]),
                    'manning_impervious': float(pair[9]),
                    'manning_pervious': float(pair[10]),
                    'depression_impervious': float(pair[11]),
                    'depression_pervious': float(pair[12]),
                    'impervious_without_depression': float(pair[13]),
                    'route_type': pair[14]
                }
                
                # Parse and add infiltration data
                infiltration_data = self._parse_infiltration_data(pair, infiltration_type)
                dic.update(infiltration_data)
                
                # Handle special cases
                if dic['curb_length'] < 10e-5:
                    dic['curb_length'] = int(0)
                
                if pair[8] != 'VOID':
                    dic['snow_pack'] = pair[8]
                    
                if pair[15] != '100':
                    dic['route_type_ratio'] = float(pair[15])
                
                # Add the area to the collection
                self.add_area(dic)
            
            # Process polygon data
            self._process_polygon_data(polygon_contents)
            
            return 0
            
        except Exception as e:
            raise IOError(f"Error reading SWMM input file: {str(e)}")

    def _write_subcatchments_section(self, f):
        """
        Writes the SUBCATCHMENTS section to the file.
        
        Args:
            f (file): Open file handle to write to
        """
        f.write('\n\n[SUBCATCHMENTS]\n')
        f.write(';;Name       RainGage  Outlet     Area    %Imperv    Width    %Slope    CurbLen  (SnowPack)\n')
        for area in self.data:
            f.write(
                f'{area.name}  {area.rain_gage}  {area.outlet}  {area.area:8.3f}  '
                f'{area.impervious_ratio:8.2f}  {area.width:8.3f}  {area.slope:8.2f}  '
                f'{area.curb_length:8}  {area.snow_pack}\n')
    
    def _write_subareas_section(self, f):
        """
        Writes the SUBAREAS section to the file.
        
        Args:
            f (file): Open file handle to write to
        """
        f.write('\n\n[SUBAREAS]\n')
        f.write(';;Subcatchment   N-Imperv   N-Perv  S-Imperv  S-Perv  PctZero  RouteTo  (PctRouted)\n')
        for area in self.data:
            base_str = (f'{area.name}  {area.manning_impervious:8.3f}  {area.manning_pervious:8.2f}  '
                       f'{area.depression_impervious:8.2f}  {area.depression_pervious:8.2f}  '
                       f'{area.impervious_without_depression:8.2f}  {area.route_type:8}')
            
            if area.route_type_ratio != 100:
                f.write(f'{base_str}  {area.route_type_ratio:8}\n')
            else:
                f.write(f'{base_str}\n')
    
    def _write_infiltration_section(self, f, infiltration_type):
        """
        Writes the INFILTRATION section to the file based on infiltration type.
        
        Args:
            f (file): Open file handle to write to
            infiltration_type (str): Type of infiltration model
            
        Raises:
            ValueError: If infiltration type is not supported
        """
        f.write('\n\n[INFILTRATION]\n')
        
        match infiltration_type:
            case 'Horton':
                f.write(';;;;Subcatchment   MaxRate    MinRate    Decay      DryTime    MaxInfil \n')
                for area in self.data:
                    horton = area.infiltration.horton
                    f.write(
                        f'{area.name}  {horton.maximum_rate:8.1f}  {horton.minimum_rate:8.1f}  '
                        f'{horton.decay_rate:8.1f}  {horton.dry_time:8.1f}  '
                        f'{horton.maximum_infiltration_volume:8.1f}\n')
            case 'GreenAmpt':
                f.write(';;;;Subcatchment   Suction   Conductivity   InitialDeficit\n')
                for area in self.data:
                    ga = area.infiltration.green_ampt
                    f.write(
                        f'{area.name}  {ga.soil_capillary_suction:8}  '
                        f'{ga.soil_saturated_hydraulic_conductivity:8}  '
                        f'{ga.initial_soil_moisture_deficit:8}\n')
            case 'CurveNumber':
                f.write(';;;;Subcatchment   CurveNum   Conductivity   DryTime\n')
                for area in self.data:
                    cn = area.infiltration.curve_number
                    f.write(
                        f'{area.name}  {cn.curve_number:8}  '
                        f'{cn.soil_saturated_hydraulic_conductivity:8}  '
                        f'{cn.dry_time:8}\n')
            case _:
                raise ValueError(f"Unsupported infiltration type: {infiltration_type}")
    
    def _write_polygons_section(self, file):
        """
        Writes the Polygons section to the file.
        
        Args:
            file (file): Open file handle to write to
        """
        # Check if any areas have polygon data
        has_polygons = any(area.polygon.area_name is not None for area in self.data)

        if has_polygons:
            # Read file line by line to find sections
            with open(file.name, 'r') as read_file:
                lines = read_file.readlines()
            # Find [Polygons] section
            polygons_line = -1
            next_section_line = -1
            for i, line in enumerate(lines):
                if line.strip() == '[Polygons]':
                    polygons_line = i
                elif polygons_line != -1 and line.strip().startswith('['):
                    next_section_line = i
                    break
            if polygons_line == -1:
                # No existing section, create new one at current position
                file.write('\n\n[Polygons]\n')
                file.write(';;Name          X-Coord            Y-Coord\n')

                # Write polygon data
                for area in self.data:
                    if area.polygon.area_name is not None:
                        for xi, yi in zip(area.polygon.x, area.polygon.y):
                            file.write(f'{area.polygon.area_name}  {xi}  {yi}\n')
            else:
                # Section exists, we need to modify file content
                # Insert our polygon data just after the header line
                insert_position = polygons_line + 2  # +1 for the header, +1 for the column labels

                # Prepare polygon data lines
                new_lines = []
                for area in self.data:
                    if area.polygon.area_name is not None:
                        for xi, yi in zip(area.polygon.x, area.polygon.y):
                            new_lines.append(f'{area.polygon.area_name}  {xi}  {yi}\n')

                # Insert the new lines at the appropriate position
                lines[insert_position:insert_position] = new_lines

                # Rewrite the entire file
                file.seek(0)
                file.writelines(lines)
                file.truncate()
    
    def write_to_swmm_inp(self, filename):
        """
        Writes subcatchment area data to a SWMM .inp file.
        
        Args:
            filename (str): Path to the output .inp file
        
        Returns:
            int: 0 on success, raises exceptions on failure
            
        Raises:
            IOError: If file operations fail
            ValueError: If infiltration type is not supported
        """
        try:
            # Determine infiltration type to use
            infiltration_type = 'Horton'  # Default fallback
            
            # Check if file exists to read infiltration type from it
            import os
            if os.path.exists(filename):
                try:
                    # Get infiltration method from the OPTIONS section if file exists
                    options_contents = get_swmm_inp_content(filename, '[OPTIONS]')
                    
                    # Extract the infiltration method from OPTIONS section
                    for line in options_contents:
                        pair = line.split()
                        if len(pair) >= 2 and pair[0] == 'INFILTRATION':
                            # Convert SWMM infiltration method to our internal format
                            if pair[1].upper() in ['HORTON', 'MODIFIED_HORTON']:
                                infiltration_type = 'Horton'
                            elif pair[1].upper() in ['GREEN_AMPT', 'MODIFIED_GREEN_AMPT']:
                                infiltration_type = 'GreenAmpt'
                            elif pair[1].upper() == 'CURVE_NUMBER':
                                infiltration_type = 'CurveNumber'
                            break
                except Exception as e:
                    # If there's an error reading the file, use default infiltration type
                    logger.warning(f"Could not read infiltration type from file: {str(e)}")
                    logger.warning(f"Using default infiltration type: {infiltration_type}")
            
            with open(filename, 'a', encoding='utf-8') as f:
                self._write_subcatchments_section(f)
                self._write_subareas_section(f)
                self._write_infiltration_section(f, infiltration_type)
                self._write_polygons_section(f)
            return 0
        except Exception as e:
            raise IOError(f"Error writing to SWMM input file: {str(e)}")

