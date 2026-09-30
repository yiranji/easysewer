"""LID configuration contracts, distinct from layer state and derived volumes."""
from dataclasses import fields, replace

from ..model import lid as l, hydrology as h, network as n, quality as q
from ..model.fields import validate_fields
from ..model.identity import Ref
from ..model.inspection import FieldFact, FieldSemantics
from ..model.options import get_options
from ..model.resources import Curve
from ..model.units import UnitContext
from ..model.values import FileReference
from ..validation import Diagnostic, ValidationReport
from .field_contracts import FieldRule
from .hydrology_fields import known, invalid, na, reference
from .resource_fields import dimensions

LAYERS = tuple(cls for _, cls in l.LAYERS.values())
LAYER_NAMES = tuple(name for name, _ in l.LAYERS.values())
NODES = (n.Junction, n.Storage, n.Divider, n.Outfall)


def target(context, ref, types):
    fact = reference(context, ref)
    if fact.status != 'known':
        return fact
    value = context.store.collection(ref.collection)[ref.key]
    return known(value) if type(value) in types else FieldFact(reason='Extension target requires its own LID contract')


def control_context(context, row):
    if type(row) is not l.LidControl or any(type(getattr(row, name)) is not cls
            for name, cls in l.LAYERS.values() if getattr(row, name) is not None) or any(type(v) is not l.LidRemoval for v in row.removals):
        return FieldFact(reason='Extension control, layer or removal requires its own LID contract')
    if not ValidationReport(diagnostics=tuple(validate_fields(row))).is_valid:
        return invalid('Invalid LID control')
    for removal in row.removals:
        fact = target(context, removal.pollutant, (q.Pollutant,))
        if fact.status != 'known':
            return fact
    if row.drain and row.drain.curve:
        fact = target(context, row.drain.curve, (Curve,))
        if fact.status != 'known':
            return fact
        if fact.value.kind != 'CONTROL' or not fact.value.points:
            return invalid('A LID drain requires a nonempty CONTROL curve')
        fact = dimensions(replace(context, owner=row.drain.curve, record=fact.value))
        if fact.status != 'known':
            return fact
    return known(row)


def drain_target(context, ref):
    fact = target(context, ref, (h.Subcatchment,) if ref.collection == 'swmm:subcatchments' else NODES)
    if fact.status != 'known':
        return fact
    if ref.collection == 'swmm:nodes' and context.store.contains(Ref(collection='swmm:subcatchments', key=ref.key)):
        return FieldFact(status='ambiguous', reason='Native drain names select a subcatchment before a node of the same name')
    return known(ref, 'Configured drain destination; no routing state is evaluated')


def usage_context(context, row, units):
    if type(row) not in (l.LidUsage, l.DisabledLidUsage):
        return FieldFact(reason='Extension deployment requires its own LID contract')
    if not ValidationReport(diagnostics=tuple(validate_fields(row))).is_valid:
        return invalid('Invalid LID deployment')
    fact = target(context, row.subcatchment, (h.Subcatchment,))
    if fact.status != 'known':
        return fact
    catchment = fact.value
    fact = target(context, row.control, (l.LidControl,))
    if fact.status != 'known':
        return fact
    control = fact.value
    fact = control_context(context, control)
    if fact.status != 'known':
        return fact
    if type(row) is l.DisabledLidUsage:
        return known(catchment)
    if row.report_file is not None and type(row.report_file) is not FileReference:
        return FieldFact(reason='Extension report reference requires its own LID contract')
    fact = drain_target(context, row.drain_to or catchment.outlet)
    if fact.status != 'known':
        return fact
    if control.kind == 'VS' and row.width <= 0:
        return invalid('Vegetative swale deployment requires positive width')
    peers = [v for v in context.store.collection('swmm:lid_usage').values()
             if isinstance(getattr(v, 'subcatchment', None), Ref) and v.subcatchment.canonical == row.subcatchment.canonical]
    if any(type(v) not in (l.LidUsage, l.DisabledLidUsage) for v in peers):
        return FieldFact(reason='Extension deployment requires its own group area/capture contract')
    if any(not ValidationReport(diagnostics=tuple(validate_fields(v))).is_valid for v in peers):
        return invalid('Invalid deployment in the same LID group')
    active = [v for v in peers if type(v) is l.LidUsage]
    area_factor = 1 / 2.2956e-5 if units.system == 'US' else .3048**2 / .92903e-5
    if sum(v.number * v.area for v in active) > catchment.area * area_factor * 1.001:
        return invalid('Replicated LID area exceeds the native catchment area tolerance')
    if sum(v.from_impervious for v in active) > 100.1 or sum(v.from_pervious or 0 for v in active) > 100.1:
        return invalid('LID group capture exceeds the native 100.1 percent tolerance')
    return known(catchment)


