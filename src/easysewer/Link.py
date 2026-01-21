"""
Link Management Module

This module implements various types of conduits and channels that connect nodes
in the drainage network. Supports different cross-section types and hydraulic
characteristics.
"""
from .utils import *
import logging

logger = logging.getLogger(__name__)


class Vertices:
    """
    Container for link vertex coordinates.
    
    Stores the geometry of a link's path between its endpoints.

    Attributes:
        link_name (str): Name of the associated link
        x (list): X-coordinates of vertices
        y (list): Y-coordinates of vertices
    """
    def __init__(self):
        self.link_name = None
        self.x = []
        self.y = []


class Link:
    """
    Base class for all hydraulic links.
    
    Represents a connection between two nodes in the drainage network.

    Attributes:
        name (str): Unique identifier for the link
        vertices (Vertices): Geometric path of the link
    """
    def __init__(self):
        self.name = ''
        self.vertices = Vertices()

    def __repr__(self):
        return f'Link<{self.name}>'


class Conduit(Link):
    """
    Base class for conduit-type links.
    
    Represents closed conduits or open channels with specific hydraulic properties.

    Attributes:
        upstream_node (str): Name of upstream node
        downstream_node (str): Name of downstream node
        length (float): Conduit length
        roughness (float): Manning's roughness coefficient
        upstream_offset (float): Offset at upstream end
        downstream_offset (float): Offset at downstream end
        initial_flow (float): Initial flow rate
        maximum_flow (float): Maximum allowed flow rate
    """
    def __init__(self):
        Link.__init__(self)
        self.upstream_node = ''
        self.downstream_node = ''
        self.length = 0.0
        self.roughness = 0.0
        self.upstream_offset = 0.0
        self.downstream_offset = 0.0
        # optional variable
        self.initial_flow = 0
        self.maximum_flow = 0  # means no limit


class ConduitCircle(Conduit):
    """
    Circular conduit type.
    
    Represents a conduit with circular cross-section.
    
    Attributes:
        Inherits all attributes from Conduit class
        barrels_number (int): Number of identical barrels (pipes)
        height (float): Diameter of the circular conduit
    """
    def __init__(self):
        Conduit.__init__(self)
        self.barrels_number = 1
        self.height = 0.0


class ConduitFilledCircle(Conduit):
    """
    Partially filled circular conduit type.
    
    Represents a circular conduit with sediment or partial filling.
    
    Attributes:
        Inherits all attributes from Conduit class
        barrels_number (int): Number of identical barrels (pipes)
        height (float): Diameter of the circular conduit
        filled (float): Height of filling/sediment from bottom
    """
    def __init__(self):
        Conduit.__init__(self)
        self.barrels_number = 1
        self.height = 0.0
        self.filled = 0.0


class ConduitRectangleOpen(Conduit):
    """
    Open rectangular conduit type.
    
    Represents an open channel with rectangular cross-section.
    
    Attributes:
        Inherits all attributes from Conduit class
        barrels_number (int): Number of identical barrels (channels)
        height (float): Height of the rectangular channel
        width (float): Width of the rectangular channel
    """
    def __init__(self):
        Conduit.__init__(self)
        self.barrels_number = 1
        self.height = 0.0
        self.width = 0.0


class ConduitCustom(Conduit):
    """
    Custom conduit type.
    
    Represents a conduit with custom-defined cross-section based on a curve.
    
    Attributes:
        Inherits all attributes from Conduit class
        barrels_number (int): Number of identical barrels
        height (float): Maximum height of the custom cross-section
        curve (str): Name of the curve defining the custom cross-section
    """
    def __init__(self):
        Conduit.__init__(self)
        self.barrels_number = 1
        self.height = 0.0
        self.curve = ''


class Pump(Link):
    """
    Pump link type.
    
    Represents a pump that lifts water from an inlet node to an outlet node,
    using a pump curve or an ideal transfer (curve name '*').
    
    Attributes:
        upstream_node (str): Name of inlet node
        downstream_node (str): Name of outlet node
        curve (str): Pump curve name, or '*' for ideal pump
        status (str): Initial status, 'ON' or 'OFF'
        startup_depth (float): Startup water depth at inlet node
        shutoff_depth (float): Shutoff water depth at inlet node
    """
    def __init__(self):
        Link.__init__(self)
        self.upstream_node = ''
        self.downstream_node = ''
        self.curve = ''
        self.status = 'ON'
        self.startup_depth = 0.0
        self.shutoff_depth = 0.0


