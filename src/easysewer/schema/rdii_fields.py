"""Static seasonal RDII configuration, not an instantaneous response lookup."""
from dataclasses import fields, replace

from ..model import rdii as r, hydrology as h, network as n
from ..model.fields import validate_fields
from ..model.files import InterfaceFile
from ..model.identity import Ref, canonical_key
from ..model.inspection import FieldFact, FieldSemantics
from ..model.options import get_options
from ..model.units import UnitContext
from ..validation import Diagnostic, ValidationReport
from .field_contracts import FieldRule
from .hydrology_fields import known, invalid, na, reference


def hydrograph_context(context, group, options):
    if type(group) is not r.UnitHydrograph or any(type(v) is not r.HydrographResponse for v in group.responses):
        return FieldFact(reason='Extension hydrograph/response requires its own seasonal contract')
    issues = tuple(validate_fields(group))
    if not ValidationReport(diagnostics=issues).is_valid:
        return invalid('Invalid unit hydrograph configuration')
    for gage in (*group.prior_rain_gages, *((group.rain_gage,) if group.rain_gage is not None else ())):
        checked = reference(context, gage)
        if checked.status != 'known':
            return checked
        if type(context.store.collection(gage.collection)[gage.key]) is not h.RainGage:
            return FieldFact(reason='Extension rain gage requires its own RDII contract')
    used = any(isinstance(getattr(row, 'hydrograph', None), Ref)
               and canonical_key(row.hydrograph.key) == canonical_key(group.id)
               for row in context.store.collection('swmm:rdii').values())
    namespaces = {s.key for s in context.store.specifications}
    binding = context.store.collection('swmm:files').get(('RDII', 'USE')) if 'swmm:files' in namespaces else None
    if binding is not None:
        if type(binding) is not InterfaceFile:
            return FieldFact(reason='Extension RDII interface requires its own generation contract')
        if not ValidationReport(diagnostics=tuple(validate_fields(binding))).is_valid:
            return invalid('Invalid RDII interface binding')
    use_file = binding is not None
    generation = not use_file and not options.ignore_rainfall and not options.ignore_rdii
    if used and generation and group.rain_gage is None:
        return invalid('Generating RDII requires a rain gage for each used hydrograph')
    for slots in seasonal_slots(group):
        active = [group.responses[index] for index in slots if index is not None
                  and group.responses[index].native_base_seconds > 0]
        if sum(v.fraction for v in active) > 1.01:
            return invalid('An active monthly response sum exceeds the native 1.01 limit')
    return known(group)


def seasonal_slots(group):
    # The group is already validated. Keep tuple positions so equal repeated
    # rows remain distinct ordered assignments, and avoid validating it again
    # for every month through the public for_month() convenience method.
    slots = [[None, None, None] for _ in r.MONTHS]
    for index, response in enumerate(group.responses):
        months = range(12) if response.month == 'ALL' else (r.MONTHS.index(response.month),)
        for month in months:
            slots[month][r.RESPONSES.index(response.response)] = index
    return tuple(tuple(row) for row in slots)


def active_months(group, index):
    return tuple(month for month, slots in zip(r.MONTHS, seasonal_slots(group)) if index in slots)


def rdii_field(context):
    root, container, name = context.record, context.container, context.field
    options = get_options(context.store)
    issues = tuple(validate_fields(root)) + tuple(validate_fields(options))
    if not ValidationReport(diagnostics=issues).is_valid:
        return FieldSemantics(effective=invalid('Invalid RDII configuration or options'), diagnostics=issues)
    unit = na('Non-numeric RDII declaration')
    default = FieldFact(status='required')
    effective = known(context.value, 'Configured seasonal input, not instantaneous RDII flow')
    def finish(fact):
        diagnostics = (Diagnostic(code='rdii.field_context', message=fact.reason),) if fact.status in ('invalid', 'ambiguous') else ()
        return FieldSemantics(unit=unit, default=default, effective=fact, diagnostics=diagnostics)
    group = root
    if type(root) is r.RdiiInflow:
        for ref, types in ((root.node, (n.Junction, n.Storage, n.Divider, n.Outfall)),
                           (root.hydrograph, (r.UnitHydrograph,))):
            fact = reference(context, ref)
            if fact.status != 'known':
                return finish(fact)
            if type(context.store.collection(ref.collection)[ref.key]) not in types:
                return finish(FieldFact(reason='Extension target requires its own RDII contract'))
        group = context.store.collection(root.hydrograph.collection)[root.hydrograph.key]
    fact = hydrograph_context(context, group, options)
    if fact.status != 'known':
        return finish(fact)
    units = UnitContext(flow_units=options.flow_units or context.profile.option_default('flow_units'))
    if type(container) is Ref:
        if name == 'collection':
            default = na('Reference namespace fixed by the RDII grammar')
    elif type(root) is r.RdiiInflow and name == 'sewer_area':
        unit = known(units.unit('catchment_area'))
    elif type(root) is r.UnitHydrograph:
        if container is root:
            if name == 'rain_gage':
                default = known(None, 'No explicitly configured rain gage')
            elif name == 'prior_rain_gages':
                default = known((), 'No prior gage assignments')
                effective = known(root.prior_rain_gages, 'Prior gages still have native activation side effects')
            elif name == 'responses':
                default = known((), 'Unassigned month/response slots have zero native parameters')
                effective = known(tuple(replace(v, maximum_abstraction=v.maximum_abstraction or 0.,
                    recovery_rate=v.recovery_rate or 0., initial_abstraction=v.initial_abstraction or 0.) for v in root.responses),
                    'Ordered assignments; ALL and monthly entries apply independently to each response type')
        if context.path[0] == 'responses' and len(context.path) > 1:
            index = context.path[1]; response = root.responses[index]
            months = active_months(root, index)
            reason = 'Configured response effective in ' + ', '.join(months)
            if len(context.path) == 2:
                default = na('Ordered response entries have no independent omitted-row default')
                effective = known(replace(response, maximum_abstraction=response.maximum_abstraction or 0.,
                    recovery_rate=response.recovery_rate or 0., initial_abstraction=response.initial_abstraction or 0.), reason)
            elif type(container) is r.HydrographResponse:
                dimension = next(f for f in fields(response) if f.name == name).metadata.get('dimension')
                if dimension:
                    unit = known(units.unit(dimension))
                effective = known(context.value, reason)
                if name in ('maximum_abstraction', 'recovery_rate', 'initial_abstraction'):
                    default = known(0., 'Omitted initial-abstraction parameter is zero')
                    effective = known(context.value if context.value is not None else 0., reason)
                elif name in ('time_to_peak', 'recession_ratio'):
                    effective = known(context.value, reason + f'; native peak/base independently truncate to {response.native_peak_seconds}/{response.native_base_seconds} seconds')
            if not months:
                effective = na('This ordered response is superseded in every month')
    return finish(effective)


RDII_FIELD_RULES = tuple(FieldRule(value_type=t, field=f.name, root_type=root, resolve=rdii_field,
    resolve_item=rdii_field if f.name in ('responses', 'prior_rain_gages') else None)
    for root, types in ((r.UnitHydrograph, (r.UnitHydrograph, r.HydrographResponse, Ref)),
                        (r.RdiiInflow, (r.RdiiInflow, Ref)))
    for t in types for f in fields(t))