def configured_layer(row, layer):
    """Fill only representable defaults, keeping ratios and validation inputs."""
    if type(layer) is l.LidSurface:
        return replace(layer, vegetation_fraction=layer.vegetation_fraction if layer.storage_depth else 0.)
    if type(layer) is l.LidPavement:
        return replace(layer, regeneration_days=layer.regeneration_days or 0., regeneration_fraction=layer.regeneration_fraction or 0.)
    if type(layer) is l.LidStorage:
        return replace(layer, covered=layer.covered if layer.covered is not None else False)
    if type(layer) is l.LidDrain:
        return replace(layer, open_head=layer.open_head or 0., close_head=layer.close_head or 0.,
                       offset=layer.offset if row.storage and row.storage.thickness > 0 else 0.)
    return layer


def lid_field(context):
    root, container, name = context.record, context.container, context.field
    options = get_options(context.store)
    issues = tuple(validate_fields(options))
    if not ValidationReport(diagnostics=issues).is_valid:
        return FieldSemantics(effective=invalid('Invalid LID unit context'), diagnostics=issues)
    units = UnitContext(flow_units=options.flow_units or context.profile.option_default('flow_units'))
    unit = na('Non-numeric LID declaration')
    default = FieldFact(status='required')
    effective = known(context.value, 'Configured LID input, not live layer state or flow')
    def finish(fact):
        diagnostics = (Diagnostic(code='lid.field_context', message=fact.reason),) if fact.status in ('invalid', 'ambiguous') else ()
        return FieldSemantics(unit=unit, default=default, effective=fact, diagnostics=diagnostics)
    fact = control_context(context, root) if type(root) is l.LidControl else usage_context(context, root, units)
    if fact.status != 'known':
        return finish(fact)
    if type(container) is Ref:
        if name == 'collection':
            default = na('Namespace selected by the declared LID reference role')
    elif type(root) is l.DisabledLidUsage:
        if context.path[0] == 'parameters':
            default = na('Disabled input retains at least five uninterpreted tokens')
            effective = na('Zero replicates: native ignores these tokens without opening files or resolving drain names')
        elif name == 'record_id':
            default = na('Stable Model/JSON identity; INP assigns an ordinal identity on import')
    elif type(root) is l.LidUsage:
        catchment = fact.value
        if type(container) is FileReference:
            default = known({'base_directory': None, 'flavor': 'native', 'direction': 'output'}[name]) if name != 'path' else FieldFact(status='required')
            effective = known(context.value, 'Lexical output path; no file is opened or resolved')
        elif container is root:
            dimension = next(f for f in fields(root) if f.name == name).metadata.get('dimension')
            if dimension:
                unit = known(units.unit(dimension))
            if name == 'record_id':
                default = na('Stable Model/JSON identity; INP assigns an ordinal identity on import')
            elif name == 'from_pervious':
                default = known(0., 'Omitted pervious capture is zero percent')
                effective = known(root.from_pervious or 0.)
            elif name == 'report_file':
                default = known(None, 'No detailed report when omitted')
            elif name == 'drain_to':
                default = known(catchment.outlet, 'Omitted drain destination inherits the catchment outlet')
                effective = known(root.drain_to or catchment.outlet)
            elif name == 'to_pervious' and catchment.impervious_percent >= 99.9:
                effective = known(False, 'Native disables return to pervious area at >=99.9 percent impervious')
    elif container is root:
        if name in LAYER_NAMES:
            required = {'BC': ('soil',), 'RG': ('soil',), 'GR': ('soil', 'drain_mat'),
                        'PP': ('pavement',), 'IT': ('storage',), 'VS': ('surface',)}.get(root.kind, ())
            default = (FieldFact(status='required', reason=f'{root.kind} requires this layer') if name in required else
                       na('DRAINMAT is read only for a preceding GR declaration') if name == 'drain_mat' else
                       known(None, 'No explicit layer; native initialization and kind-specific processing apply'))
            effective = known(configured_layer(root, context.value),
                              'Configured layer with omitted tail defaults; no internal void-fraction or GR storage substitution')
        elif name == 'removals' and len(context.path) == 1:
            default = known((), 'Unlisted pollutants have zero drain removal')
    elif type(container) in (*LAYERS, l.LidRemoval):
        dimension = next(f for f in fields(container) if f.name == name).metadata.get('dimension')
        if dimension == 'lid_drain_coefficient':
            exponent = 0 if root.kind == 'RD' else container.exponent
            unit = known(units.unit('rain_intensity') if root.kind == 'RD' else
                         f"({units.unit('rain_intensity')})/({units.unit('rain_depth')})^{exponent:g}")
        elif dimension:
            unit = known(units.unit(dimension))
        resolved = configured_layer(root, container)
        effective = known(getattr(resolved, name), 'Configured parameter with representable native defaults')
        defaults = {l.LidPavement: {'regeneration_days': 0., 'regeneration_fraction': 0.},
                    l.LidStorage: {'covered': False}, l.LidDrain: {'open_head': 0., 'close_head': 0., 'curve': None}}
        if name in defaults.get(type(container), {}):
            default = known(defaults[type(container)][name], 'Native omitted layer-tail parameter')
        if type(container) in (l.LidPavement, l.LidStorage) and name == 'void_ratio':
            effective = known(context.value, 'Input void ratio; native converts to ratio/(1+ratio), then RB/GR may override its internal void fraction')
        elif name == 'clogging_factor':
            effective = known(context.value, 'Input clogging factor; native volume scaling is not an equivalent input value')
        elif type(container) is l.LidStorage and root.kind == 'GR':
            effective = known(getattr(resolved, name), 'Original storage input remains a validation/ordering input; GR derives internal storage later from its drainage mat')
        if type(container) is l.LidDrain:
            if root.kind in ('GR', 'VS') or root.kind == 'RD' and name != 'coefficient':
                effective = na('This LID process does not evaluate this conventional drain parameter')
            elif name == 'delay' and root.kind != 'RB':
                effective = na('Drain delay applies only to rain barrels')
        elif type(container) is l.LidStorage and name == 'covered' and root.kind != 'RB':
            effective = na('Only rain barrels use the cover flag to exclude rainfall')
    # Ordered removal tuple entries have configured values but no row default.
    if type(root) is l.LidControl and context.path[0] == 'removals' and len(context.path) == 2:
        default = na('A removal entry has no independent omitted-row default')
    return finish(effective)


LID_FIELD_RULES = tuple(FieldRule(value_type=t, field=f.name, root_type=root, resolve=lid_field,
    resolve_item=lid_field if f.name in ('removals', 'parameters') else None)
    for root, types in ((l.LidControl, (l.LidControl, *LAYERS, l.LidRemoval, Ref)),
                        (l.LidUsage, (l.LidUsage, Ref, FileReference)),
                        (l.DisabledLidUsage, (l.DisabledLidUsage, Ref)))
    for t in types for f in fields(t))
