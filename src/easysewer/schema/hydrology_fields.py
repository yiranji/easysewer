"""Rain-gage and catchment input facts, separate from live runoff state."""

from dataclasses import fields, replace

from ..model import hydrology as h
from ..model.fields import validate_fields
from ..model.identity import Ref
from ..model.inspection import FieldFact, FieldSemantics
from ..model.options import get_options
from ..model.resources import InlineTimeSeries, FileTimeSeries
from ..model.units import UnitContext
from ..model.values import FileReference, Point
from ..validation import Diagnostic, ValidationReport
from .display_fields import coordinate_unit
from .field_contracts import FieldRule


def known(value, reason='Configured input in model units, not live runoff state'):
    return FieldFact(status='known', value=value, reason=reason)


def na(reason):
    return FieldFact(status='not_applicable', reason=reason)


def invalid(reason):
    return FieldFact(status='invalid', reason=reason)


def reference(context, ref):
    if not isinstance(ref, Ref) or not context.store.contains(ref):
        return invalid('Missing referenced object')
    row = context.store.collection(ref.collection)[ref.key]
    if not ValidationReport(diagnostics=tuple(validate_fields(row))).is_valid:
        return invalid('Invalid referenced object')
    if type(context.record) is h.Subcatchment and ref.canonical == context.record.outlet.canonical:
        other = 'swmm:nodes' if ref.collection == 'swmm:subcatchments' else 'swmm:subcatchments'
        if context.store.contains(Ref(collection=other, key=ref.key)):
            return FieldFact(status='ambiguous', reason='Native outlet name denotes both a node and a subcatchment')
    return known(ref, 'Configured reference; external resources are not opened')


def rainfall(context):
    source = context.record.source
    if type(source) is h.FileRainfall:
        return known(source, 'Configured external source; availability, detected format and interface header are not inspected')
    if type(source) is not h.SeriesRainfall:
        return FieldFact(reason='Extension rainfall source requires its own contract')
    fact = reference(context, source.series)
    if fact.status != 'known':
        return fact
    series = context.store.collection(source.series.collection)[source.series.key]
    if type(series) not in (InlineTimeSeries, FileTimeSeries):
        return FieldFact(reason='Extension time series requires its own rainfall contract')
    return known(source, 'Configured rainfall series, not current rain intensity')


def infiltration(context, options):
    row = context.record
    value = row.infiltration
    if value is None:
        return na('No pervious runoff area') if row.area == 0 or row.impervious_percent >= 100 else invalid('Pervious area requires infiltration parameters')
    if type(value) is not h.Infiltration:
        return FieldFact(reason='Extension infiltration requires its own contract')
    method = h.effective_infiltration_method(value, options, context.profile)
    expected = h.Horton if method in ('HORTON', 'MODIFIED_HORTON') else h.GreenAmpt if method in ('GREEN_AMPT', 'MODIFIED_GREEN_AMPT') else h.CurveNumber
    if not isinstance(value.parameters, expected):
        return invalid('Parameters do not match the explicit or inherited infiltration method')
    if type(value.parameters) is not expected:
        return FieldFact(reason='Extension infiltration parameters require their own contract')
    parameters = value.parameters
    if expected is h.Horton:
        parameters = replace(parameters, maximum_volume=parameters.maximum_volume or 0.0,
                             drying_time=parameters.drying_time or 1e-6)
    elif expected is h.CurveNumber:
        parameters = replace(parameters, curve_number=min(99.0, max(10.0, parameters.curve_number)))
    return known(replace(value, method=method, parameters=parameters),
                 'Resolved method and input parameters; not infiltration capacity, soil state or seasonal adjustment')


def subareas(context):
    row, value = context.record, context.record.subareas
    if value is None:
        return na('Zero-area catchment has no runoff subareas') if row.area == 0 else invalid('A nonzero catchment requires subarea parameters')
    if type(value) is not h.Subareas:
        return FieldFact(reason='Extension subareas require their own contract')
    if value.zero_storage_percent > 100:
        return invalid('Zero-storage percentage above 100 produces a negative subarea fraction')
    route = 'OUTLET' if row.impervious_percent == 0 or row.impervious_percent >= 100 else value.route_to
    return known(replace(value, route_to=route, routed_percent=100.0 if value.routed_percent is None else value.routed_percent),
                 'Configured routing after area-fraction adjustment; routed percent has no effect for OUTLET')