class LinkList:
    """
    A collection class for managing links in a drainage network.
    
    This class provides storage and management for various types of links (conduits),
    with methods for adding, accessing, and processing links.
    
    Attributes:
        data (list): List containing all link objects
    """
    def __init__(self):
        self.data = []

    def __repr__(self):
        """
        Returns a string representation of the LinkList.
        
        Returns:
            str: A string showing the count of links in the list
        """
        return f'{len(self.data)} Links'

    def __len__(self):
        """
        Returns the number of links in the list.
        
        Returns:
            int: Number of links in the list
        """
        return len(self.data)

    def __getitem__(self, key):
        """
        Gets a link by index or name.
        
        Args:
            key (int|str): Index or name of link to retrieve
            
        Returns:
            Link: The requested link
            
        Raises:
            KeyError: If link name not found
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
        Returns an iterator for the link list.
        
        Returns:
            iterator: Iterator for the links
        """
        return iter(self.data)

    def __contains__(self, item):
        """
        Checks if a link exists in the list.
        
        Args:
            item: Link to check for
            
        Returns:
            bool: True if link exists in list
        """
        return item in self.data

    def add_link(self, link_type, link_information=None, check=False):
        """
        Add a new link to the data structure based on its type and information.
        Generates default values for missing essential attributes.

        Args:
            check: if check
            link_type (str): Type of link to add (e.g., 'conduit_circle', 'conduit_filled_circle')
            link_information (dict, optional): Dictionary containing link attributes
                                              Defaults to empty dict if None

        Returns:
            Link: The newly created link object

        Raises:
            TypeError: If link_type is not recognized
            ValueError: If a link with the same name already exists
                        or if required attributes are missing

        Example:
            >>> links = LinkList()
            >>> links.add_link('conduit_circle', {'name': 'C1', 'upstream_node': 'J1', 
            ...                                  'downstream_node': 'J2', 'height': 0.5})
            <Link C1>
        """
        # Initialize link_information dict if not provided
        if link_information is None:
            link_information = {}

        # Normalize link type: lowercase and handle both formats
        normalized_type = link_type.lower().replace('_', '')
        # Keep recognized prefixes; default unknown types to conduit*
        if normalized_type.startswith('conduit') or normalized_type.startswith('pump'):
            normalized_type = normalized_type
        else:
            normalized_type = 'conduit' + normalized_type

        if check:
            # Check if a name is provided and if it already exists in the collection
            if 'name' in link_information:
                requested_name = link_information['name']
                if any(link.name == requested_name for link in self.data):
                    raise ValueError(f"Link with name '{requested_name}' already exists")

        # Define attribute hierarchy based on class inheritance
        # Level 1: Common attributes for all Link types with defaults
        link_base_attrs = {
            'name': lambda link_type, info: (
                info['name'] if 'name' in info
                else self._generate_default_name(link_type)
            )
        }

        # Level 2: Attributes for all Conduit types with defaults
        conduit_base_attrs = {
            'upstream_node': lambda _, info: info.get('upstream_node', ''),
            'downstream_node': lambda _, info: info.get('downstream_node', ''),
            'length': lambda _, info: info.get('length', 4.29),
            'roughness': lambda _, info: info.get('roughness', 0.013),  # Default Manning's n
            'upstream_offset': lambda _, info: info.get('upstream_offset', 0.0),
            'downstream_offset': lambda _, info: info.get('downstream_offset', 0.0),
            'initial_flow': lambda _, info: info.get('initial_flow', 0.0),
            'maximum_flow': lambda _, info: info.get('maximum_flow', 0.0)
        }

        # Level 2: Attributes for Pump with defaults
        pump_base_attrs = {
            'upstream_node': lambda _, info: info.get('upstream_node', ''),
            'downstream_node': lambda _, info: info.get('downstream_node', ''),
            'curve': lambda _, info: info.get('curve', ''),
            'status': lambda _, info: info.get('status', 'ON'),
            'startup_depth': lambda _, info: info.get('startup_depth', 0.0),
            'shutoff_depth': lambda _, info: info.get('shutoff_depth', 0.0),
        }

        # Level 3: Specific attributes for conduit subtypes with defaults
        conduit_specific_attrs = {
            'conduitcircle': {
                'barrels_number': lambda _, info: info.get('barrels_number', 1),
                'height': lambda _, info: info.get('height', 0.3)
            },
            'conduitfilledcircle': {
                'barrels_number': lambda _, info: info.get('barrels_number', 1),
                'height': lambda _, info: info.get('height', 0.3),
                'filled': lambda _, info: info.get('filled', 0.0)
            },
            'conduitrectangleopen': {
                'barrels_number': lambda _, info: info.get('barrels_number', 1),
                'height': lambda _, info: info.get('height', 2.0),
                'width': lambda _, info: info.get('width', 1.0)
            },
            'conduitcustom': {
                'barrels_number': lambda _, info: info.get('barrels_number', 1),
                'height': lambda _, info: info.get('height', 1.0),
                'curve': lambda _, info: info.get('curve', '')
            }
        }

        # Define link type configurations
        link_types = {
            'conduitcircle': {
                'class': ConduitCircle,
                'attrs': {**link_base_attrs, **conduit_base_attrs, **conduit_specific_attrs['conduitcircle']}
            },
            'conduitfilledcircle': {
                'class': ConduitFilledCircle,
                'attrs': {**link_base_attrs, **conduit_base_attrs, **conduit_specific_attrs['conduitfilledcircle']}
            },
            'conduitrectangleopen': {
                'class': ConduitRectangleOpen,
                'attrs': {**link_base_attrs, **conduit_base_attrs, **conduit_specific_attrs['conduitrectangleopen']}
            },
            'conduitcustom': {
                'class': ConduitCustom,
                'attrs': {**link_base_attrs, **conduit_base_attrs, **conduit_specific_attrs['conduitcustom']}
            },
            'pump': {
                'class': Pump,
                'attrs': {**link_base_attrs, **pump_base_attrs}
            }
        }

        # Check if normalized link type exists
        if normalized_type not in link_types:
            raise TypeError(
                f"Unknown link type '{link_type}', failed to add {link_information.get('name', 'unnamed link')}"
            )

        # Get link configuration
        link_config = link_types[normalized_type]
        link_class = link_config['class']
        attrs = link_config['attrs']

        # Create new link
        new_link = link_class()

        # Set all applicable attributes according to hierarchy, using default generators
        for attr, default_generator in attrs.items():
            value = default_generator(normalized_type, link_information)
            setattr(new_link, attr, value)

        # Check if the generated default name is unique (when name wasn't explicitly provided)
        if 'name' not in link_information and any(link.name == new_link.name for link in self.data):
            raise ValueError(f"Generated default name '{new_link.name}' already exists")

        # Add link to data structure
        self.data.append(new_link)

        return new_link  # Return the created link for immediate use if needed

    def _generate_default_name(self, link_type):
        """
        Generate a default name for a link based on its type and existing links count.
        
        Args:
            link_type (str): Type of link (e.g. 'conduit_circle', 'conduit_custom')
            
        Returns:
            str: Generated name in format 'TYPE##' where TYPE is first 3 letters of link type
                 and ## is sequential number
        """
        # Determine specific type for prefix
        if link_type.startswith('conduit'):
            if '_' in link_type:
                specific_type = link_type.split('_')[1]
            else:
                specific_type = link_type.replace('conduit', '')
        elif link_type.startswith('pump'):
            specific_type = 'pump'
        else:
            specific_type = link_type
            
        # Get first 3 letters of the specific type
        prefix = specific_type[:3].upper()  # First 3 letters of link type
        
        # Get count of links with the same type prefix
        existing_count = sum(1 for link in self.data if hasattr(link, 'name') and 
                             link.name and link.name.startswith(prefix))

        # Generate name with next number
        return f"{prefix}{existing_count + 1}"

    def read_from_swmm_inp(self, filename):
        """
        Read link data from a SWMM input file.
        
        Processes the following sections from SWMM input file:
        - [CONDUITS]
        - [XSECTIONS]
        - [VERTICES]
        
        Args:
            filename (str): Path to the SWMM input file
            
        Returns:
            int: 0 if successful
            
        Raises:
            FileNotFoundError: If the input file doesn't exist
            ValueError: If required sections are missing or data is malformed
            Exception: For other processing errors
        """
        try:
            # Read all required sections
            conduit_contents = get_swmm_inp_content(filename, '[CONDUITS]')
            x_section_contents = get_swmm_inp_content(filename, '[XSECTIONS]')
            vertices_contents = get_swmm_inp_content(filename, '[VERTICES]')
            pump_contents = get_swmm_inp_content(filename, '[PUMPS]')
            
            # Process conduits and cross-sections
            self._process_conduits_and_xsections(conduit_contents, x_section_contents)
            
            # Process vertices
            self._process_vertices(vertices_contents)
            # Process pumps
            self._process_pumps(pump_contents)
            
            return 0
        except Exception as e:
            # Re-raise with more context
            raise type(e)(f"Error reading SWMM input file: {str(e)}")
            
    def _process_conduits_and_xsections(self, conduit_contents, x_section_contents):
        """
        Process conduit and cross-section data from SWMM input file.
        
        Args:
            conduit_contents (list): Lines from the [CONDUITS] section
            x_section_contents (list): Lines from the [XSECTIONS] section
        """
        # Fill in default values for conduit contents
        for index, line in enumerate(conduit_contents):
            parts = line.split()
            if len(parts) == 7:
                conduit_contents[index] += '  0  0'  # Add default initial_flow and maximum_flow
            elif len(parts) == 8:
                conduit_contents[index] += '  0'  # Add default maximum_flow
        
        # Combine conduit and cross-section data
        combined_content = combine_swmm_inp_contents(conduit_contents, x_section_contents)
        
        # Process each combined line
        for line in combined_content:
            try:
                parts = line.split()
                if len(parts) < 10:  # Need at least basic conduit info + shape
                    continue
                    
                # Create basic conduit dictionary
                conduit_data = {
                    'name': parts[0],
                    'upstream_node': parts[1],
                    'downstream_node': parts[2],
                    'length': float(parts[3]),
                    'roughness': float(parts[4]),
                    'upstream_offset': float(parts[5]),
                    'downstream_offset': float(parts[6]),
                    'initial_flow': float(parts[7]),
                    'maximum_flow': float(parts[8])
                }
                
                # Process based on cross-section shape
                shape = parts[9]
                if shape == 'CIRCULAR':
                    if len(parts) > 10:
                        conduit_data['height'] = float(parts[10])
                    if len(parts) >= 15:
                        conduit_data['barrels_number'] = int(parts[14])
                    self.add_link('conduit_circle', conduit_data)
                    
                elif shape == 'FILLED_CIRCULAR':
                    if len(parts) > 11:
                        conduit_data['height'] = float(parts[10])
                        conduit_data['filled'] = float(parts[11])
                    if len(parts) >= 15:
                        conduit_data['barrels_number'] = int(parts[14])
                    self.add_link('conduit_filled_circle', conduit_data)
                    
                elif shape == 'RECT_OPEN':
                    if len(parts) > 11:
                        conduit_data['height'] = float(parts[10])
                        conduit_data['width'] = float(parts[11])
                    if len(parts) >= 15:
                        conduit_data['barrels_number'] = int(parts[14])
                    self.add_link('conduit_rectangle_open', conduit_data)
                    
                elif shape == 'CUSTOM':
                    if len(parts) > 11:
                        conduit_data['height'] = float(parts[10])
                        conduit_data['curve'] = parts[11]
                    if len(parts) >= 13:
                        conduit_data['barrels_number'] = int(parts[-1])
                    self.add_link('conduit_custom', conduit_data)
            except (ValueError, IndexError) as e:
                # Log error but continue processing other conduits
                logger.warning(f"Error processing conduit in line '{line}': {str(e)}")
                
    def _process_vertices(self, vertices_contents):
        """
        Process vertex data from SWMM input file.
        
        Args:
            vertices_contents (list): Lines from the [VERTICES] section
        """
        for line in vertices_contents:
            try:
                parts = line.split()
                if len(parts) < 3:  # Need at least link name, x, y
                    continue
                    
                link_name = parts[0]
                x_coord = float(parts[1])
                y_coord = float(parts[2])
                
                # Find the link and add vertex
                for link in self.data:
                    if link.name == link_name:
                        link.vertices.x.append(x_coord)
                        link.vertices.y.append(y_coord)
                        link.vertices.link_name = link_name
                        break
            except (ValueError, IndexError) as e:
                # Log error but continue processing other vertices
                logger.warning(f"Error processing vertex in line '{line}': {str(e)}")

    def _process_pumps(self, pump_contents):
        """
        Process pump data from SWMM input file.
        
        Args:
            pump_contents (list): Lines from the [PUMPS] section
        """
        for line in pump_contents:
            try:
                parts = line.split()
                if len(parts) < 5:
                    continue
                dic = {
                    'name': parts[0],
                    'upstream_node': parts[1],
                    'downstream_node': parts[2],
                    'curve': parts[3],
                    'status': parts[4]
                }
                # Optional startup and shutoff depths
                if len(parts) > 5:
                    dic['startup_depth'] = float(parts[5])
                if len(parts) > 6:
                    dic['shutoff_depth'] = float(parts[6])
                self.add_link('pump', dic)
            except (ValueError, IndexError) as e:
                logger.warning(f"Error processing pump in line '{line}': {str(e)}")

    def write_to_swmm_inp(self, filename):
        """
        Write link data to a SWMM input file.
        
        Writes the following sections to the SWMM input file:
        - [CONDUITS]
        - [XSECTIONS]
        - [VERTICES]
        
        Args:
            filename (str): Path to the SWMM input file to write to
            
        Returns:
            int: 0 if successful
            
        Raises:
            IOError: If the file cannot be opened or written to
        """
        try:
            with open(filename, 'a', encoding='utf-8') as f:
                # Write CONDUITS section
                f.write('\n\n[CONDUITS]\n')
                f.write(
                    ';;Name                          Upstream  Downstream  Length  Roughness  Up-offset Down-offset  Init_flow Max_flow\n')
                for link in self.data:
                    if isinstance(link, Conduit):
                        f.write(
                            f'{link.name:30}  {link.upstream_node:8}  {link.downstream_node:8}  {link.length:8.2f}  {link.roughness:8.3f}  {link.upstream_offset:8.3f}  {link.downstream_offset:8.3f}  {link.initial_flow:8.2f}  {link.maximum_flow:8.2f}\n')
                
                # Write XSECTIONS section
                f.write('\n\n[XSECTIONS]\n')
                f.write(
                    ';;Name                          Shape         Geom1      Geom2      Geom3      Geom4      Barrels      (Culvert)\n')
                for link in self.data:
                    zero = 0
                    if isinstance(link, ConduitCircle):
                        f.write(
                            f'{link.name:30}  CIRCULAR  {link.height:8.2f}  {zero:8}  {zero:8}  {zero:8}  {link.barrels_number:8}\n')
                    elif isinstance(link, ConduitFilledCircle):
                        f.write(
                            f'{link.name:30}  FILLED_CIRCULAR  {link.height:8.2f}  {link.filled:8.2f}  {zero:8}  {zero:8}  {link.barrels_number:8}\n')
                    elif isinstance(link, ConduitRectangleOpen):
                        f.write(
                            f'{link.name:30}  RECT_OPEN {link.height:8.2f}  {link.width:8.2f}  {zero:8}  {zero:8}  {link.barrels_number:8}\n')
                    elif isinstance(link, ConduitCustom):
                        f.write(
                            f'{link.name:30}  CUSTOM    {link.height:8.2f}  {link.curve:8}  0  0  {link.barrels_number:8}\n')
                
                # Write PUMPS section
                f.write('\n\n[PUMPS]\n')
                f.write(';;\t\t Inlet           \t Outlet          \t Pump           \t Init. \t Startup \t Shutoff\n')
                f.write(';;Name          \t Node            \t Node            \t Curve          \t Status \t Depth   \t Depth   \n')
                f.write(';;-------------- \t ---------------- \t ---------------- \t ---------------- \t ------ \t -------- \t --------\n')
                for link in self.data:
                    if isinstance(link, Pump):
                        f.write(
                            f'{link.name:16}\t {link.upstream_node:16}\t {link.downstream_node:16}\t {link.curve:16}\t {link.status:6}\t {link.startup_depth:8.3f}\t {link.shutoff_depth:8.3f}\n'
                        )
                
                # Write VERTICES section
                f.write('\n\n[VERTICES]\n')
                f.write(';;Link           X-Coord            Y-Coord\n')
                for link in self.data:
                    if link.vertices.link_name is not None:
                        for xi, yi in zip(link.vertices.x, link.vertices.y):
                            f.write(f'{link.vertices.link_name}  {xi}  {yi}\n')
            return 0
        except IOError as e:
            raise IOError(f"Error writing to SWMM input file: {str(e)}")

    def index_of(self, link_name, return_link=False):
        """
        Find the index of a link by name.
        
        Args:
            link_name (str): Name of the link to find
            return_link (bool, optional): If True, returns the link object instead of index
                                         Defaults to False
            
        Returns:
            int or Link: Index of the link in the data list, or the link object if return_link is True
            
        Raises:
            ValueError: If no link with the given name is found
            
        Example:
            >>> links = LinkList()
            >>> links.add_link('conduit_circle', {'name': 'C1'})
            >>> links.index_of('C1')
            0
        """
        for index, item in enumerate(self.data):
            if item.name == link_name:
                return item if return_link else index
        raise ValueError(f"No item found with name '{link_name}'")
