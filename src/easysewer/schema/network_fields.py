"""Input and configured network facts; these are not live solver state.

The pinned link validation expands junction depth to adjacent crowns. Unsupported
geometry preprocessing remains an explicit unknown, never the input zero.
"""

from dataclasses import fields, replace

from ..model import geometry as g, network as n
from ..model.fields import validate_fields
from ..model.identity import Ref
from ..model.inspection import FieldFact, FieldSemantics
from ..model.options import get_options
from ..model.units import UnitContext
from ..model.values import Offset, Point
from ..validation import Diagnostic
from .display_fields import coordinate_unit
from .field_contracts import FieldRule
from .geometry_profile import section_height, native_length
from .regulator_fields import regulator_crown


def known(value, reason=''):
    return FieldFact(status='known', value=value, reason=reason)


def _node(store, ref):
    if not isinstance(ref, Ref) or ref.collection != 'swmm:nodes' or not store.contains(ref):
        return None
    value = store.collection(ref.collection)[ref.key]
    return value if not tuple(validate_fields(value)) else None


def _offset(link, name, store, mode, *, factor=1.):
    value = getattr(link, name)
    if mode == 'ELEVATION':
        node = _node(store, link.inlet if name == 'inlet_offset' else link.outlet)
        if node is None or value is None:
            return FieldFact(status='invalid', reason='Absolute offset requires a valid endpoint and an elevation or invert marker')
        height = 0.0 if value is Offset.NODE_INVERT else max(0.0, value / factor - node.elevation / factor)
    else:
        if value is Offset.NODE_INVERT:
            return FieldFact(status='invalid', reason='Node-invert marker requires ELEVATION mode')
        # None is a model/export default, not a legal omitted INP column.
        height = max(0.0, 0.0 if value is None else value / factor)
    if link.section is None:
        return FieldFact(status='invalid', reason='A conduit requires a cross-section')
    if type(link.section.geometry) is g.FilledCircular:
        height += link.section.geometry.filled_depth / factor
    return known(height, 'Effective height above the declared endpoint invert, including circular fill; solver orientation does not change this endpoint convention')


def node_depth(context, mode, *, links=None, internal=False):
    options = get_options(context.store)
    units = UnitContext(flow_units=options.flow_units or context.profile.option_default('flow_units'))
    factor = native_length(units, context.profile) if internal else 1.
    depth = (context.record.max_depth or 0.0) / factor
    if type(context.record) is n.Storage and not context.record.surcharge_depth:
        return known(depth, 'Open storage retains its declared maximum depth without adjacent crown expansion')
    adjacent = context.store.collection('swmm:links').values() if links is None else links
    from ..validation._cooperative import checkpointed
    for link in checkpointed(adjacent):
        if not isinstance(link.inlet, Ref) or not isinstance(link.outlet, Ref):
            return FieldFact(status='invalid', reason='Invalid network endpoint reference')
        if context.owner.canonical not in (link.inlet.canonical, link.outlet.canonical):
            continue
        if tuple(validate_fields(link)):
            return FieldFact(status='invalid', reason='Invalid adjacent link')
        if _node(context.store, link.inlet) is None or _node(context.store, link.outlet) is None:
            return FieldFact(status='invalid', reason='Missing or invalid adjacent endpoint')
        if type(link) is n.Pump or type(link) is n.Orifice and link.orientation == 'BOTTOM':
            continue
        if type(link) is not n.Conduit:
            if link.outlet.canonical == context.owner.canonical:
                continue  # Only conduits expand their downstream node.
            if type(link) not in (n.Orifice, n.Weir, n.Outlet):
                return FieldFact(reason='Extension link requires its own effective crown contract')
            crown = regulator_crown(link, context.store, units, context.profile, internal=internal)
            if crown.status != 'known':
                return crown
            depth = max(depth, crown.value)
            continue
        height = section_height(link.section, context.store, units, context.profile, internal=internal)
        if height.status != 'known':
            return height
        for name, end in (('inlet_offset', link.inlet), ('outlet_offset', link.outlet)):
            if end.canonical == context.owner.canonical:
                offset = _offset(link, name, context.store, mode, factor=factor)
                if offset.status != 'known':
                    return offset
                depth = max(depth, offset.value + height.value)
    return known(depth, 'Input maximum depth expanded to all supported adjacent link crowns; independent of internal conduit reversal')


def initial_depth_within_bound(context, mode, *, links=None):
    """Compare in internal feet in the pinned node.c operation order."""
    if context.profile.key != 'epa-swmm:5.2.4':
        return FieldFact(reason='Profile requires its own initial-depth validation contract')
    maximum = node_depth(context, mode, links=links, internal=True)
    if maximum.status != 'known':
        return maximum
    options = get_options(context.store)
    units = UnitContext(flow_units=options.flow_units or context.profile.option_default('flow_units'))
    factor = native_length(units, context.profile)
    value = context.record.initial_depth or 0.
    return known(value / factor <= maximum.value + (context.record.surcharge_depth or 0.) / factor)


