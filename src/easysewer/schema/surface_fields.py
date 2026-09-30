"""Static transect, street and inlet inputs, separate from hydraulic state."""
from dataclasses import fields, replace

from ..model import surface as s, geometry as g, network as n
from ..model.fields import validate_fields
from ..model.identity import Ref
from ..model.inspection import FieldFact, FieldSemantics
from ..model.options import get_options
from ..model.resources import Curve
from ..model.units import UnitContext
from ..validation import Diagnostic, ValidationReport
from .field_contracts import FieldRule
from .hydrology_fields import known, invalid, na, reference
from .resource_fields import dimensions

DESIGNS = (s.GrateInlet, s.CurbInlet, s.CombinationInlet, s.SlottedInlet, s.CustomInlet)
STREET_DEFAULTS = dict(gutter_depression=0., gutter_width=0., sides=2,
                       backing_width=0., backing_slope=0., backing_roughness=0.)
USAGE_DEFAULTS = dict(count=1, percent_clogged=0., maximum_flow=0., local_depression=0.,
                      local_width=0., placement='AUTOMATIC')


def target(context, ref, types):
    fact = reference(context, ref)
    if fact.status != 'known':
        return fact
    row = context.store.collection(ref.collection)[ref.key]
    return known(row) if type(row) in types else FieldFact(reason='Extension target requires its own surface contract')


def design_context(context, design):
    if type(design) not in DESIGNS:
        return FieldFact(reason='Extension inlet design requires its own contract')
    if type(design) is s.CombinationInlet:
        for part in (design.grate, design.curb):
            fact = design_context(context, part)
            if fact.status != 'known':
                return fact
    if type(design) is s.GrateInlet and type(design.grate) not in (s.StandardGrate, s.GenericGrate):
        return FieldFact(reason='Extension grate requires its own contract')
    if type(design) is s.CustomInlet:
        fact = target(context, design.curve, (Curve,))
        if fact.status != 'known':
            return fact
        if fact.value.kind not in ('DIVERSION', 'RATING') or not fact.value.points:
            return invalid('Custom inlet requires a nonempty DIVERSION or RATING curve')
        fact = dimensions(replace(context, owner=design.curve, record=fact.value))
        if fact.status != 'known':
            return fact
    return known(design)


def surface_context(context):
    row = context.record
    if type(row) is s.Transect:
        if type(row.roughness) is not s.TransectRoughness or any(type(p) is not s.TransectPoint for p in row.stations):
            return FieldFact(reason='Extension transect value requires its own contract')
    elif type(row) is s.InletDesign:
        return design_context(context, row.design)
    elif type(row) is s.InletUsage:
        for ref, types in ((row.link, (n.Conduit,)), (row.inlet, (s.InletDesign,)),
                           (row.node, (n.Junction, n.Storage, n.Divider, n.Outfall))):
            fact = target(context, ref, types)
            if fact.status != 'known':
                return fact
        link = context.store.collection(row.link.collection)[row.link.key]
        design = context.store.collection(row.inlet.collection)[row.inlet.key].design
        fact = design_context(context, design)
        if fact.status != 'known':
            return fact
        if link.section is None:
            return invalid('Inlet requires a conduit cross-section')
        if type(link.section) is not g.CrossSection or type(link.section.geometry) not in g.BUILTIN_GEOMETRIES:
            return FieldFact(reason='Extension conduit geometry requires its own inlet contract')
        shape = link.section.geometry
        if type(design) is not s.CustomInlet:
            allowed = (g.RectOpen, g.Trapezoidal) if getattr(design, 'kind', '').startswith('DROP_') else (g.Street,)
            if type(shape) not in allowed:
                return invalid('Native would remove this inlet from an incompatible conduit shape')
        if type(shape) is g.Street:
            fact = target(context, shape.street, (s.StreetSection,))
            if fact.status != 'known':
                return fact
        if row.count is not None and row.count > 2147483647:
            return FieldFact(reason='Count exceeds the supported native signed integer range')
    return known(row)


def configured_design(value):
    if type(value) is s.CombinationInlet:
        return replace(value, grate=configured_design(value.grate), curb=configured_design(value.curb))
    if type(value) is s.GrateInlet and type(value.grate) is s.GenericGrate:
        return replace(value, grate=replace(value.grate, splash_velocity=value.grate.splash_velocity or 0.))
    if type(value) is s.CurbInlet and value.kind == 'CURB':
        return replace(value, throat=value.throat or 'VERTICAL')
    return value


