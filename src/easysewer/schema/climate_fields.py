"""Static climate configuration facts; no file reads or live weather lookup."""

from dataclasses import fields, is_dataclass, replace

from ..model import climate as c
from ..model.fields import validate_fields
from ..model.identity import Ref
from ..model.inspection import FieldFact, FieldSemantics
from ..model.options import get_options
from ..model.resources import Pattern, InlineTimeSeries, FileTimeSeries
from ..model.units import UnitContext
from ..model.values import FileReference
from ..validation import Diagnostic, ValidationReport
from .field_contracts import FieldRule
from .hydrology_fields import known, invalid, na, reference

TYPES = (c.Climate, c.ClimateFile, c.SeriesTemperature, c.FileTemperature,
         c.MonthlyFactors, c.MonthlyWindSpeeds, c.MonthlyEvaporation, c.MonthlyTemperatureChanges,
         c.Snowmelt, c.ArealDepletion, c.Evaporation, c.ConstantEvaporation, c.SeriesEvaporation,
         c.TemperatureEvaporation, c.FileEvaporation, c.FileWind, c.ClimateAdjustments, Ref, FileReference)


def descendants(value):
    if is_dataclass(value):
        yield value
        for f in fields(value):
            yield from descendants(getattr(value, f.name))
    elif isinstance(value, tuple):
        for item in value:
            yield from descendants(item)


def at(value, path):
    for part in path:
        value = value[part] if type(part) is int else getattr(value, part)
    return value


def climate_field(context):
    root, container, name = context.record, context.container, context.field
    options = get_options(context.store)
    issues = tuple(validate_fields(root)) + tuple(validate_fields(options))
    if not ValidationReport(diagnostics=issues).is_valid:
        return FieldSemantics(effective=invalid('Invalid climate configuration or options'), diagnostics=issues)
    if any(type(value) not in TYPES for value in descendants(root)):
        return FieldSemantics(effective=FieldFact(reason='Extension climate values require their own configuration contract'))
    issues += tuple(c.validate_climate(context.store, context.profile, for_run=True))
    for value in descendants(root):
        if type(value) is Ref:
            fact = reference(context, value)
            if fact.status != 'known':
                issues += (Diagnostic(code='climate.field_reference', message=fact.reason, field=name),)
                continue
            target = context.store.collection(value.collection)[value.key]
            allowed = (Pattern,) if value.collection == 'swmm:patterns' else (InlineTimeSeries, FileTimeSeries)
            if type(target) not in allowed:
                return FieldSemantics(effective=FieldFact(reason='Extension climate resource requires its own contract'))
            if type(target) is Pattern and target.kind != 'MONTHLY':
                issues += (Diagnostic(code='climate.field_pattern', message='Recovery requires a MONTHLY pattern', field=name),)
    if not ValidationReport(diagnostics=issues).is_valid:
        return FieldSemantics(effective=invalid('Invalid climate dependencies or physical configuration'), diagnostics=issues)
    try:
        resolved = c.resolve_climate(root, options, context.profile)
    except ValueError as error:
        return FieldSemantics(effective=FieldFact(reason=str(error)))
    unit = na('Non-numeric configured declaration')
    default = FieldFact(status='required')
    reason = 'Effective static climate configuration; not a date-selected or live weather value'
    effective = known(at(resolved, context.path), reason)
    units = UnitContext(flow_units=options.flow_units or context.profile.option_default('flow_units'))
    if container is root:
        omitted = c.resolve_climate(replace(root, **{name: None}), options, context.profile)
        default = known(getattr(omitted, name), 'Effective configuration when this group is omitted')
    elif type(container) is c.ClimateFile:
        if name == 'units':
            default = known(dict(context.profile.climate_defaults)['file_units_us' if units.system == 'US' else 'file_units_si'])
            effective = known(resolved.file.units, 'Configured GHCND selector; other detected file formats have their own units, not inspected here')
        elif name == 'start_date':
            default = known(options.start_date or context.profile.option_default('start_date'), 'Omitted file start follows simulation date')
            unit = known('model-local date')
    elif type(container) is FileReference and name != 'path':
        default = na('Derived file-reference metadata has no independent INP token')
    elif type(container) is Ref and name == 'collection':
        default = na('The climate grammar fixes the reference namespace')
    elif type(container) is c.Evaporation:
        default = known({'source': c.ConstantEvaporation(rate=0.), 'dry_only': False, 'recovery_pattern': None}[name])
    elif type(container) is c.FileEvaporation:
        default = known(c.MonthlyFactors(values=(1.,) * 12), 'Omitted pan coefficients require explicit normalization of bare FILE')
    elif type(container) is c.ClimateAdjustments:
        default = known(getattr(c.resolve_climate(c.Climate(), options, context.profile).adjustments, name))
    dimension = next(f for f in fields(container) if f.name == name).metadata.get('dimension')
    if type(container) in (c.MonthlyFactors, c.MonthlyWindSpeeds, c.MonthlyEvaporation, c.MonthlyTemperatureChanges):
        dimension = container.dimension
    elif type(container) is c.ArealDepletion:
        dimension = 'ratio'
    if dimension:
        unit = known(units.unit(dimension))
    return FieldSemantics(unit=unit, default=default, effective=effective, diagnostics=issues)


CLIMATE_FIELD_RULES = tuple(FieldRule(value_type=t, field=f.name, root_type=c.Climate,
    resolve=climate_field, resolve_item=climate_field if f.name in ('values', 'fractions') else None)
    for t in TYPES for f in fields(t))
