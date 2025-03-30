"""
Link Management Module

This module implements various types of conduits and channels that connect nodes
in the drainage network. Supports different cross-section types and hydraulic
characteristics.
"""
from .utils import *


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
    def __init__(self):
        Conduit.__init__(self)
        self.barrels_number = 1
        self.height = 0.0


class ConduitFilledCircle(Conduit):
    def __init__(self):
        Conduit.__init__(self)
        self.barrels_number = 1
        self.height = 0.0
        self.filled = 0.0


class ConduitRectangleOpen(Conduit):
    def __init__(self):
        Conduit.__init__(self)
        self.barrels_number = 1
        self.height = 0.0
        self.width = 0.0


class ConduitCustom(Conduit):
    def __init__(self):
        Conduit.__init__(self)
        self.barrels_number = 1
        self.height = 0.0
        self.curve = ''


class LinkList:
    def __init__(self):
        self.data = []

    def __repr__(self):
        return f'{len(self.data)} Links'

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

    def add_link(self, link_type, link_information):
        def execute(func1, func2):
            def inner():
                new_link = func1()
                # basic information of conduit
                new_link.name = link_information['name']
                new_link.upstream_node = link_information['upstream_node']
                new_link.downstream_node = link_information['downstream_node']
                new_link.length = link_information['length']
                new_link.roughness = link_information['roughness']
                new_link.upstream_offset = link_information['upstream_offset']
                new_link.downstream_offset = link_information['downstream_offset']
                if 'initial_flow' in link_information:
                    new_link.initial_flow = link_information['initial_flow']
                if 'maximum_flow' in link_information:
                    new_link.maximum_flow = link_information['maximum_flow']
                # specific information of different conduit type
                func2(new_link)
                # add new link to link list
                self.data.append(new_link)
                return 0

            return inner

        match link_type:
            case 'conduit_circle' | 'ConduitCircle':
                def conduit_circle(new_link):
                    new_link.height = link_information['height']
                    if 'barrels_number' in link_information:
                        new_link.barrels_number = link_information['barrels_number']

                return execute(ConduitCircle, conduit_circle)()

            case 'conduit_filled_circle' | 'ConduitFilledCircle':

                def conduit_filled_circle(new_link):
                    new_link.height = link_information['height']
                    new_link.filled = link_information['filled']
                    if 'barrels_number' in link_information:
                        new_link.barrels_number = link_information['barrels_number']

                return execute(ConduitFilledCircle, conduit_filled_circle)()

            case 'conduit_rectangle_open' | 'ConduitRectangleOpen':
                def conduit_rectangle_open(new_link):
                    new_link.height = link_information['height']
                    new_link.width = link_information['width']
                    if 'barrels_number' in link_information:
                        new_link.barrels_number = link_information['barrels_number']

                return execute(ConduitRectangleOpen, conduit_rectangle_open)()

            case 'conduit_custom' | 'ConduitCustom':
                def conduit_custom(new_link):
                    new_link.height = link_information['height']
                    new_link.curve = link_information['curve']
                    if 'barrels_number' in link_information:
                        new_link.barrels_number = link_information['barrels_number']

                return execute(ConduitCustom, conduit_custom)()

            case _:
                raise TypeError(f"Unknown link type, failed to add {link_information['name']}")

    def read_from_swmm_inp(self, filename):
        conduit_contents = get_swmm_inp_content(filename, '[CONDUITS]')
        # fill in default values
        for index, line in enumerate(conduit_contents):
            if len(line.split()) == 7:
                conduit_contents[index] += '  0  0'
            elif len(line.split()) == 8:
                conduit_contents[index] += '  0'
        x_section = get_swmm_inp_content(filename, '[XSECTIONS]')
        content = combine_swmm_inp_contents(conduit_contents, x_section)
        for line in content:
            pair = line.split()
            dic = {'name': pair[0], 'upstream_node': pair[1], 'downstream_node': pair[2], 'length': float(pair[3]),
                   'roughness': float(pair[4]), 'upstream_offset': float(pair[5]), 'downstream_offset': float(pair[6]),
                   'initial_flow': float(pair[7]), 'maximum_flow': float(pair[8])}

            match pair[9]:
                case 'CIRCULAR':
                    dic['height'] = float(pair[10])
                    # optional variable: Barrels
                    if len(pair) >= 15:
                        dic['barrels_number'] = int(pair[14])
                    self.add_link('conduit_circle', dic)

                case 'FILLED_CIRCULAR':
                    dic['height'] = float(pair[10])
                    dic['filled'] = float(pair[11])
                    # optional variable: Barrels
                    if len(pair) >= 15:
                        dic['barrels_number'] = int(pair[14])
                    self.add_link('conduit_filled_circle', dic)

                case 'RECT_OPEN':
                    dic['height'] = float(pair[10])
                    dic['width'] = float(pair[11])
                    # optional variable: Barrels
                    if len(pair) >= 15:
                        dic['barrels_number'] = int(pair[14])
                    self.add_link('conduit_rectangle_open', dic)

                case 'CUSTOM':
                    dic['height'] = float(pair[10])
                    dic['curve'] = pair[11]
                    # optional variable: Barrels
                    if len(pair) >= 13:
                        dic['barrels_number'] = int(pair[-1])
                    self.add_link('conduit_custom', dic)
        #
        vertices_contents = get_swmm_inp_content(filename, '[VERTICES]')
        for line in vertices_contents:
            pair = line.split()
            for link in self.data:
                if link.name == pair[0]:
                    link.vertices.x.append(float(pair[1]))
                    link.vertices.y.append(float(pair[2]))
                    link.vertices.link_name = pair[0]
        return 0

    def write_to_swmm_inp(self, filename):
        with open(filename, 'a', encoding='utf-8') as f:
            f.write('\n\n[CONDUITS]\n')
            f.write(
                ';;Name                          Upstream  Downstream  Length  Roughness  Up-offset Down-offset  Init_flow Max_flow\n')
            for link in self.data:
                f.write(
                    f'{link.name:30}  {link.upstream_node:8}  {link.downstream_node:8}  {link.length:8.2f}  {link.roughness:8.3f}  {link.upstream_offset:8.3f}  {link.downstream_offset:8.3f}  {link.initial_flow:8.2f}  {link.maximum_flow:8.2f}\n')
            #
            f.write('\n\n[XSECTIONS]\n')
            f.write(
                ';;Name                          Shape         Geom1      Geom2      Geom3      Geom4      Barrels      (Culvert)\n')
            for link in self.data:
                zero = 0
                if isinstance(link, ConduitCircle):
                    f.write(
                        f'{link.name:30}  CIRCULAR  {link.height:8.2f}  {zero:8}  {zero:8}  {zero:8}  {link.barrels_number:8}\n')
                if isinstance(link, ConduitFilledCircle):
                    f.write(
                        f'{link.name:30}  FILLED_CIRCULAR  {link.height:8.2f}  {link.filled:8.2f}  {zero:8}  {zero:8}  {link.barrels_number:8}\n')
                if isinstance(link, ConduitRectangleOpen):
                    f.write(
                        f'{link.name:30}  RECT_OPEN {link.height:8.2f}  {link.width:8.2f}  {zero:8}  {zero:8}  {link.barrels_number:8}\n')
                if isinstance(link, ConduitCustom):
                    f.write(
                        f'{link.name:30}  CUSTOM    {link.height:8.2f}  {link.curve:8}  0  0  {link.barrels_number:8}\n')
            #
            f.write('\n\n[VERTICES]\n')
            f.write(';;Link           X-Coord            Y-Coord\n')
            for link in self.data:
                if link.vertices.link_name is not None:
                    for xi, yi in zip(link.vertices.x, link.vertices.y):
                        f.write(f'{link.vertices.link_name}  {xi}  {yi}\n')
        return 0

    def index_of(self, link_name):
        for index, item in enumerate(self.data):
            if item.name == link_name:
                return index
        raise ValueError(f"No item found with name '{link_name}'")
