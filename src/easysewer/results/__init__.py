"""Immutable result values and extensible variable definitions; no native imports."""

from .series import ResultSource, ResultSeries, SampleMaximum
from .variables import OutputVariable, OutputVariables, swmm_output_variables
from .tables import ResultCell, ResultColumn, ResultRow, ResultTable
from .applicability import ResultApplicability, ResultContext

__all__ = ['ResultSource', 'ResultSeries', 'SampleMaximum', 'OutputVariable',
           'OutputVariables', 'swmm_output_variables', 'ResultCell', 'ResultColumn',
           'ResultRow', 'ResultTable', 'ResultApplicability', 'ResultContext']
