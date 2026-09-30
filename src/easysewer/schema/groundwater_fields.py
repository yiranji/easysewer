"""Groundwater input inheritance and empirical expression field contracts."""
from dataclasses import fields

from ..model import groundwater as g, expressions as e, hydrology as h, network as n
from ..model.fields import validate_fields
from ..model.identity import Ref
from ..model.inspection import FieldFact, FieldSemantics
from ..model.options import get_options
from ..model.resources import Pattern
from ..model.units import UnitContext
from ..validation import Diagnostic, ValidationReport
from .field_contracts import FieldRule
from .hydrology_fields import known, invalid, na, reference

EXPRESSION_TYPES = (e.ExpressionNumber, e.UnaryExpression, e.BinaryExpression,
                    e.FunctionExpression, g.GroundwaterVariable)
INHERITED = ('bottom_elevation', 'water_table_elevation', 'upper_moisture')


def target(context, ref, types):
    fact = reference(context, ref)
    if fact.status != 'known':
        return fact
    row = context.store.collection(ref.collection)[ref.key]
    return known(row) if type(row) in types else FieldFact(reason='Extension target requires its own groundwater contract')


def aquifer_context(context, aquifer):
    if type(aquifer) is not g.Aquifer:
        return FieldFact(reason='Extension aquifer requires its own groundwater contract')
    if not ValidationReport(diagnostics=tuple(validate_fields(aquifer))).is_valid:
        return invalid('Invalid aquifer configuration')
    if aquifer.evaporation_pattern is not None:
        fact = target(context, aquifer.evaporation_pattern, (Pattern,))
        if fact.status != 'known':
            return fact
        if fact.value.kind != 'MONTHLY':
            return invalid('Aquifer evaporation requires a MONTHLY pattern')
    return known(aquifer)


def binding_context(context, row, units):
    if type(row) is not g.Groundwater:
        return FieldFact(reason='Extension binding requires its own groundwater contract')
    if not ValidationReport(diagnostics=tuple(validate_fields(row))).is_valid:
        return invalid('Invalid groundwater binding')
    for ref, types in ((row.subcatchment, (h.Subcatchment,)),
                       (row.node, (n.Junction, n.Storage, n.Divider, n.Outfall)),
                       (row.aquifer, (g.Aquifer,))):
        fact = target(context, ref, types)
        if fact.status != 'known':
            return fact
    aquifer = fact.value
    fact = aquifer_context(context, aquifer)
    if fact.status != 'known':
        return fact
    native_length = UnitContext().convert(1., dimension='length', to=units, rules=context.profile.unit_rules)
    for name in ('threshold_elevation', *INHERITED):
        value = getattr(row, name)
        if value is not None and value / (1. if name == 'upper_moisture' else native_length) == -1.e10:
            return invalid('Numeric native missing sentinel must be represented by None')
    bottom, table, moisture = (getattr(row, name) if getattr(row, name) is not None else getattr(aquifer, name)
                               for name in INHERITED)
    if not bottom <= table <= row.surface_elevation or bottom >= row.surface_elevation:
        return invalid('Local water table requires bottom <= table <= surface and positive aquifer depth')
    if not aquifer.wilting_point <= moisture <= aquifer.porosity:
        return invalid('Local moisture must be between aquifer wilting point and porosity')
    return known(aquifer)