def initial_depth_fact(context, mode, *, links=None):
    bound = initial_depth_within_bound(context, mode, links=links)
    if bound.status != 'known':
        return bound
    if not bound.value:
        return FieldFact(status='invalid', reason='Initial depth exceeds adjusted maximum plus surcharge in native internal feet')
    return known(context.record.initial_depth or 0., 'Initial depth satisfies the configured native depth bound')


def network_field(context):
    root, value, name = context.record, context.value, context.field
    options = get_options(context.store)
    issues = tuple(validate_fields(root)) + tuple(validate_fields(options))
    default = FieldFact(status='required')
    unit = FieldFact(status='not_applicable', reason='Non-numeric network declaration')
    effective = known(value, 'Configured value in model units; not a live simulation observation')
    mode = options.link_offsets or context.profile.option_default('link_offsets')
    if issues:
        return FieldSemantics(effective=FieldFact(status='invalid', reason='Invalid network record or options'), diagnostics=issues)
    units = UnitContext(flow_units=options.flow_units or context.profile.option_default('flow_units'))
    dimension = next(f for f in fields(context.container) if f.name == name).metadata.get('dimension')
    if dimension and dimension != 'map_coordinate':
        unit = known(units.unit('length' if dimension == 'offset' else dimension))
    if isinstance(context.container, Point) or name in ('position', 'vertices'):
        unit, issues = coordinate_unit(context)
        default = known(()) if name == 'vertices' else known(None) if name == 'position' else default
        if unit.status == 'invalid':
            effective = FieldFact(status='invalid', reason='Invalid explicit map context')
    elif isinstance(context.container, Ref):
        if name == 'collection':
            default = FieldFact(status='not_applicable', reason='Reference namespace is fixed by the network grammar')
    elif type(context.container) is n.ConduitLosses:
        # The entire omitted LOSSES statement uses these defaults as well.
        default = known(False if name == 'flap_gate' else 0.0, 'Native omitted LOSSES default')
        effective = known(default.value if value is None else value)
    elif type(root) is n.Junction:
        if name in ('max_depth', 'initial_depth', 'surcharge_depth', 'ponded_area'):
            default = known(0.0, 'Native optional JUNCTIONS input default')
            effective = known(0.0 if value is None else value)
        if name == 'max_depth':
            effective = node_depth(context, mode)
        elif name == 'initial_depth':
            effective = initial_depth_fact(context, mode)
        elif name == 'ponded_area' and not (options.allow_ponding or context.profile.option_default('allow_ponding')):
            effective = FieldFact(status='not_applicable', reason='Surface ponding is disabled; the declared area remains available')
    elif type(root) is n.Conduit:
        if name in ('initial_flow', 'maximum_flow'):
            default = known(0.0, 'Native optional CONDUITS input default')
            effective = known(0.0 if value is None else value,
                'Signed initial-flow setting in declared link direction' if name == 'initial_flow' else 'Zero means no imposed maximum-flow limit')
        elif name in ('inlet_offset', 'outlet_offset'):
            effective = _offset(root, name, context.store, mode)
        elif name == 'section':
            if value is None:
                effective = FieldFact(status='invalid', reason='A conduit requires a cross-section')
        elif name == 'losses':
            default = known(n.ConduitLosses(entry=0.0, exit=0.0, average=0.0, flap_gate=False, seepage=0.0))
            effective = known(default.value if value is None else replace(value,
                flap_gate=False if value.flap_gate is None else value.flap_gate,
                seepage=0.0 if value.seepage is None else value.seepage))
        elif name == 'roughness':
            # Keep the declared Manning coefficient separate from the friction
            # factors for force mains, meandering channels and lengthening.
            effective = known(value, 'Declared Manning n; not the derived full-flow/friction coefficient')
        elif name == 'length':
            effective = known(value, 'Declared physical length; not the solver lengthened computational length')
        if name in ('inlet', 'outlet') and _node(context.store, value) is None:
            effective = FieldFact(status='invalid', reason='Missing or invalid endpoint node')
    if effective.status == 'invalid':
        issues += (Diagnostic(code='network.field_context', object_id=root.id,
                              field='.'.join(map(str, context.path)), message=effective.reason),)
    return FieldSemantics(unit=unit, default=default, effective=effective, diagnostics=issues)


NETWORK_FIELD_RULES = tuple(FieldRule(value_type=kind, field=f.name, root_type=root, resolve=network_field)
    for root in (n.Junction, n.Conduit)
    for kind in (root, Point, *((Ref, n.ConduitLosses) if root is n.Conduit else ()))
    for f in fields(kind))
