"""
Curve and Pattern Management Module

This module handles various types of curves and patterns used in the drainage model,
including rating curves, shape curves, and time patterns for different parameters.
"""
from .utils import *


class Curve:
    def __init__(self):
        self.name = ''
        self.type = ''
        self.x = []
        self.y = []


class Pattern:
    def __init__(self):
        self.name = ''
        self.type = ''
        self.value = []


class ValueList:
    """
    Container for curves and patterns used in the model.
    
    Manages collections of curves (rating curves, shape curves) and patterns
    (time patterns) used throughout the drainage model.

    Attributes:
        curve_list (list): Collection of curve objects
        pattern_list (list): Collection of pattern objects
    """
    def __init__(self):
        self.curve_list = []
        self.pattern_list = []

    def __repr__(self):
        return 'ValueList'

    def add_curve(self, new_curve):
        self.curve_list.append(new_curve)

    def add_pattern(self, new_pattern):
        self.pattern_list.append(new_pattern)

    def read_from_swmm_inp(self, filename):
        #
        content = get_swmm_inp_content(filename, '[CURVES]')
        this_curve = Curve()
        this_curve.name = 'initial'
        for line in content:
            pair = line.split()
            name = pair[0]
            if this_curve.name == 'initial':
                this_curve.name = name
            if this_curve.name != name:
                self.add_curve(this_curve)
                this_curve = Curve()
                this_curve.name = name
            if len(pair) % 2 == 0:
                this_curve.type = pair[1]
                x_list = [float(i) for index, i in enumerate(pair[2::]) if index % 2 == 0]
                y_list = [float(i) for index, i in enumerate(pair[2::]) if index % 2 == 1]
            else:
                x_list = [float(i) for index, i in enumerate(pair[1::]) if index % 2 == 0]
                y_list = [float(i) for index, i in enumerate(pair[1::]) if index % 2 == 1]
            for x, y in zip(x_list, y_list):
                this_curve.x.append(x)
                this_curve.y.append(y)
        if this_curve.name != 'initial':
            self.add_curve(this_curve)
        #
        content = get_swmm_inp_content(filename, '[PATTERNS]')
        this_pattern = Pattern()
        this_pattern.name = 'initial'
        for line in content:
            pair = line.split()
            name = pair[0]
            if this_pattern.name == 'initial':
                this_pattern.name = name
            if this_pattern.name != name:
                self.add_pattern(this_pattern)
                this_pattern = Pattern()
                this_pattern.name = name
            if pair[1].isalpha():
                this_pattern.type = pair[1]
                for factor in pair[2::]:
                    this_pattern.value.append(factor)
            else:
                for factor in pair[1::]:
                    this_pattern.value.append(factor)
        if this_pattern.name != 'initial':
            self.add_pattern(this_pattern)
        return 0

    def write_to_swmm_inp(self, filename):
        with open(filename, 'a', encoding='utf-8') as f:
            f.write('\n\n[CURVES]\n')
            f.write(';;Name           Type       X-Value    Y-Value  \n')
            for curve in self.curve_list:
                flag = 0
                for x, y in zip(curve.x, curve.y):
                    if flag == 0:
                        f.write(f'{curve.name}  {curve.type:8}  {x}  {y}\n')
                        flag = 1
                    else:
                        f.write(f'{curve.name}            {x}  {y}\n')
                f.write(';\n')
            #
            f.write('\n\n[PATTERNS]\n')
            f.write(';;Name           Type       Multipliers\n')
            for pattern in self.pattern_list:
                string = ' '.join(pattern.value)
                f.write(f'{pattern.name}  {pattern.type} {string}\n')
                f.write(';\n')
        return 0
