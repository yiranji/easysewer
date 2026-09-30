"""Shared resource configuration, with consumer-declared ordinate dimensions.

These facts do not evaluate interpolation, pattern scheduling or external files.
Calendar facts use Python model-local timestamps, not native floating DateTime.
"""

from dataclasses import fields, replace
from datetime import datetime, time, timedelta

from ..model.fields import validate_fields
from ..model.inspection import FieldFact, FieldSemantics
from ..model.options import get_options
from ..model.resources import (CURVE_DIMENSIONS, PATTERN_LENGTHS, Curve, CurvePoint,
    FileTimeSeries, InlineTimeSeries, Pattern, SeriesPoint)
from ..model.units import UnitContext
from ..model.values import FileReference
from ..validation import Diagnostic, ValidationReport
from .field_contracts import FieldRule
from .option_profile import clock_duration


def known(value, reason='Configured resource value; not a live consumer lookup'):
    return FieldFact(status='known', value=value, reason=reason)


def na(reason):
    return FieldFact(status='not_applicable', reason=reason)


def dimensions(context):
    """Use all registered consumers, including independently registered codecs."""
    root = context.record
    intrinsic = CURVE_DIMENSIONS.get(root.kind) if type(root) is Curve else None
    width = 2 if type(root) is Curve else 1
    uses = context.resource_uses(context.owner) if context.resource_uses is not None else ()
    references = {(use.owner.canonical, use.path) for use in context.store.referenced_by(context.owner)}
    declared = {(use.owner.canonical, use.path) for use in uses}
    if declared - references:
        return FieldFact(status='invalid', reason='Consumer declarations must identify actual references')
    if any(use.accepted_kinds and getattr(root, 'kind', None) not in use.accepted_kinds for use in uses):
        return FieldFact(status='invalid', reason='Resource kind is incompatible with a registered consumer')
    if any(use.dimensions and len(use.dimensions) != width for use in uses):
        return FieldFact(status='invalid', reason='Consumer dimension count does not match the resource axes')
    declared_dimensions = {use.dimensions for use in uses if use.dimensions}
    if intrinsic is not None:
        declared_dimensions.add(intrinsic)
    if len(declared_dimensions) > 1:
        return FieldFact(status='ambiguous', reason='Shared resource consumers disagree on dimensions')
    if intrinsic is None and (not uses or references != declared or any(not use.dimensions for use in uses)):
        return FieldFact(reason='Every consumer must declare dimensions; unbound resources have no inferred units')
    return known(next(iter(declared_dimensions)), 'Intrinsic curve axes' if intrinsic else 'All registered consumers agree')


def units(context, options):
    fact = dimensions(context)
    if fact.status != 'known':
        return fact
    try:
        unit_context = UnitContext(flow_units=options.flow_units or context.profile.option_default('flow_units'))
        return known(tuple(unit_context.unit(d) for d in fact.value), fact.reason)
    except ValueError:
        return FieldFact(reason='A declared consumer dimension has no registered unit label')


def resolved_points(context, options):
    try:
        start = datetime.combine(options.start_date or context.profile.option_default('start_date'), time()) + clock_duration(
            options.start_time if options.start_time is not None else context.profile.option_default('start_time'))
        points = tuple(replace(p, time=start + p.time) if isinstance(p.time, timedelta) else p for p in context.record.points)
    except OverflowError:
        return FieldFact(status='invalid', reason='Series or simulation start exceeds the supported calendar range')
    if any(a.time >= b.time for a, b in zip(points, points[1:])):
        return FieldFact(status='invalid', reason='Resolved series times must increase strictly')
    return known(points, 'Model-local calendar timestamps; relative times use the full simulation start')


def resource_field(context):
    root, container, name = context.record, context.container, context.field
    options = get_options(context.store)
    issues = tuple(validate_fields(root)) + tuple(validate_fields(options))
    if not ValidationReport(diagnostics=issues).is_valid:
        return FieldSemantics(effective=FieldFact(status='invalid', reason='Invalid resource or options'), diagnostics=issues)
    unit = na('Non-numeric resource declaration')
    default = FieldFact(status='required')
    effective = known(context.value)
    if type(root) is Pattern:
        if name == 'factors':
            unit = known('1', 'Dimensionless pattern multiplier')
            default = known((1.0,) * PATTERN_LENGTHS[root.kind], 'Unspecified factors are one')
            effective = known(root.effective_factors, 'Declared period only; WEEKEND applicability depends on the consumer calendar')
    elif type(root) is Curve and (name == 'points' or type(container) is CurvePoint):
        unit = units(context, options)
        if unit.status == 'known' and type(container) is CurvePoint:
            unit = known(unit.value[0 if name == 'x' else 1], unit.reason)
        if name == 'points':
            default = known((), 'No points are supplied by an omitted curve body')
        if not root.points and context.store.referenced_by(context.owner):
            effective = FieldFact(status='invalid', reason='A referenced curve requires points')
    elif type(root) is InlineTimeSeries and (name == 'points' or type(container) is SeriesPoint):
        resolved = resolved_points(context, options)
        effective = resolved
        if resolved.status == 'known' and type(container) is SeriesPoint:
            effective = known(getattr(resolved.value[context.path[-2]], name), resolved.reason if name == 'time' else '')
        if name == 'time':
            unit = known('model-local datetime', 'Effective timestamp; stored timedeltas are offsets from simulation start')
        else:
            unit = units(context, options)
            if unit.status == 'known':
                unit = known(('model-local datetime', unit.value[0]) if name == 'points' else unit.value[0], unit.reason)
    elif type(root) is FileTimeSeries:
        if name == 'file':
            unit = units(context, options)
            if unit.status == 'known':
                unit = known(unit.value[0], 'Expected ordinate unit in external data; the path itself has no physical unit')
            effective = known(root.file, 'Configured input reference; existence and contents are not read')
        elif name in ('base_directory', 'flavor', 'direction'):
            default = na('Derived file-reference metadata has no independent INP token')
    if unit.status in ('invalid', 'ambiguous'):
        effective = FieldFact(status=unit.status, reason=unit.reason)
        issues += (Diagnostic(code='resource.field_dimensions', message=unit.reason, object_id=root.id, field=name),)
    return FieldSemantics(unit=unit, default=default, effective=effective, diagnostics=issues)


def pattern_item(context):
    fact = resource_field(context)
    if fact.effective.status != 'known':
        return fact
    effective = (known(context.value, 'Declared multiplier at this period index')
        if context.path[-1] < PATTERN_LENGTHS[context.record.kind]
        else na('Stored factor beyond the declared pattern period is inactive'))
    return replace(fact, default=known(1.0, 'Unspecified factors are one'), effective=effective)


RESOURCE_FIELD_RULES = tuple(FieldRule(value_type=value_type, field=f.name, root_type=root_type,
    resolve=resource_field, resolve_item=pattern_item if root_type is Pattern and f.name == 'factors' else None)
    for root_type, types in ((Curve, (Curve, CurvePoint)), (InlineTimeSeries, (InlineTimeSeries, SeriesPoint)),
                            (FileTimeSeries, (FileTimeSeries, FileReference)), (Pattern, (Pattern,)))
    for value_type in types for f in fields(value_type))