def hydrology_field(context):
    root, container, name, value = context.record, context.container, context.field, context.value
    options = get_options(context.store)
    issues = tuple(validate_fields(root)) + tuple(validate_fields(options))
    if not ValidationReport(diagnostics=issues).is_valid:
        return FieldSemantics(effective=invalid('Invalid hydrology record or options'), diagnostics=issues)
    default = FieldFact(status='required')
    unit = na('Non-numeric hydrology declaration')
    effective = known(value)
    units = UnitContext(flow_units=options.flow_units or context.profile.option_default('flow_units'))
    dimension = next(f for f in fields(container) if f.name == name).metadata.get('dimension')
    if dimension and dimension != 'map_coordinate':
        unit = known(units.unit(dimension))
    if type(container) is Point or name in ('position', 'polygon'):
        unit, map_issues = coordinate_unit(context)
        issues += map_issues
        if name == 'position':
            default = known(None, 'No declared symbol position')
        elif name == 'polygon':
            default = known((), 'No declared catchment polygon')
        if unit.status == 'invalid':
            effective = invalid('Invalid explicit map context')
    elif type(container) is Ref:
        if name == 'collection':
            default = na('Namespace fixed or resolved by the input grammar')
        checked = reference(context, container)
        if checked.status != 'known':
            effective = checked
    elif type(root) is h.RainGage:
        if name == 'source':
            effective = rainfall(context)
        elif name == 'interval':
            unit = known('s', 'Whole-second timedelta; numeric INP declarations are hours')
        elif type(container) is h.SeriesRainfall:
            effective = reference(context, value)
        elif type(container) is h.FileRainfall:
            if name == 'start_date':
                default = known(None, 'No input date filter')
            elif name == 'file':
                effective = known(value, 'Configured input reference without file I/O')
        elif type(container) is FileReference and name in ('base_directory', 'flavor', 'direction'):
            default = na('Derived file-reference metadata has no independent input token')
        if type(root.source) is h.FileRainfall and container is root and name in ('form', 'interval'):
            effective = FieldFact(reason='External rainfall/interface processing can replace this declaration; inspect the file/header separately')
    elif type(root) is h.Subcatchment:
        if container is root:
            if name in ('rain_gage', 'outlet', 'snowpack'):
                if name == 'snowpack':
                    default = known(None, 'No snowpack assignment')
                effective = known(None) if value is None else reference(context, value)
            elif name == 'impervious_percent':
                effective = known(min(100.0, value), 'Native caps impervious percentage at 100')
            elif name == 'subareas':
                if root.area == 0:
                    default = na('Subarea parameters may be absent for a zero-area catchment')
                effective = subareas(context)
            elif name == 'infiltration':
                if root.area == 0 or root.impervious_percent >= 100:
                    default = na('Infiltration parameters may be absent without pervious runoff area')
                effective = infiltration(context, options)
        elif type(container) is h.Subareas:
            fact = subareas(context)
            if name == 'routed_percent':
                default = known(100.0, 'Omitted within-subcatchment routing percentage')
            if fact.status != 'known':
                effective = fact
            elif name in ('route_to', 'routed_percent'):
                effective = (na('Runoff goes directly to the catchment outlet') if name == 'routed_percent' and fact.value.route_to == 'OUTLET'
                             else known(getattr(fact.value, name), fact.reason))
        elif type(container) in (h.Infiltration, h.Horton, h.GreenAmpt, h.CurveNumber):
            fact = infiltration(context, options)
            if type(container) is h.Infiltration and name == 'method':
                default = known(options.infiltration or context.profile.option_default('infiltration'), 'Inherited project infiltration method')
            elif type(container) is h.Horton and name == 'maximum_volume':
                default = known(0.0, 'Zero means no maximum infiltration-volume limit')
            if fact.status != 'known':
                effective = fact
            else:
                resolved = fact.value if type(container) is h.Infiltration else fact.value.parameters
                effective = known(getattr(resolved, name), fact.reason)
    if effective.status in ('invalid', 'ambiguous'):
        issues += (Diagnostic(code='hydrology.field_context', message=effective.reason, object_id=root.id, field=name),)
    return FieldSemantics(unit=unit, default=default, effective=effective, diagnostics=issues)


HYDROLOGY_FIELD_RULES = tuple(FieldRule(value_type=value_type, field=f.name, root_type=root_type, resolve=hydrology_field)
    for root_type, types in ((h.RainGage, (h.RainGage, h.SeriesRainfall, h.FileRainfall, FileReference, Ref, Point)),
        (h.Subcatchment, (h.Subcatchment, h.Subareas, h.Infiltration, h.Horton, h.GreenAmpt, h.CurveNumber, Ref, Point)))
    for value_type in types for f in fields(value_type))
