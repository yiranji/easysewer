"""Configured pump/regulator fields and upstream crest adjustments.

Pinned SWMM 5.2.4 link.c/roadway.c semantics, evaluated on the structured graph.
These are not live controls, settings, flows or a model-runnable certificate.
"""

from dataclasses import fields
from datetime import timedelta

from ..model import geometry as g, network as n
from ..model.fields import validate_fields
from ..model.identity import Ref
from ..model.inspection import FieldFact, FieldSemantics
from ..model.options import get_options
from ..model.resources import Curve
from ..model.units import UnitContext
from ..model.values import Offset, Point
from ..validation import Diagnostic, ValidationReport
from .display_fields import coordinate_unit
from .field_contracts import FieldRule
from .geometry_profile import known, invalid, valid, section_height, native_length


def _na(reason):
    return FieldFact(status='not_applicable', reason=reason)


def _endpoint(store, ref):
    if not isinstance(ref, Ref) or ref.collection != 'swmm:nodes' or not store.contains(ref):
        return None
    node = store.collection(ref.collection)[ref.key]
    return node if isinstance(node, n.Node) and valid(node) else None


def regulator_offset(link, store, profile, *, internal=False):
    """Configured crest above the declared upstream invert, after adjustment."""
    if type(link) not in (n.Orifice, n.Weir, n.Outlet):
        return FieldFact(reason='Extension link requires its own crest contract')
    if type(link) is n.Outlet and type(link.rating) not in (n.FunctionalRating, n.TabularRating):
        return FieldFact(reason='Extension outlet rating requires its own consumer contract')
    inlet, outlet = _endpoint(store, link.inlet), _endpoint(store, link.outlet)
    if inlet is None or outlet is None:
        return invalid('Missing or invalid regulator endpoint')
    options = get_options(store)
    if not valid(link) or not valid(options):
        return invalid('Invalid regulator or options')
    value = link.crest_height if type(link) is n.Weir else link.offset
    mode = options.link_offsets or profile.option_default('link_offsets')
    units = UnitContext(flow_units=options.flow_units or profile.option_default('flow_units'))
    factor = native_length(units, profile) if internal else 1.
    if mode == 'ELEVATION':
        if value is None:
            return invalid('Absolute regulator offset requires an elevation or invert marker')
        height = 0. if value is Offset.NODE_INVERT else max(0., value / factor - inlet.elevation / factor)
    else:
        if value is Offset.NODE_INVERT:
            return invalid('Node-invert marker requires ELEVATION mode')
        height = max(0., (value or 0.) / factor)
    if (options.flow_routing or profile.option_default('flow_routing')) == 'DYNWAVE':
        if internal:
            if inlet.elevation / factor + height < outlet.elevation / factor:
                height = outlet.elevation / factor - inlet.elevation / factor
        else:
            height = max(height, outlet.elevation - inlet.elevation)
    return known(height, 'Height above declared upstream invert, clamped at zero and raised to downstream invert only for dynamic wave')


def regulator_crown(link, store, units, profile, *, internal=False):
    offset = regulator_offset(link, store, profile, internal=internal)
    if offset.status != 'known':
        return offset
    if type(link) is n.Outlet:
        height = known(1.e-6 if internal else 1.e-6 * native_length(units, profile), 'Outlet uses native DUMMY full depth')
    else:
        if link.section is None:
            return invalid('Regulator requires a cross-section')
        allowed = (g.Circular, g.RectClosed) if type(link) is n.Orifice else {
            'TRANSVERSE': (g.RectOpen,), 'SIDEFLOW': (g.RectOpen,), 'ROADWAY': (g.RectOpen,),
            'V-NOTCH': (g.Triangular,), 'TRAPEZOIDAL': (g.Trapezoidal,),
        }[link.weir_type]
        if type(link.section.geometry) not in allowed:
            return invalid('Cross-section is not applicable to this regulator')
        height = section_height(link.section, store, units, profile, internal=internal)
    return known(offset.value + height.value) if height.status == 'known' else height


def _curve(store, ref, kinds):
    if not store.contains(ref):
        return invalid('Missing curve reference')
    value = store.collection(ref.collection)[ref.key]
    if type(value) is not Curve:
        return FieldFact(reason='Extension curve requires its own consumer contract')
    if not valid(value) or not value.points or value.kind not in kinds:
        return invalid('Curve has an invalid type or point sequence for this consumer')
    return known(ref, 'Configured curve reference, not a time-varying lookup result')


