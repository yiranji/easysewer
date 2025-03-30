"""
Subcatchment Area Management Module

This module handles subcatchment areas in the drainage network, including their
physical characteristics, infiltration parameters, and routing behavior.
"""
from .utils import *


class InfiltrationHorton:
    def __init__(self):
        self.maximum_rate = 50  # mm/h
        self.minimum_rate = 5  # mm/h
        self.decay_rate = 5  # 1/h
        self.dry_time = 7  # day
        self.maximum_infiltration_volume = 0  # mm, 0 if not applicable


class InfiltrationGreenAmpt:
    def __init__(self):
        self.soil_capillary_suction = 0
        self.soil_saturated_hydraulic_conductivity = 0
        self.initial_soil_moisture_deficit = 0


class InfiltrationCurveNumber:
    def __init__(self):
        self.curve_number = 0
        self.dry_time = 0
        self.soil_saturated_hydraulic_conductivity = 0


class Infiltration:
    def __init__(self):
        self.horton = InfiltrationHorton()
        self.green_ampt = InfiltrationGreenAmpt()
        self.curve_number = InfiltrationCurveNumber()


class Polygon:
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
    def __init__(self):
        self.data = []

    def __repr__(self):
        return f'{len(self.data)} Areas'

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

    def add_area(self, area_information):
        new_area = Area()
        if 'name' in area_information:
            new_area.name = area_information['name']
        if 'rain_gage' in area_information:
            new_area.rain_gage = area_information['rain_gage']
        if 'outlet' in area_information:
            new_area.outlet = area_information['outlet']
        #
        if 'area' in area_information:
            new_area.area = area_information['area']
        if 'impervious_ratio' in area_information:
            new_area.impervious_ratio = area_information['impervious_ratio']
        if 'width' in area_information:
            new_area.width = area_information['width']
        if 'slope' in area_information:
            new_area.slope = area_information['slope']
        #
        if 'curb_length' in area_information:
            new_area.curb_length = area_information['curb_length']
        if 'snow_pack' in area_information:
            new_area.snow_pack = area_information['snow_pack']
        #
        if 'manning_impervious' in area_information:
            new_area.manning_impervious = area_information['manning_impervious']
        if 'manning_pervious' in area_information:
            new_area.manning_pervious = area_information['manning_pervious']
        if 'depression_impervious' in area_information:
            new_area.depression_impervious = area_information['depression_impervious']
        if 'depression_pervious' in area_information:
            new_area.depression_pervious = area_information['depression_pervious']
        if 'impervious_without_depression' in area_information:
            new_area.impervious_without_depression = area_information['impervious_without_depression']
        #
        if 'route_type' in area_information:
            new_area.route_type = area_information['route_type']
        if 'route_type_ratio' in area_information:
            new_area.route_type_ratio = area_information['route_type_ratio']
        #
        if 'infiltration' in area_information:
            new_area.infiltration = area_information['infiltration']
        #
        #
        self.data.append(new_area)

    def read_from_swmm_inp(self, filename, infiltration_type='Horton'):
        sub_contents = get_swmm_inp_content(filename, '[SUBCATCHMENTS]')
        # fill in default values
        for index, line in enumerate(sub_contents):
            if len(line.split()) == 8:
                sub_contents[index] += '  VOID'
        #
        subarea_contents = get_swmm_inp_content(filename, '[SUBAREAS]')
        # fill in default values
        for index, line in enumerate(subarea_contents):
            if len(line.split()) == 7:
                subarea_contents[index] += '  100'
        content = combine_swmm_inp_contents(sub_contents, subarea_contents)
        #
        infiltration_contents = get_swmm_inp_content(filename, '[INFILTRATION]')
        content = combine_swmm_inp_contents(content, infiltration_contents)

        for line in content:
            pair = line.split()
            dic = {'name': pair[0],
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
            if dic['curb_length'] < 10e-5:
                dic['curb_length'] = int(0)
            #
            if pair[8] != 'VOID':
                dic['snow_pack'] = pair[8]
            if pair[15] != '100':
                dic['route_type_ratio'] = float(pair[15])
            #
            new_infiltration = Infiltration()

            match infiltration_type:
                case 'Horton':
                    new_infiltration.horton.maximum_rate = float(pair[16])
                    new_infiltration.horton.minimum_rate = float(pair[17])
                    new_infiltration.horton.decay_rate = float(pair[18])
                    new_infiltration.horton.dry_time = float(pair[19])
                    new_infiltration.horton.maximum_infiltration_volume = float(pair[20])
                case 'GreenAmpt':
                    new_infiltration.green_ampt.soil_capillary_suction = float(pair[16])
                    new_infiltration.green_ampt.soil_saturated_hydraulic_conductivity = float(pair[17])
                    new_infiltration.green_ampt.initial_soil_moisture_deficit = float(pair[18])
                case 'CurveNumber':
                    new_infiltration.curve_number.curve_number = float(pair[16])
                    new_infiltration.curve_number.soil_saturated_hydraulic_conductivity = float(pair[17])
                    new_infiltration.curve_number.dry_time = float(pair[18])

            dic['infiltration'] = new_infiltration
            #
            self.add_area(dic)

        #
        polygon_contents = get_swmm_inp_content(filename, '[Polygons]')
        for line in polygon_contents:
            pair = line.split()
            for area in self.data:
                if area.name == pair[0]:
                    area.polygon.x.append(float(pair[1]))
                    area.polygon.y.append(float(pair[2]))
                    area.polygon.area_name = pair[0]
        return 0

    def write_to_swmm_inp(self, filename, infiltration_type='Horton'):
        with open(filename, 'a', encoding='utf-8') as f:
            f.write('\n\n[SUBCATCHMENTS]\n')
            f.write(
                ';;Name       RainGage  Outlet     Area    %Imperv    Width    %Slope    CurbLen  (SnowPack)\n')
            for area in self.data:
                f.write(
                    f'{area.name}  {area.rain_gage}  {area.outlet}  {area.area:8.3f}  {area.impervious_ratio:8.2f}  {area.width:8.3f}  {area.slope:8.2f}  {area.curb_length:8}  {area.snow_pack}\n')
            #
            f.write('\n\n[SUBAREAS]\n')
            f.write(';;Subcatchment   N-Imperv   N-Perv  S-Imperv  S-Perv  PctZero  RouteTo  (PctRouted)\n')
            for area in self.data:
                if area.route_type_ratio != 100:
                    f.write(
                        f'{area.name}  {area.manning_impervious:8.3f}  {area.manning_pervious:8.2f}  {area.depression_impervious:8.2f}  {area.depression_pervious:8.2f}  {area.impervious_without_depression:8.2f}  {area.route_type:8}  {area.route_type_ratio:8}\n')
                else:
                    f.write(
                        f'{area.name}  {area.manning_impervious:8.3f}  {area.manning_pervious:8.2f}  {area.depression_impervious:8.2f}  {area.depression_pervious:8.2f}  {area.impervious_without_depression:8.2f}  {area.route_type:8}\n')
            #
            f.write('\n\n[INFILTRATION]\n')
            match infiltration_type:
                case 'Horton':
                    f.write(';;;;Subcatchment   MaxRate    MinRate    Decay      DryTime    MaxInfil \n')
                    for area in self.data:
                        f.write(
                            f'{area.name}  {area.infiltration.horton.maximum_rate:8.1f}  {area.infiltration.horton.minimum_rate:8.1f}  {area.infiltration.horton.decay_rate:8.1f}  {area.infiltration.horton.dry_time:8.1f}  {area.infiltration.horton.maximum_infiltration_volume:8.1f}\n')
                case 'GreenAmpt':
                    f.write(';;;;Subcatchment   \n')
                    for area in self.data:
                        f.write(
                            f'{area.name}  {area.infiltration.green_ampt.soil_capillary_suction:8}  {area.infiltration.green_ampt.soil_saturated_hydraulic_conductivity:8}  {area.infiltration.green_ampt.initial_soil_moisture_deficit:8}\n')
                case 'CurveNumber':
                    f.write(';;;;Subcatchment   \n')
                    for area in self.data:
                        f.write(
                            f'{area.name}  {area.infiltration.curve_number.curve_number:8}  {area.infiltration.curve_number.soil_saturated_hydraulic_conductivity:8}  {area.infiltration.curve_number.dry_time:8}\n')
            #
            f.write('\n\n[Polygons]\n')
            f.write(';;Subcatchment   X-Coord            Y-Coord\n')
            for area in self.data:
                if area.polygon.area_name is not None:
                    for xi, yi in zip(area.polygon.x, area.polygon.y):
                        f.write(f'{area.polygon.area_name}  {xi}  {yi}\n')
            return 0