def surface_field(context):
    root, container, name, value = context.record, context.container, context.field, context.value
    options = get_options(context.store)
    issues = tuple(validate_fields(root)) + tuple(validate_fields(options))
    if not ValidationReport(diagnostics=issues).is_valid:
        return FieldSemantics(effective=invalid('Invalid surface record or options'), diagnostics=issues)
    units = UnitContext(flow_units=options.flow_units or context.profile.option_default('flow_units'))
    unit = na('Non-numeric surface declaration')
    default = FieldFact(status='required')
    effective = known(value, 'Configured surface input, not hydraulic tables or live captured flow')
    dimension = next(f for f in fields(container) if f.name == name).metadata.get('dimension')
    if dimension == 'transect_elevation_offset':
        unit = known(units.unit('length'),
            f'Nominal input length; this profile applies the native length factor {context.profile.transect_offset_power} times to the offset. Engine-preserving conversion uses that power, not ordinary length scaling')
    elif dimension:
        unit = known(units.unit(dimension))
    fact = surface_context(context)
    if fact.status != 'known':
        effective = fact
    elif type(container) is Ref:
        if name == 'collection':
            default = na('Namespace selected by the declared surface reference role')
    elif type(root) is s.Transect:
        if container is root and name in ('meander_factor', 'width_factor'):
            effective = known(value or 1., 'Required INP slot: explicit zero selects factor one')
        elif type(container) is s.TransectRoughness:
            effective = known(value, 'Resolved NC input before this transect applies sqrt(meander); inheritance was resolved during decoding')
        elif name in ('stations', 'left_bank', 'right_bank') or type(container) is s.TransectPoint:
            effective = known(value, 'Original coordinates; width/offset and native exact bank comparisons remain engine operations')
        if name == 'stations' and len(context.path) == 2:
            default = na('Ordered station entry has no omitted-row default')
    elif type(root) is s.StreetSection:
        if name in STREET_DEFAULTS:
            default = known(STREET_DEFAULTS[name], 'Native omitted street parameter')
            effective = known(default.value if value is None else value)
        if name in ('backing_slope', 'backing_roughness') and not root.backing_width:
            default = effective = na('Native ignores backing slope and roughness at zero backing width')
    elif type(root) is s.InletUsage and name in USAGE_DEFAULTS:
        default = known(USAGE_DEFAULTS[name], 'Native omitted inlet usage parameter')
        effective = known(default.value if value is None else value,
            'Configured placement; AUTOMATIC is resolved by native bypass-node topology, not by this static query' if name == 'placement' else
            'Configured inlet parameter; clog fraction, effective width, flow limit and captured flow remain engine operations')
    elif type(root) is s.InletDesign:
        if type(value) in DESIGNS:
            effective = known(configured_design(value), 'Configured inlet design with representable omitted defaults')
        elif type(container) is s.CurbInlet and name == 'throat':
            if container.kind == 'DROP_CURB':
                default = effective = na('DROP_CURB does not read a throat parameter')
            else:
                default = known('VERTICAL'); effective = known(value or 'VERTICAL')
        elif type(container) is s.GenericGrate and name == 'splash_velocity':
            default = known(0.); effective = known(value or 0.)
        elif type(value) is s.GenericGrate:
            effective = known(replace(value, splash_velocity=value.splash_velocity or 0.))
    if effective.status in ('invalid', 'ambiguous'):
        issues += (Diagnostic(code='surface.field_context', message=effective.reason),)
    return FieldSemantics(unit=unit, default=default, effective=effective, diagnostics=issues)


TRANSECT_FIELD_RULES = tuple(FieldRule(value_type=t, field=f.name, root_type=s.Transect,
    resolve=surface_field, resolve_item=surface_field if f.name == 'stations' else None)
    for t in (s.Transect, s.TransectPoint, s.TransectRoughness) for f in fields(t))
SURFACE_FIELD_RULES = tuple(FieldRule(value_type=t, field=f.name, root_type=root, resolve=surface_field)
    for root, types in ((s.StreetSection, (s.StreetSection,)),
                        (s.InletDesign, (s.InletDesign, *DESIGNS, s.StandardGrate, s.GenericGrate, Ref)),
                        (s.InletUsage, (s.InletUsage, Ref)))
    for t in types for f in fields(t))