def regulator_field(context):
    root, container, name, value = context.record, context.container, context.field, context.value
    options = get_options(context.store)
    issues = tuple(validate_fields(root)) + tuple(validate_fields(options))
    if not ValidationReport(diagnostics=issues).is_valid:
        return FieldSemantics(effective=invalid('Invalid regulator or options'), diagnostics=issues)
    units = UnitContext(flow_units=options.flow_units or context.profile.option_default('flow_units'))
    default = FieldFact(status='required')
    unit = _na('Non-numeric declaration')
    effective = known(value, 'Configured parameter, not a live simulation observation')
    dimension = next(f for f in fields(container) if f.name == name).metadata.get('dimension')
    if dimension == 'outlet_coefficient':
        exponent = container.exponent
        length = units.unit('length')
        label = units.flow_units if exponent == 0 else f'{units.flow_units}/{length}' if exponent == 1 else f'{units.flow_units}/{length}^{exponent}'
        unit = known(label, 'Outlet flow divided by head/depth raised to the declared exponent')
    elif dimension and dimension != 'map_coordinate':
        unit = known(units.unit('length' if dimension == 'offset' else dimension))
    if type(container) is Point or container is root and name == 'vertices':
        unit, map_issues = coordinate_unit(context)
        issues += map_issues
        if name == 'vertices':
            default = known(())
        if unit.status == 'invalid':
            effective = invalid('Invalid explicit map context')
    elif type(container) is Ref:
        if name == 'collection':
            default = _na('Reference namespace is fixed by the grammar')
        if not context.store.contains(container):
            effective = invalid('Missing referenced object')
    elif type(container) in (n.FunctionalRating, n.TabularRating):
        if name == 'basis':
            default = known('DEPTH', 'Omitted relation qualifier uses depth')
        elif name == 'curve':
            effective = _curve(context.store, value, ('RATING',))
        elif value < 0:
            effective = invalid('Negative functional rating parameters are not supported physical ratings')
    elif name in ('inlet', 'outlet'):
        if _endpoint(context.store, value) is None:
            effective = invalid('Missing or invalid endpoint node')
    elif name in ('offset', 'crest_height'):
        effective = regulator_offset(root, context.store, context.profile)
    elif name == 'section':
        fact = regulator_crown(root, context.store, units, context.profile)
        if fact.status != 'known':
            effective = fact
    elif type(root) is n.Pump:
        defaults = {'curve': None, 'initially_on': True, 'startup_depth': 0., 'shutoff_depth': 0.}
        if name in defaults:
            default = known(defaults[name])
            effective = known(default.value if value is None else value, 'Configured pump initial condition, before controls or automatic depth switching')
        if name == 'curve' and value is not None:
            effective = _curve(context.store, value, ('PUMP1','PUMP2','PUMP3','PUMP4','PUMP5'))
        if name in ('startup_depth','shutoff_depth') and (root.startup_depth or 0) > 0 and root.startup_depth <= (root.shutoff_depth or 0):
            effective = invalid('Positive startup depth must exceed shutoff depth')
    elif type(root) is n.Orifice:
        if name in ('gated','opening_time'):
            default = known(False if name == 'gated' else timedelta(0))
            effective = known(default.value if value is None else value)
        if name == 'opening_time':
            unit = known('s', 'Model duration; original INP token is decimal hours')
    elif type(root) is n.Weir:
        defaults = {'gated':False, 'end_contractions':0., 'end_coefficient':0., 'can_surcharge':True,
                    'road_width':0., 'road_surface':None, 'coefficient_curve':None}
        if name in defaults:
            default = known(defaults[name])
            effective = known(default.value if value is None else value)
        roadway = root.weir_type == 'ROADWAY'
        if roadway and name in ('gated','end_contractions','end_coefficient','can_surcharge','coefficient_curve'):
            effective = _na('ROADWAY flow calculation ignores this configured parameter')
        elif not roadway and name in ('road_width','road_surface'):
            default = effective = _na('Road geometry is only used by ROADWAY weirs')
        elif name == 'end_contractions' and root.weir_type not in ('TRANSVERSE','SIDEFLOW'):
            effective = _na('End contractions are only used by transverse and sideflow weirs')
        elif name == 'end_coefficient' and root.weir_type not in ('TRAPEZOIDAL','V-NOTCH'):
            effective = _na('End coefficient is used by trapezoidal flow, including a partially open V-notch')
        elif name == 'coefficient_curve' and value is not None:
            effective = _curve(context.store, value, ('WEIR',))
        elif name == 'coefficient':
            if roadway and (root.road_width or 0) > 0 and root.road_surface in ('PAVED','GRAVEL'):
                effective = _na('Positive road width and recognized surface select the native variable coefficient tables')
            elif not roadway and root.coefficient_curve is not None:
                curve = _curve(context.store, root.coefficient_curve, ('WEIR',))
                effective = _na('Configured coefficient curve supplies the head-dependent coefficient') if curve.status == 'known' else curve
    elif type(root) is n.Outlet:
        if name == 'gated':
            default = known(False)
            effective = known(False if value is None else value)
        elif name == 'rating' and type(value) not in (n.FunctionalRating, n.TabularRating):
            effective = FieldFact(reason='Extension outlet rating requires its own consumer contract')
    if effective.status == 'invalid':
        issues += (Diagnostic(code='regulator.field_context', object_id=root.id,
                              field='.'.join(map(str, context.path)), message=effective.reason),)
    return FieldSemantics(unit=unit, default=default, effective=effective, diagnostics=issues)


REGULATOR_FIELD_RULES = tuple(FieldRule(value_type=kind, field=f.name, root_type=root, resolve=regulator_field)
    for root in (n.Pump,n.Orifice,n.Weir,n.Outlet)
    for kind in (root,Ref,Point,*((n.FunctionalRating,n.TabularRating) if root is n.Outlet else ()))
    for f in fields(kind))
