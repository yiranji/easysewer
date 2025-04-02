"""
Node Management Module

This module implements various types of nodes used in urban drainage networks including:
- Basic nodes (junctions)
- Outfall nodes with different boundary conditions
- Support for node properties like elevation, coordinates, and flow characteristics
"""
from .utils import *


class Node:
    """
    Base class for all node types in the drainage network.
    
    Represents a point element in the drainage network with basic properties
    like location and elevation.

    Attributes:
        name (str): Unique identifier for the node
        coordinate (list): [x, y] coordinates of the node
        elevation (float): Node invert elevation
    """
    def __init__(self):
        self.name = ''
        self.coordinate = [0.0, 0.0]
        self.elevation = 0

    def __repr__(self):
        return f'Node<{self.name}>'


class Junction(Node):
    """
    Junction node type for connecting conduits.
    
    Represents intersection points in the drainage network where flows combine
    or split. Includes properties for depth and ponding characteristics.

    Attributes:
        maximum_depth (float): Maximum water depth at junction
        initial_depth (float): Initial water depth at start of simulation
        overload_depth (float): Depth above which overflows occur
        surface_ponding_area (float): Area available for surface ponding
        dwf_base_value (float): Base dry weather flow value
        dwf_patterns (list): Time patterns for dry weather flow
        inflow (dict): Inflow characteristics and time series
    """
    def __init__(self):
        Node.__init__(self)
        self.maximum_depth = 0
        self.initial_depth = 0
        self.overload_depth = 0
        self.surface_ponding_area = 0
        #
        # dry weather flow
        self.dwf_base_value = 0
        self.dwf_patterns = []
        #
        # inflow
        self.inflow = None


class Outfall(Node):
    """
    Base class for outfall nodes.
    
    Represents points where water leaves the drainage system. Supports various
    boundary condition types through derived classes.

    Attributes:
        flap_gate (bool): Whether backflow prevention is present
        route_to (str): Routing destination for diverted flow
    """
    def __init__(self):
        Node.__init__(self)
        self.flap_gate = False
        self.route_to = ''


class OutfallFree(Outfall):
    """
    Free outfall node type.
    
    Represents an outfall with free boundary condition where water can freely exit the system.
    
    Attributes:
        Inherits all attributes from Outfall class
    """
    def __init__(self):
        Outfall.__init__(self)


class OutfallNormal(Outfall):
    """
    Normal outfall node type.
    
    Represents an outfall with normal boundary condition where water exits with normal depth.
    
    Attributes:
        Inherits all attributes from Outfall class
    """
    def __init__(self):
        Outfall.__init__(self)


class OutfallFixed(Outfall):
    """
    Fixed outfall node type.
    
    Represents an outfall with fixed boundary condition where water exits at a fixed stage.
    
    Attributes:
        Inherits all attributes from Outfall class
        stage (float): Fixed water surface elevation at the outfall
    """
    def __init__(self):
        Outfall.__init__(self)
        self.stage = 0.0


class OutfallTidal(Outfall):
    """
    Tidal outfall node type.
    
    Represents an outfall with tidal boundary condition where water level varies with tides.
    
    Attributes:
        Inherits all attributes from Outfall class
        tidal (str): Tidal condition identifier or time series name
    """
    def __init__(self):
        Outfall.__init__(self)
        self.tidal = ''


class OutfallTimeseries(Outfall):
    """
    Timeseries outfall node type.
    
    Represents an outfall with time-varying boundary condition specified by a time series.
    
    Attributes:
        Inherits all attributes from Outfall class
        time_series (str): Name of time series defining water surface elevation
    """
    def __init__(self):
        Outfall.__init__(self)
        self.time_series = ''


