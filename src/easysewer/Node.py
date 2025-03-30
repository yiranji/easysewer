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
    def __init__(self):
        Outfall.__init__(self)


class OutfallNormal(Outfall):
    def __init__(self):
        Outfall.__init__(self)


class OutfallFixed(Outfall):
    def __init__(self):
        Outfall.__init__(self)
        self.stage = 0.0


class OutfallTidal(Outfall):
    def __init__(self):
        Outfall.__init__(self)
        self.tidal = ''


class OutfallTimeseries(Outfall):
    def __init__(self):
        Outfall.__init__(self)
        self.time_series = ''


class NodeList:
    def __init__(self):
        self.data = []

    def __repr__(self):
        return f'{len(self.data)} Nodes'

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

    def add_node(self, node_type, node_information):
        def execute(func1, func2):
            def inner():
                # new an object according to node_type
                new_node = func1()
                # add essential information
                if 'name' in node_information:
                    new_node.name = node_information['name']
                else:  # if it can not find name, raise error
                    # print('Unknown Node: Can not recognize node name')
                    return -1
                if 'coordinate' in node_information:
                    new_node.coordinate = node_information['coordinate']
                if 'elevation' in node_information:
                    new_node.elevation = node_information['elevation']
                # for Outfalls
                if 'flap_gate' in node_information:
                    new_node.flap_gate = True if node_information['flap_gate'] == 'YES' else False
                if 'route_to' in node_information:
                    new_node.route_to = node_information['route_to']
                # add node_type related information
                func2(new_node)
                # update node_list
                self.data.append(new_node)
                return 0

            return inner

        match node_type:
            case 'junction' | 'Junction':
                def junction_type(new_node):
                    if 'maximum_depth' in node_information:
                        new_node.maximum_depth = node_information['maximum_depth']
                    if 'initial_depth' in node_information:
                        new_node.initial_depth = node_information['initial_depth']
                    if 'overload_depth' in node_information:
                        new_node.overload_depth = node_information['overload_depth']
                    if 'surface_ponding_area' in node_information:
                        new_node.surface_ponding_area = node_information['surface_ponding_area']
                    if 'dwf_base_value' in node_information:
                        new_node.dwf_base_value = node_information['dwf_base_value']
                    if 'dwf_patterns' in node_information:
                        new_node.dwf_patterns = node_information['dwf_patterns']

                return execute(Junction, junction_type)()

            case 'outfall_free' | 'OutfallFree':
                def outfall_free_type(_):
                    pass

                return execute(OutfallFree, outfall_free_type)()

            case 'outfall_normal' | 'OutfallNormal':
                def outfall_normal_type(_):
                    pass

                return execute(OutfallNormal, outfall_normal_type)()

            case 'outfall_fixed' | 'OutfallFixed':
                def outfall_fixed_type(new_node):
                    if 'stage' in node_information:
                        new_node.stage = node_information['stage']

                return execute(OutfallFixed, outfall_fixed_type)()

            case 'outfall_tidal' | 'OutfallTidal':
                def outfall_tidal_type(new_node):
                    if 'tidal' in node_information:
                        new_node.tidal = node_information['tidal']

                return execute(OutfallTidal, outfall_tidal_type)()

            case 'outfall_time_series' | 'OutfallTimeseries':
                def outfall_time_series_type(new_node):
                    if 'time_series' in node_information:
                        new_node.time_series = node_information['time_series']

                return execute(OutfallTimeseries, outfall_time_series_type)()

            case _:
                raise TypeError(f"Unknown node type, failed to add {node_information['name']}")

    def read_from_swmm_inp(self, filename):
        junction_contents = get_swmm_inp_content(filename, '[JUNCTIONS]')
        coordinates = get_swmm_inp_content(filename, '[COORDINATES]')
        outfall_contents = get_swmm_inp_content(filename, '[OUTFALLS]')
        dwf_contents = get_swmm_inp_content(filename, '[DWF]')
        inflow_contents = get_swmm_inp_content(filename, '[INFLOWS]')

        # coordinate list
        coordinates_dic = {}
        for line in coordinates:
            keys = line.split()
            coordinates_dic[keys[0]] = [float(keys[1]), float(keys[2])]
        # process junctions
        for line in junction_contents:
            pair = line.split()
            dic = {'name': pair[0], 'coordinate': [0.0, 0.0], 'elevation': float(pair[1]),
                   'maximum_depth': float(pair[2]), 'initial_depth': float(pair[3]),
                   'overload_depth': float(pair[4]), 'surface_ponding_area': float(pair[5])}
            dic['coordinate'] = coordinates_dic[dic['name']]
            self.add_node('junction', dic)
        # process outfalls
        for line in outfall_contents:
            pair = line.split()
            dic = {'name': pair[0], 'coordinate': [0.0, 0.0], 'elevation': float(pair[1])}
            dic['coordinate'] = coordinates_dic[dic['name']]
            #
            if pair[-1] == 'YES':
                dic['flap_gate'] = 'YES'
            elif pair[-1] == 'NO':
                dic['flap_gate'] = 'NO'
            else:
                dic['flap_gate'] = pair[-2]
                dic['route_to'] = pair[-1]
            #
            match pair[2]:
                case 'FREE':
                    self.add_node('outfall_free', dic)
                case 'NORMAL':
                    self.add_node('outfall_normal', dic)
                case 'FIXED':
                    dic['stage'] = float(pair[2])
                    self.add_node('outfall_fixed', dic)
                case 'TIDAL':
                    dic['tidal'] = float(pair[2])
                    self.add_node('outfall_tidal', dic)
                case 'TIMESERIES':
                    dic['time_series'] = float(pair[2])
                    self.add_node('outfall_time_series', dic)
                case _:
                    pass
        # process DWF
        for line in dwf_contents:
            pair = line.split()
            for node in self.data:
                if node.name == pair[0]:
                    node.dwf_base_value = pair[2]
                    for pattern in pair[3::]:
                        node.dwf_patterns.append(pattern)
        # process inflow
        for line in inflow_contents:
            pair = line.split()
            if pair[1] != 'FLOW':
                raise Exception('Unsupported inflow type, only FLOW is accepted.')
            for node in self.data:
                if node.name == pair[0]:
                    result = {'time_series': pair[2], 'type': pair[3], 'm_factor': float(pair[4]),
                              's_factor': float(pair[5]), 'baseline': float(pair[6]), 'pattern': pair[7]}
                    node.inflow = result
        return 0

    def write_to_swmm_inp(self, filename):
        with open(filename, 'a', encoding='utf-8') as f:
            f.write('\n\n[JUNCTIONS]\n')
            f.write(';;Name  Elevation  MaxDepth  InitDepth  SurDepth  Ponding\n')
            for node in self.data:
                if isinstance(node, Junction):
                    f.write(
                        f'{node.name:8}  {node.elevation:8.3f}  {node.maximum_depth:8.3f}  {node.initial_depth:8.3f}  {node.overload_depth:8.3f}  {node.surface_ponding_area:8.3f}\n')
            #
            f.write('\n\n[OUTFALLS]\n')
            f.write(';;Name  Elevation  Type  //  Gated  RouteTo\n')
            for node in self.data:
                if isinstance(node, OutfallFree):
                    msg = 'YES' if node.flap_gate else 'NO'
                    f.write(f'{node.name:8}  {node.elevation:8.3f}    FREE    {msg:8}  {node.route_to}\n')
                if isinstance(node, OutfallNormal):
                    msg = 'YES' if node.flap_gate else 'NO'
                    f.write(f'{node.name:8}  {node.elevation:8.3f}    NORMAL    {msg:8}  {node.route_to}\n')
                if isinstance(node, OutfallFixed):
                    msg = 'YES' if node.flap_gate else 'NO'
                    f.write(
                        f'{node.name:8}  {node.elevation:8.3f}    FIXED    {node.stage:8}  {msg}  {node.route_to}\n')
                if isinstance(node, OutfallTidal):
                    msg = 'YES' if node.flap_gate else 'NO'
                    f.write(
                        f'{node.name:8}  {node.elevation:8.3f}    TIDAL    {node.tidal:8}  {msg}  {node.route_to}\n')
                if isinstance(node, OutfallTimeseries):
                    msg = 'YES' if node.flap_gate else 'NO'
                    f.write(
                        f'{node.name:8}  {node.elevation:8.3f}    TIMESERIES    {node.time_series:8}  {msg}  {node.route_to}\n')
            #
            f.write('\n\n[COORDINATES]\n')
            f.write(';;Name  X-Coord  Y-Coord\n')
            for node in self.data:
                f.write(f'{node.name:8}  {node.coordinate[0]:8.2f}  {node.coordinate[1]:8.2f}\n')
            #
            f.write('\n\n[DWF]\n')
            f.write(';;Node           Constituent      Baseline   Patterns  \n')
            for node in self.data:
                if isinstance(node, Junction):
                    if node.dwf_base_value != 0:
                        string = ' '.join(node.dwf_patterns)
                        f.write(f'{node.name}  FLOW  {node.dwf_base_value}  {string}\n')
            #
            f.write('\n\n[INFLOWS]\n')
            f.write(';;Node           Constituent      Time Series      Type     Mfactor  Sfactor  Baseline Pattern\n')
            for node in self.data:
                if isinstance(node, Junction):
                    if node.inflow is not None:
                        res = [str(i) for i in list(node.inflow.values())]
                        res = '    '.join(res)
                        f.write(f'{node.name}  FLOW  {res}  \n')
        return 0

    def index_of(self, node_name):
        for index, item in enumerate(self.data):
            if item.name == node_name:
                return index
        raise ValueError(f"No item found with name '{node_name}'")
