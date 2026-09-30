"""Pollutant and inflow configuration, independent of instantaneous loading."""
from dataclasses import fields, replace

from ..model import inflows as i, quality as q, network as n
from ..model.fields import validate_fields
from ..model.identity import Ref
from ..model.inspection import FieldFact, FieldSemantics
from ..model.options import get_options
from ..model.resources import Pattern, InlineTimeSeries, FileTimeSeries
from ..model.units import UnitContext
from ..validation import Diagnostic, ValidationReport
from .field_contracts import FieldRule
from .hydrology_fields import known, invalid, na, reference
from .resource_fields import dimensions

EXTERNAL = (i.FlowInflow, i.ConcentrationInflow, i.MassInflow)
DRY = (i.DryWeatherFlow, i.DryWeatherConcentration)


def target(context, ref, types):
    fact = reference(context, ref)
    if fact.status != 'known':
        return fact
    row = context.store.collection(ref.collection)[ref.key]
    return known(row) if type(row) in types else FieldFact(reason='Extension resource requires its own inflow contract')


def finish(unit, default, effective):
    issues = ()
    if effective.status in ('invalid', 'ambiguous'):
        issues = (Diagnostic(code='inflow.field_context', message=effective.reason),)
    return FieldSemantics(unit=unit, default=default, effective=effective, diagnostics=issues)


def pollutant_field(context):
    root, container, name = context.record, context.container, context.field
    issues = tuple(validate_fields(root))
    if not ValidationReport(diagnostics=issues).is_valid:
        return FieldSemantics(effective=invalid('Invalid pollutant declaration'), diagnostics=issues)
    unit = na('Non-numeric pollutant declaration')
    default = FieldFact(status='required')
    effective = known(context.value, 'Configured pollutant input, not simulated concentration')
    other = None
    if root.co_pollutant is not None:
        checked = target(context, root.co_pollutant, (q.Pollutant,))
        if checked.status != 'known':
            return finish(unit, default, checked)
        other = checked.value
    if container is root:
        defaults = dict(snow_only=False, co_pollutant=None, co_fraction=0., dwf_concentration=0., initial_concentration=0.)
        if name in defaults:
            default = known(defaults[name], 'Native omitted pollutant parameter')
            if context.value is None:
                effective = default
        dimension = next(f for f in fields(root) if f.name == name).metadata.get('dimension')
        if dimension == 'concentration':
            unit = known(UnitContext().unit(q.concentration_dimension(root)))
        elif name == 'co_fraction':
            if other is None:
                unit = na('No co-pollutant conversion'); effective = na('No co-pollutant is assigned')
            else:
                unit = known('1' if root.units == other.units else f'{root.units} per {other.units}',
                             'Native combines numerical concentrations without automatic cross-pollutant unit conversion')
        elif dimension:
            unit = known(UnitContext().unit(dimension))
    elif name == 'collection':
        default = na('Reference namespace fixed by the pollutant grammar')
    return finish(unit, default, effective)


def inflow_field(context):
    root, container, name = context.record, context.container, context.field
    options = get_options(context.store)
    issues = tuple(validate_fields(root)) + tuple(validate_fields(options))
    if not ValidationReport(diagnostics=issues).is_valid:
        return FieldSemantics(effective=invalid('Invalid inflow or options'), diagnostics=issues)
    unit = na('Non-numeric inflow declaration')
    default = FieldFact(status='required')
    effective = known(context.value, 'Configured inflow input, not a date-selected flow or pollutant load')
    node = target(context, root.node, (n.Junction, n.Storage, n.Divider, n.Outfall))
    if node.status != 'known':
        return finish(unit, default, node)
    pollutant = None
    if root.constituent is not i.FLOW:
        fact = target(context, root.constituent, (q.Pollutant,))
        if fact.status != 'known':
            return finish(unit, default, fact)
        pollutant = fact.value
    if type(root) is i.ConcentrationInflow and type(node.value) is not n.Outfall:
        flow = context.store.collection('swmm:inflows').get((root.node.key, 'FLOW'))
        if flow is None or not ValidationReport(diagnostics=tuple(validate_fields(flow))).is_valid:
            return finish(unit, default, invalid('External concentration requires valid external FLOW at the same node'))
        if type(flow) is not i.FlowInflow:
            return finish(unit, default, FieldFact(reason='Extension FLOW requires its own concentration contract'))
    patterns = (root.pattern,) if type(root) in EXTERNAL else root.patterns
    kinds = []
    for ref in patterns:
        if ref is None:
            kinds.append(None); continue
        fact = target(context, ref, (Pattern,))
        if fact.status != 'known':
            return finish(unit, default, fact)
        kinds.append(fact.value.kind)
    if type(root) in EXTERNAL and root.series is not None:
        fact = target(context, root.series, (InlineTimeSeries, FileTimeSeries))
        if fact.status != 'known':
            return finish(unit, default, fact)
        axes = dimensions(replace(context, owner=root.series, record=fact.value))
        if axes.status != 'known':
            return finish(unit, default, axes)
    if type(container) is Ref:
        if name == 'collection':
            default = na('Reference namespace fixed by the inflow grammar')
    elif name == 'baseline':
        dimension = ('flow' if pollutant is None else 'external_mass_rate' if type(root) is i.MassInflow
                     else q.concentration_dimension(pollutant))
        unit = known(UnitContext(flow_units=options.flow_units or context.profile.option_default('flow_units')).unit(dimension))
        if type(root) in EXTERNAL:
            default = known(0., 'Omitted external baseline is zero')
            effective = known(root.baseline if root.baseline is not None else 0.,
                              'Configured baseline; scale_factor applies only to the time-series term')
    elif name in ('scale_factor', 'mass_factor'):
        default = known(1., 'Native omitted factor is one')
        effective = known(context.value if context.value is not None else 1.)
        if name == 'mass_factor':
            unit = known({'MG/L': 'mg/s', 'UG/L': 'ug/s', '#/L': 'count/s'}[pollutant.units] + ' per user mass-rate unit',
                         'Configured conversion before native internal liters-per-cubic-foot scaling')
        else:
            unit = known('1')
            if root.series is None:
                effective = na('No series term to scale; the baseline is not scaled')
    elif name in ('series', 'pattern'):
        default = known(None, 'No configured resource when omitted')
    if type(root) in DRY and context.path[0] == 'patterns':
        last = {kind: index for index, kind in enumerate(kinds) if kind is not None}
        active = tuple(ref if kind is not None and last[kind] == index else None
                       for index, (kind, ref) in enumerate(zip(kinds, root.patterns)))
        if len(context.path) == 1:
            default = known((), 'No DWF patterns when omitted')
            effective = known(active, 'Original slots with inactive entries cleared; WEEKEND replaces HOURLY only on weekends')
        else:
            index = context.path[1]
            if len(context.path) == 2:
                default = known(None, 'An empty optional slot selects no pattern')
            if active[index] is None:
                effective = na('Empty slot or superseded by a later pattern of the same kind')
    return finish(unit, default, effective)


POLLUTANT_FIELD_RULES = tuple(FieldRule(value_type=t, field=f.name, root_type=q.Pollutant, resolve=pollutant_field)
    for t in (q.Pollutant, Ref) for f in fields(t))
INFLOW_FIELD_RULES = tuple(FieldRule(value_type=t, field=f.name, root_type=root, resolve=inflow_field,
    resolve_item=inflow_field if f.name == 'patterns' else None)
    for root in EXTERNAL + DRY for t in (root, Ref) for f in fields(t))
