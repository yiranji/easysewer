"""SWMM 5.2.4 configured geometry, without loading a native library.

Size tables are inches from EPA v5.2.4 src/solver/xsect.dat, SHA256
4a9a4685d34d64481ce4cd40cade155c2e421c61944c58e724a8063d0c0311fa.
Height preprocessing follows xsect.c, transect.c and street.c of that tag.
These facts do not certify the entire model as runnable.
"""

import math

from ..model import geometry as g
from ..model.fields import validate_fields
from ..model.inspection import FieldFact
from ..model.resources import Curve
from ..model.surface import StreetSection, Transect
from ..model.units import UnitContext
from ..validation import ValidationReport


ELLIPSE_MINOR = (14,19,22,24,27,29,32,34,38,43,48,53,58,63,68,72,77,82,87,92,97,106,116)
ELLIPSE_MAJOR = (23,30,34,38,42,45,49,53,60,68,76,83,91,98,106,113,121,128,136,143,151,166,180)
ARCH_HEIGHT = (
    11,13.5,15.5,18,22.5,26.625,31.3125,36,40,45,54,62,72,77.5,87.125,96.875,106.5,
    13,15,18,20,24,29,33,38,43,47,52,57,
    31,36,41,46,51,55,59,63,67,71,75,79,83,87,91,
    55,57,59,61,63,65,67,69,71,73,75,77,79,81,83,85,87,89,91,
    93,95,97,100,101,103,105,107,109,111,113,115,118,119,121,
    112,114,116,118,120,122,124,126,128,130,132,134,136,138,140,142,144,146,148,150,152,154,156,158,
)
ARCH_WIDTH = (
    18,22,26,28.5,36.25,43.75,51.125,58.5,65,73,88,102,115,122,138,154,168.75,
    17,21,24,28,35,42,49,57,64,71,77,83,
    40,46,53,60,66,73,81,87,95,103,112,117,128,137,142,
    73,76,81,84,87,92,95,98,103,106,112,114,117,123,128,131,137,139,
    142,148,150,152,154,161,167,169,171,178,184,186,188,190,197,199,
    159,162,168,170,173,179,184,187,190,195,198,204,206,209,215,217,223,225,231,234,236,239,245,247,
)


def known(value, reason=''):
    return FieldFact(status='known', value=value, reason=reason)


def invalid(reason):
    return FieldFact(status='invalid', reason=reason)


def valid(value):
    return ValidationReport(diagnostics=tuple(validate_fields(value))).is_valid


def native_length(units, profile):
    return UnitContext().convert(1., dimension='length', to=units, rules=profile.unit_rules)


def standard_dimensions(shape, units, profile, *, internal=False):
    """Effective depth/width, independent of how the size code was written."""
    tables = {g.HorizontalEllipse: (ELLIPSE_MINOR, ELLIPSE_MAJOR),
              g.VerticalEllipse: (ELLIPSE_MAJOR, ELLIPSE_MINOR), g.Arch: (ARCH_HEIGHT, ARCH_WIDTH)}
    if type(shape) not in tables or profile.key != 'epa-swmm:5.2.4':
        return FieldFact(reason='No standard-size table declared for this geometry/profile')
    if not valid(shape):
        return invalid('Invalid standard-size geometry')
    if shape.size_code is None:
        return known((shape.full_depth / native_length(units, profile), shape.width / native_length(units, profile)) if internal else (shape.full_depth, shape.width))
    heights, widths = tables[type(shape)]
    if not 1 <= shape.size_code <= len(heights):
        return invalid(f'Standard size code must be between 1 and {len(heights)}')
    factor = 1. if internal else native_length(units, profile)
    index = shape.size_code - 1
    return known((heights[index] / 12. * factor, widths[index] / 12. * factor),
                 'Pinned native table in inches, converted from internal feet to model units')


def _resource(store, ref, kind):
    if not store.contains(ref):
        return None, invalid('Missing geometry resource')
    resource = store.collection(ref.collection)[ref.key]
    if type(resource) is not kind:
        return None, FieldFact(reason='Extension resource requires its own geometry preprocessing contract')
    if not valid(resource):
        return None, invalid('Invalid geometry resource')
    return resource, None


def section_height(section, store, units, profile, *, internal=False):
    """Effective full depth used to raise a conduit endpoint's node crown."""
    if section is None or not valid(section):
        return invalid('Missing or invalid adjacent cross-section')
    shape = section.geometry
    if type(shape) not in g.BUILTIN_GEOMETRIES or profile.key != 'epa-swmm:5.2.4':
        return FieldFact(reason='Geometry/profile requires its own effective crown contract')
    factor = native_length(units, profile)
    result_factor = 1. if internal else factor
    if type(shape) is g.Dummy:
        return known(1.e-6 * result_factor, 'Native DUMMY full depth is TINY internal feet')
    if type(shape) in (g.Circular, g.ForceMain):
        return known(shape.diameter / factor if internal else shape.diameter)
    if type(shape) is g.FilledCircular:
        return known(shape.diameter / factor - shape.filled_depth / factor if internal else shape.diameter - shape.filled_depth)
    if type(shape) in (g.HorizontalEllipse, g.VerticalEllipse, g.Arch):
        dimensions = standard_dimensions(shape, units, profile, internal=internal)
        return known(dimensions.value[0], dimensions.reason) if dimensions.status == 'known' else dimensions
    if type(shape) is g.Irregular:
        resource, error = _resource(store, shape.transect, Transect)
        if error is not None:
            return error
        # Preserve native operation order, including the pinned double
        # conversion of the offset: large offsets can erase finite depths.
        offset = resource.elevation_offset / factor ** (profile.transect_offset_power - 1)
        levels = tuple((point.elevation + offset) / factor for point in resource.stations)
        height = (max(levels) - min(levels)) * result_factor
    elif type(shape) is g.Street:
        resource, error = _resource(store, shape.street, StreetSection)
        if error is not None:
            return error
        width = resource.crown_width / factor
        gutter_width = (resource.gutter_width or 0.) / factor
        gutter_depth = (resource.gutter_depression or 0.) / factor
        curb = resource.curb_height / factor
        slope = resource.cross_slope / 100.
        backing = (resource.backing_width or 0.) / factor
        backing_slope = (resource.backing_slope or 0.) / 100. if backing else 0.
        y3 = gutter_depth + slope * gutter_width
        y1 = curb + gutter_depth
        height = max(backing_slope * backing + y1, y3 + slope * (width - gutter_width)) * result_factor
    elif type(shape) is g.Custom:
        resource, error = _resource(store, shape.curve, Curve)
        if error is not None:
            return error
        if resource.kind != 'SHAPE' or not resource.points:
            return invalid('Custom cross-section requires a nonempty SHAPE curve')
        height = shape.full_depth / factor if internal else shape.full_depth
    else:
        height = shape.full_depth / factor if internal else shape.full_depth
    if not math.isfinite(height) or height <= 0:
        return invalid('Native preprocessing produces no finite positive full depth')
    return known(height, 'Configured native geometry full depth in internal feet' if internal else 'Configured native geometry full depth in model units')