def groundwater_field(context):
    root, container, name = context.record, context.container, context.field
    if type(root) is g.GroundwaterExpression and any(type(node) not in EXPRESSION_TYPES for node in e.walk_expression(root.expression)):
        return FieldSemantics(effective=FieldFact(reason='Extension arithmetic requires its own groundwater contract'))
    options = get_options(context.store)
    issues = tuple(validate_fields(root)) + tuple(validate_fields(options))
    if not ValidationReport(diagnostics=issues).is_valid:
        return FieldSemantics(effective=invalid('Invalid groundwater declaration or options'), diagnostics=issues)
    units = UnitContext(flow_units=options.flow_units or context.profile.option_default('flow_units'))
    unit = na('Non-numeric groundwater declaration')
    default = FieldFact(status='required')
    effective = known(context.value, 'Configured groundwater input, not a runtime soil or water state')
    def finish(fact):
        diagnostics = (Diagnostic(code='groundwater.field_context', message=fact.reason),) if fact.status in ('invalid', 'ambiguous') else ()
        return FieldSemantics(unit=unit, default=default, effective=fact, diagnostics=diagnostics)
    if type(root) is g.Aquifer:
        fact = aquifer_context(context, root)
    elif type(root) is g.Groundwater:
        fact = binding_context(context, root, units)
    else:
        fact = target(context, root.subcatchment, (h.Subcatchment,))
        if fact.status == 'known':
            binding = context.store.collection('swmm:groundwater').get(root.subcatchment.key)
            fact = na('Expression has no groundwater binding and is inactive') if binding is None else binding_context(context, binding, units)
    if fact.status != 'known':
        return finish(fact)
    aquifer = fact.value
    if type(container) is Ref:
        if name == 'collection':
            default = na('Reference namespace fixed by the groundwater grammar')
    elif type(root) is g.GroundwaterExpression:
        if container is root and name == 'expression':
            unit = known(units.unit('groundwater_flux' if root.kind == 'LATERAL' else 'rain_intensity'))
        elif type(container) is g.GroundwaterVariable:
            unit = known(units.unit(g.VARIABLE_DIMENSIONS[container.name]), 'Units of the native variable represented by this symbol')
        elif type(context.value) is g.GroundwaterVariable:
            unit = known(units.unit(g.VARIABLE_DIMENSIONS[context.value.name]))
        elif type(container) in EXPRESSION_TYPES and name not in ('operator', 'function'):
            unit = FieldFact(reason='Empirical constant or subexpression has no independently declared unit; no dimensional inference is imposed')
    elif container is root:
        dimension = next(f for f in fields(root) if f.name == name).metadata.get('dimension')
        if name in ('groundwater_coefficient', 'surface_water_coefficient', 'interaction_coefficient'):
            exponent = (root.groundwater_exponent if name == 'groundwater_coefficient' else
                        root.surface_water_exponent if name == 'surface_water_coefficient' else 2)
            unit = known(f"({units.unit('groundwater_flux')})/({units.unit('length')})^{exponent:g}",
                         'Groundwater flux uses cfs/acre or cms/ha independently of the selected flow-unit scale')
        elif dimension:
            unit = known(units.unit(dimension))
        if type(root) is g.Aquifer and name == 'evaporation_pattern':
            default = known(None, 'No monthly adjustment pattern when omitted')
        elif type(root) is g.Groundwater:
            if name in INHERITED:
                default = known(getattr(aquifer, name), 'Inherited from the referenced aquifer when omitted')
                effective = known(context.value if context.value is not None else default.value,
                                  'Resolved input; native saturation clamps are initialization state and are not written back')
            elif name == 'threshold_elevation':
                default = known(context.store.collection(root.node.collection)[root.node.key].elevation,
                                'Receiving node invert elevation when omitted')
                effective = known(context.value if context.value is not None else default.value)
            elif name == 'fixed_surface_depth' and root.fixed_surface_depth == 0:
                effective = known(0., 'Zero selects the live receiving-node depth; this query does not evaluate that runtime depth')
    return finish(effective)


GROUNDWATER_FIELD_RULES = tuple(FieldRule(value_type=t, field=f.name, root_type=root, resolve=groundwater_field)
    for root, types in ((g.Aquifer, (g.Aquifer, Ref)), (g.Groundwater, (g.Groundwater, Ref)),
                        (g.GroundwaterExpression, (g.GroundwaterExpression, Ref, *EXPRESSION_TYPES)))
    for t in types for f in fields(t))
