"""
Urban Drainage Model (UDM) - Main Model Class

This module implements the core Urban Drainage Model functionality for hydraulic simulations.
It serves as the main interface for creating and managing drainage system models including
nodes (junctions, outfalls), links (conduits), subcatchment areas, and rainfall data.

The model supports:
- Reading/writing SWMM .inp files
- Managing network elements (nodes, links, areas)
- Handling rainfall and calculation settings
- Supporting various hydraulic elements like conduits and junctions
"""

from .Options import CalculationInformation
from .Link import LinkList
from .Node import NodeList
from .Area import AreaList
from .Rain import Rain
from .Curve import ValueList
import json
from .utils import get_swmm_inp_content


class UrbanDrainageModel:
    """
    Main class for managing an Urban Drainage Model.

    This class serves as the central point for managing all aspects of an urban drainage
    model including network topology, hydraulic elements, and simulation settings.

    Attributes:
        calc (CalculationInformation): Calculation and simulation settings
        link (LinkList): Collection of conduits and other hydraulic links
        node (NodeList): Collection of junctions, outfalls and other nodes
        area (AreaList): Collection of subcatchment areas
        rain (Rain): Rainfall data and settings
        value (ValueList): Curves and patterns for various model parameters
        label (dict): Model metadata and labeling information

    Args:
        model_path (str, optional): Path to SWMM .inp file to load. Defaults to None.
    """

    def __init__(self, model_path=None):
        # calculation related information
        self.calc = CalculationInformation()

        # entity related information
        self.link = LinkList()
        self.node = NodeList()
        self.area = AreaList()

        # rain related information
        self.rain = Rain()
        self.value = ValueList()

        # label information
        self.label = {}

        # read model from the file if provided
        if model_path is not None:
            self.read_inp(model_path)

    def __repr__(self):
        """Returns a string representation of the model showing key components"""
        return f'{self.link}, {self.node}, {self.area}'

    def to_inp(self, filename):
        """
        Writes the model to a SWMM .inp file.

        Args:
            filename (str): Path to the output .inp file

        Returns:
            int: 0 on success
        """
        with open(filename, 'w', encoding='utf-8') as f:
            # Write TITLE section first
            f.write('[TITLE]\n')
            if self.label:
                try:
                    f.write(json.dumps(self.label, indent=2))
                    f.write('\n\n')
                except:
                    f.write(str(self.label.get('TITLE', '')) + '\n\n')

        # Continue with other sections
        self.calc.write_to_swmm_inp(filename)
        self.node.write_to_swmm_inp(filename)
        self.link.write_to_swmm_inp(filename)
        self.area.write_to_swmm_inp(filename)
        self.rain.write_to_swmm_inp(filename)
        self.value.write_to_swmm_inp(filename)
        return 0

    def read_inp(self, filename):
        """
        Reads a SWMM .inp file and populates the model.

        Args:
            filename (str): Path to the input .inp file

        Returns:
            int: 0 on success
        """
        # Read TITLE section
        title_content = get_swmm_inp_content(filename, '[TITLE]')
        if title_content:
            try:
                # Try to parse as JSON
                json_text = '\n'.join(title_content)
                self.label = json.loads(json_text)
            except json.JSONDecodeError:
                # If not JSON, store as plain text
                self.label = {'TITLE': '\n'.join(title_content)}

        # Continue with other sections
        self.calc.read_from_swmm_inp(filename)
        self.node.read_from_swmm_inp(filename)
        self.link.read_from_swmm_inp(filename)
        self.area.read_from_swmm_inp(filename)
        self.rain.read_from_swmm_inp(filename)
        self.value.read_from_swmm_inp(filename)
        return 0
