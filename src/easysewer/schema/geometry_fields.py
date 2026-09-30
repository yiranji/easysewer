"""Declared, default and effective cross-section parameters in owner context."""

from dataclasses import fields

from ..model import geometry as g, network as n
from ..model.fields import validate_fields
from ..model.inspection import FieldFact, FieldSemantics
from ..model.options import get_options
from ..model.units import UnitContext
from ..validation import Diagnostic, ValidationReport
from .field_contracts import FieldRule
from .geometry_profile import known, invalid, standard_dimensions


def geometry_field(context):
    root, container, name, value = context.record, context.container, context.field, context.value
    options = get_options(context.store)
    issues = tuple(validate_fields(root)) + tuple(validate_fields(options))
    if not ValidationReport(diagnostics=issues).is_valid:
        return FieldSemantics(effective=invalid('Invalid cross-section owner or options'), diagnostics=issues)
    units = UnitContext(flow_units=options.flow_units or context.profile.option_default('flow_units'))
    default = FieldFact(status='required')
    effective = known(value, 'Configured cross-section parameter; not a live solver observation')
    dimension = next(f for f in fields(container) if f.name == name).metadata.get('dimension')
    if dimension == 'force_main_roughness':
        equation = options.force_main_equation or context.profile.option_default('force_main_equation')
        dimension = 'rain_depth' if equation == 'D-W' else 'ratio'
    unit = known(units.unit(dimension)) if dimension else FieldFact(status='not_applicable')
    regulator = type(root) in (n.Orifice, n.Weir)
    shape = root.section.geometry
    if regulator:
        allowed = (g.Circular, g.RectClosed) if type(root) is n.Orifice else {
            'TRANSVERSE': (g.RectOpen,), 'SIDEFLOW': (g.RectOpen,), 'ROADWAY': (g.RectOpen,),
            'V-NOTCH': (g.Triangular,), 'TRAPEZOIDAL': (g.Trapezoidal,),
        }[root.weir_type]
        if type(shape) not in allowed:
            effective = invalid('Cross-section shape is not applicable to this regulator')
    if effective.status == 'invalid':
        pass
    elif type(container) is g.CrossSection:
        if name in ('barrels', 'culvert'):
            if regulator or type(shape) in (g.Irregular, g.Street):
                default = effective = FieldFact(status='not_applicable', reason='Native reader does not consume this parameter for this owner/shape')
            else:
                default = known(1 if name == 'barrels' else 0)
                effective = known(default.value if value is None else value,
                                  'Configured barrel count' if name == 'barrels' else 'Configured culvert code; zero disables inlet control')
                if name == 'barrels' and value is not None and value > 127:
                    effective = FieldFact(reason='Native stores barrels in char; counts above 127 are not portable and can wrap or be rejected')
                if name == 'culvert' and value is not None and value > 2147483647:
                    effective = FieldFact(reason='Culvert code exceeds the supported native signed integer range')
    elif type(container) in (g.HorizontalEllipse, g.VerticalEllipse, g.Arch):
        dimensions = standard_dimensions(container, units, context.profile)
        if dimensions.status != 'known':
            effective = dimensions
        elif name in ('full_depth', 'width'):
            effective = known(dimensions.value[0 if name == 'full_depth' else 1], dimensions.reason)
            if container.size_code is not None:
                default = FieldFact(status='not_applicable', reason='Dimensions are selected by the standard size code')
        elif container.size_code is None:
            default = FieldFact(status='not_applicable', reason='Explicit dimensions do not select a standard size code')
            effective = known(None)
    elif type(container) is g.RectOpen and name == 'ignored_sides':
        if regulator:
            default = effective = FieldFact(status='not_applicable', reason='Native regulator excludes no rectangular sides')
        else:
            # All four numeric INP slots are required. A Python None is
            # exported as zero; it is not a legal omitted input column.
            effective = known(0. if value is None else value)
    elif type(container) in (g.RectRound, g.ModifiedBasketHandle) and name in ('bottom_radius', 'top_radius'):
        width = container.top_width if type(container) is g.RectRound else container.bottom_width
        effective = known(max(value, width / 2.), 'Native radius is raised to at least half the width')
    elif name in ('curve', 'transect', 'street') and not context.store.contains(value):
        effective = invalid('Missing cross-section resource')
    if effective.status == 'invalid':
        issues += (Diagnostic(code='geometry.field_context', object_id=root.id,
                              field='.'.join(map(str, context.path)), message=effective.reason),)
    return FieldSemantics(unit=unit, default=default, effective=effective, diagnostics=issues)


GEOMETRY_FIELD_RULES = tuple(FieldRule(value_type=kind, field=f.name, root_type=root, resolve=geometry_field)
    for root in (n.Conduit, n.Orifice, n.Weir)
    for kind in (g.CrossSection, *g.BUILTIN_GEOMETRIES)
    for f in fields(kind))