class NodeList:
    """
    A collection class for managing nodes in a drainage network.
    
    This class provides storage and management for various types of nodes (junctions, outfalls),
    with methods for adding, accessing, and processing nodes. It maintains spatial bounds
    information for all contained nodes.
    
    Attributes:
        data (list): List containing all node objects
        bounds (dict): Dictionary tracking spatial bounds of all nodes with keys:
            'min_x' (float): Minimum x-coordinate
            'min_y' (float): Minimum y-coordinate
            'max_x' (float): Maximum x-coordinate
            'max_y' (float): Maximum y-coordinate
    """
    def __init__(self):
        self.data = []
        self.bounds = {
            'min_x': float('inf'),
            'min_y': float('inf'),
            'max_x': float('-inf'),
            'max_y': float('-inf')
        }

    def __repr__(self):
        """
        Returns a string representation of the NodeList.
        
        Returns:
            str: A string showing the count of nodes in the list
        """
        return f'{len(self.data)} Nodes'

    def __len__(self):
        """
        Returns the number of nodes in the list.
        
        Returns:
            int: Number of nodes in the list
        """
        return len(self.data)

    def __getitem__(self, key):
        """
        Gets a node by index or name.
        
        Args:
            key (int|str): Index or name of node to retrieve
            
        Returns:
            Node: The requested node
            
        Raises:
            KeyError: If node name not found
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
        Returns an iterator for the node list.
        
        Returns:
            iterator: Iterator for the nodes
        """
        return iter(self.data)

    def __contains__(self, item):
        """
        Checks if a node exists in the list.
        
        Args:
            item: Node to check for
            
        Returns:
            bool: True if node exists in list
        """
        return item in self.data

    def add_node(self, node_type, node_information=None):
        """
        Add a new node to the data structure based on its type and information.
        Generates default values for missing essential attributes.

        Args:
            node_type (str): Type of node to add (e.g., 'junction', 'outfall_free')
            node_information (dict, optional): Dictionary containing node attributes
                                              Defaults to empty dict if None

        Returns:
            Node: The newly created node object

        Raises:
            TypeError: If node_type is not recognized
            ValueError: If a node with the same name already exists
                        or generated default name conflicts

        Example:
            >>> nodes = NodeList()
            >>> nodes.add_node('junction', {'name': 'J1', 'elevation': 100.0})
            <Node J1>
        """
        # Initialize node_information dict if not provided
        if node_information is None:
            node_information = {}

        # Normalize node type: lowercase and remove underscores
        normalized_type = node_type.lower().replace('_', '')

        # Check if a name is provided and if it already exists in the collection
        if 'name' in node_information:
            requested_name = node_information['name']
            if any(node.name == requested_name for node in self.data):
                raise ValueError(f"Node with name '{requested_name}' already exists")

        # Define attribute hierarchy based on class inheritance
        # Level 1: Common attributes for all Node types with defaults
        node_base_attrs = {
            'name': lambda node_type, info: info.get('name', self._generate_default_name(node_type)),
            'coordinate': lambda _, info: info.get('coordinate', self._generate_default_coordinate()),
            'elevation': lambda _, info: info.get('elevation', 0.0)
        }

        # Level 2: Attributes by node category with defaults
        junction_attrs = {
            'maximum_depth': lambda _, info: info.get('maximum_depth', 10.0),
            'initial_depth': lambda _, info: info.get('initial_depth', 0.0),
            'overload_depth': lambda _, info: info.get('overload_depth', 0.0),
            'surface_ponding_area': lambda _, info: info.get('surface_ponding_area', 0.0),
            'dwf_base_value': lambda _, info: info.get('dwf_base_value', 0.0),
            'dwf_patterns': lambda _, info: info.get('dwf_patterns', None)
        }

        outfall_base_attrs = {
            'flap_gate': lambda _, info: True if info.get('flap_gate') in ('YES', True) else False,
            'route_to': lambda _, info: info.get('route_to', None)
        }

        # Level 3: Specific attributes for outfall subtypes with defaults
        outfall_specific_attrs = {
            'outfallfixed': {
                'stage': lambda _, info: info.get('stage', 0.0)
            },
            'outfalltidal': {
                'tidal': lambda _, info: info.get('tidal', None)
            },
            'outfalltimeseries': {
                'time_series': lambda _, info: info.get('time_series', None)
            },
            'outfallfree': {},
            'outfallnormal': {}
        }

        # Define node type configurations
        node_types = {
            'junction': {
                'class': Junction,
                'attrs': {**node_base_attrs, **junction_attrs}
            },
            'outfallfree': {
                'class': OutfallFree,
                'attrs': {**node_base_attrs, **outfall_base_attrs, **outfall_specific_attrs['outfallfree']}
            },
            'outfallnormal': {
                'class': OutfallNormal,
                'attrs': {**node_base_attrs, **outfall_base_attrs, **outfall_specific_attrs['outfallnormal']}
            },
            'outfallfixed': {
                'class': OutfallFixed,
                'attrs': {**node_base_attrs, **outfall_base_attrs, **outfall_specific_attrs['outfallfixed']}
            },
            'outfalltidal': {
                'class': OutfallTidal,
                'attrs': {**node_base_attrs, **outfall_base_attrs, **outfall_specific_attrs['outfalltidal']}
            },
            'outfalltimeseries': {
                'class': OutfallTimeseries,
                'attrs': {**node_base_attrs, **outfall_base_attrs, **outfall_specific_attrs['outfalltimeseries']}
            }
        }

        # Check if normalized node type exists
        if normalized_type not in node_types:
            raise TypeError(
                f"Unknown node type '{node_type}', failed to add {node_information.get('name', 'unnamed node')}"
            )

        # Get node configuration
        node_config = node_types[normalized_type]
        node_class = node_config['class']
        attrs = node_config['attrs']

        # Create new node
        new_node = node_class()

        # Set all applicable attributes according to hierarchy, using default generators
        for attr, default_generator in attrs.items():
            value = default_generator(normalized_type, node_information)
            setattr(new_node, attr, value)

        # Check if the generated default name is unique (when name wasn't explicitly provided)
        if 'name' not in node_information and any(node.name == new_node.name for node in self.data):
            raise ValueError(f"Generated default name '{new_node.name}' already exists")

        # Update coordinate bounds if coordinate is set
        if hasattr(new_node, 'coordinate') and new_node.coordinate:
            self._update_bounds(new_node.coordinate)

        # Add node to data structure
        self.data.append(new_node)

        return new_node  # Return the created node for immediate use if needed

    def _update_bounds(self, coordinate):
        """
        Update the coordinate bounds based on a new node's position.
        
        Args:
            coordinate (list): [x, y] coordinates of the node
        """
        if not coordinate:
            return

        x, y = coordinate
        self.bounds['min_x'] = min(self.bounds['min_x'], x)
        self.bounds['min_y'] = min(self.bounds['min_y'], y)
        self.bounds['max_x'] = max(self.bounds['max_x'], x)
        self.bounds['max_y'] = max(self.bounds['max_y'], y)

    def _generate_default_name(self, node_type):
        """
        Generate a default name for a node based on its type and existing nodes count.
        
        Args:
            node_type (str): Type of node (e.g. 'junction', 'outfall_free')
            
        Returns:
            str: Generated name in format 'TYPE##' where TYPE is first 3 letters of node type
                 and ## is sequential number
        """
        # Get count of nodes with the same type prefix
        prefix = node_type[:3].upper()  # First 3 letters of node type
        existing_count = sum(1 for node in self.data if hasattr(node, 'name') and
                             node.name and node.name.startswith(prefix))

        # Generate name with next number
        return f"{prefix}{existing_count + 1}"

    def _generate_default_coordinate(self):
        """
        Generate a sensible default coordinate based on existing nodes.
        
        Returns:
            tuple: (x, y) coordinates
            
        Logic:
            1. If no nodes exist, returns (0, 0)
            2. If bounds are established, returns center with slight offset
            3. Otherwise places near last node with offset
        """
        # If no nodes exist yet, start at origin
        if not self.data:
            return 0, 0

        # If bounds are established, place in center with slight offset
        if self.bounds['min_x'] != float('inf'):
            center_x = (self.bounds['min_x'] + self.bounds['max_x']) / 2
            center_y = (self.bounds['min_y'] + self.bounds['max_y']) / 2
            # Add a small offset to avoid perfect overlap
            offset = len(self.data) * 10
            return center_x + offset, center_y + offset

        # Fallback - place near the last node
        last_node = self.data[-1]
        if hasattr(last_node, 'coordinate') and last_node.coordinate:
            last_x, last_y = last_node.coordinate
            return last_x + 50, last_y + 50

        return 0, 0

    def read_from_swmm_inp(self, filename):
        """
        Read node data from a SWMM input file.
        
        Processes the following sections from SWMM input file:
        - [JUNCTIONS]
        - [OUTFALLS]
        - [COORDINATES]
        - [DWF]
        - [INFLOWS]
        
        Args:
            filename (str): Path to the SWMM input file
            
        Returns:
            int: 0 if successful
            
        Raises:
            FileNotFoundError: If the input file doesn't exist
            ValueError: If required sections are missing
            Exception: For unsupported inflow types
            
        Note:
            Continues processing other nodes if errors occur with individual nodes
        """
        try:
            # Read all required sections
            junction_contents = get_swmm_inp_content(filename, '[JUNCTIONS]')
            coordinates = get_swmm_inp_content(filename, '[COORDINATES]')
            outfall_contents = get_swmm_inp_content(filename, '[OUTFALLS]')
            dwf_contents = get_swmm_inp_content(filename, '[DWF]')
            inflow_contents = get_swmm_inp_content(filename, '[INFLOWS]')

            # Process coordinates (needed by both junctions and outfalls)
            coordinates_dic = self._process_coordinates(coordinates)

            # Process each node type
            self._process_junctions(junction_contents, coordinates_dic)
            self._process_outfalls(outfall_contents, coordinates_dic)
            self._process_dry_weather_flows(dwf_contents)
            self._process_inflows(inflow_contents)

            return 0
        except Exception as e:
            # Re-raise with more context
            raise type(e)(f"Error reading SWMM input file: {str(e)}")

    def _process_coordinates(self, coordinates):
        """Process coordinates data from SWMM input file."""
        coordinates_dic = {}
        for line in coordinates:
            keys = line.split()
            if len(keys) >= 3:  # Ensure we have at least node name, x, y
                coordinates_dic[keys[0]] = [float(keys[1]), float(keys[2])]
        return coordinates_dic

    def _process_junctions(self, junction_contents, coordinates_dic):
        """Process junction data from SWMM input file."""
        for line in junction_contents:
            parts = line.split()
            if len(parts) < 6:  # Skip lines with insufficient data
                continue

            try:
                dic = {
                    'name': parts[0],
                    'coordinate': coordinates_dic.get(parts[0], [0.0, 0.0]),
                    'elevation': float(parts[1]),
                    'maximum_depth': float(parts[2]),
                    'initial_depth': float(parts[3]),
                    'overload_depth': float(parts[4]),
                    'surface_ponding_area': float(parts[5])
                }
                self.add_node('junction', dic)
            except (ValueError, KeyError) as e:
                # Log error but continue processing other junctions
                print(f"Warning: Error processing junction '{parts[0]}': {str(e)}")

    def _process_outfalls(self, outfall_contents, coordinates_dic):
        """Process outfall data from SWMM input file."""
        for line in outfall_contents:
            parts = line.split()
            if len(parts) < 3:  # Skip lines with insufficient data
                continue

            try:
                # Set up common attributes
                dic = {
                    'name': parts[0],
                    'coordinate': coordinates_dic.get(parts[0], [0.0, 0.0]),
                    'elevation': float(parts[1])
                }

                # Process flap gate and route to parameters (last elements)
                if parts[-1] == 'YES':
                    dic['flap_gate'] = 'YES'
                elif parts[-1] == 'NO':
                    dic['flap_gate'] = 'NO'
                else:
                    dic['flap_gate'] = parts[-2]
                    dic['route_to'] = parts[-1]

                # Process outfall type
                outfall_type = parts[2]
                if outfall_type == 'FREE':
                    self.add_node('outfall_free', dic)
                elif outfall_type == 'NORMAL':
                    self.add_node('outfall_normal', dic)
                elif outfall_type == 'FIXED':
                    dic['stage'] = float(parts[3])
                    self.add_node('outfall_fixed', dic)
                elif outfall_type == 'TIDAL':
                    dic['tidal'] = parts[3]
                    self.add_node('outfall_tidal', dic)
                elif outfall_type == 'TIMESERIES':
                    dic['time_series'] = parts[3]
                    self.add_node('outfall_time_series', dic)
            except (ValueError, KeyError) as e:
                # Log error but continue processing other outfalls
                print(f"Warning: Error processing outfall '{parts[0]}': {str(e)}")

    def _process_dry_weather_flows(self, dwf_contents):
        """Process dry weather flow data from SWMM input file."""
        for line in dwf_contents:
            parts = line.split()
            if len(parts) < 3:  # Skip lines with insufficient data
                continue

            node_name = parts[0]
            try:
                node = self.index_of(node_name, return_node=True)
            except ValueError:
                node = None

            if node and hasattr(node, 'dwf_base_value'):
                node.dwf_base_value = parts[2]
                node.dwf_patterns = parts[3:] if len(parts) > 3 else []

    def _process_inflows(self, inflow_contents):
        """Process inflow data from SWMM input file."""
        for line in inflow_contents:
            parts = line.split()
            if len(parts) < 8:  # Skip lines with insufficient data
                continue

            if parts[1] != 'FLOW':
                raise Exception('Unsupported inflow type, only FLOW is accepted.')

            node_name = parts[0]
            try:
                node = self.index_of(node_name, return_node=True)
            except ValueError:
                node = None

            if node:
                inflow_data = {
                    'time_series': parts[2],
                    'type': parts[3],
                    'm_factor': float(parts[4]),
                    's_factor': float(parts[5]),
                    'baseline': float(parts[6]),
                    'pattern': parts[7]
                }
                if hasattr(node, 'inflow'):
                    node.inflow = inflow_data

    def write_to_swmm_inp(self, filename):
        """
        Write node data to a SWMM input file.
        
        Writes the following sections to SWMM input file:
        - [JUNCTIONS]
        - [OUTFALLS]
        - [COORDINATES]
        - [DWF]
        - [INFLOWS]
        
        Args:
            filename (str): Path to the SWMM input file
            
        Returns:
            int: 0 if successful
            
        Raises:
            IOError: If there's an error writing to the file
            
        Note:
            Appends to existing file content
        """
        try:
            with open(filename, 'a', encoding='utf-8') as f:
                # Write junctions section
                self._write_junctions_section(f)

                # Write outfalls section
                self._write_outfalls_section(f)

                # Write coordinates section
                self._write_coordinates_section(f)

                # Write DWF section
                self._write_dwf_section(f)

                # Write inflows section
                self._write_inflows_section(f)

            return 0
        except IOError as e:
            raise IOError(f"Error writing to SWMM input file: {str(e)}")

    def _write_junctions_section(self, file):
        """Write junctions section to the SWMM input file."""
        file.write('\n\n[JUNCTIONS]\n')
        file.write(';;Name  Elevation  MaxDepth  InitDepth  SurDepth  Ponding\n')

        for node in self.data:
            if isinstance(node, Junction):
                file.write(
                    f'{node.name:8}  {node.elevation:8.3f}  {node.maximum_depth:8.3f}  '
                    f'{node.initial_depth:8.3f}  {node.overload_depth:8.3f}  {node.surface_ponding_area:8.3f}\n')

    def _write_outfalls_section(self, file):
        """Write outfalls section to the SWMM input file."""
        file.write('\n\n[OUTFALLS]\n')
        file.write(';;Name  Elevation  Type  //  Gated  RouteTo\n')

        outfall_types = {
            OutfallFree: ('FREE', None),
            OutfallNormal: ('NORMAL', None),
            OutfallFixed: ('FIXED', 'stage'),
            OutfallTidal: ('TIDAL', 'tidal'),
            OutfallTimeseries: ('TIMESERIES', 'time_series')
        }

        for node in self.data:
            for outfall_class, (type_name, extra_attr) in outfall_types.items():
                if isinstance(node, outfall_class):
                    route_to = node.route_to if hasattr(node, 'route_to') and node.route_to else ''
                    gate_flag = 'YES' if node.flap_gate else 'NO'

                    if extra_attr:
                        extra_value = getattr(node, extra_attr, '')
                        file.write(
                            f'{node.name:8}  {node.elevation:8.3f}    {type_name}    '
                            f'{extra_value:8}  {gate_flag}  {route_to}\n')
                    else:
                        file.write(
                            f'{node.name:8}  {node.elevation:8.3f}    {type_name}    '
                            f'{gate_flag:8}  {route_to}\n')

    def _write_coordinates_section(self, file):
        """Write coordinates section to the SWMM input file."""
        file.write('\n\n[COORDINATES]\n')
        file.write(';;Name  X-Coord  Y-Coord\n')

        for node in self.data:
            if hasattr(node, 'coordinate') and len(node.coordinate) >= 2:
                file.write(f'{node.name:8}  {node.coordinate[0]:8.2f}  {node.coordinate[1]:8.2f}\n')

    def _write_dwf_section(self, file):
        """Write dry weather flow section to the SWMM input file."""
        file.write('\n\n[DWF]\n')
        file.write(';;Node           Constituent      Baseline   Patterns  \n')

        for node in self.data:
            if isinstance(node, Junction) and hasattr(node, 'dwf_base_value') and node.dwf_base_value != 0:
                patterns = ' '.join(node.dwf_patterns if hasattr(node, 'dwf_patterns') and node.dwf_patterns else [])
                file.write(f'{node.name}  FLOW  {node.dwf_base_value}  {patterns}\n')

    def _write_inflows_section(self, file):
        """Write inflows section to the SWMM input file."""
        file.write('\n\n[INFLOWS]\n')
        file.write(';;Node           Constituent      Time Series      Type     Mfactor  Sfactor  Baseline Pattern\n')

        for node in self.data:
            if isinstance(node, Junction) and hasattr(node, 'inflow') and node.inflow is not None:
                values = list(node.inflow.values())
                formatted_values = '    '.join(str(value) for value in values)
                file.write(f'{node.name}  FLOW  {formatted_values}  \n')

    def index_of(self, node_name, return_node=False):
        """
        Find a node's index by name.

        Args:
            node_name (str): The name of the node to find
            return_node (bool): If True, returns the node object instead of index

        Returns:
            int or object: The index of the node or the node object if return_node is True

        Raises:
            ValueError: If no node with the given name is found
        """
        for index, item in enumerate(self.data):
            if item.name == node_name:
                return item if return_node else index
        raise ValueError(f"No item found with name '{node_name}'")

