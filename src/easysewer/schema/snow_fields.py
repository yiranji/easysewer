"""Shared snow configuration and local monthly-pattern assignments."""

from dataclasses import fields, replace

from ..model import hydrology as h, climate as c
from ..model.fields import validate_fields
from ..model.identity import Ref
from ..model.inspection import FieldFact, FieldSemantics
from ..model.options import get_options
from ..model.resources import Pattern
from ..model.units import UnitContext
from ..validation import Diagnostic, ValidationReport
from .field_contracts import FieldRule
from .hydrology_fields import known, invalid, na, reference

SURFACES = {'plowable': h.PlowableSnow, 'impervious': h.DepletableSnow, 'pervious': h.DepletableSnow}


def default_surface(kind, units):
    # snow_initSnowmelt initializes tbase to zero internal degrees Fahrenheit.
    args = dict(minimum_melt=0., maximum_melt=0., base_temperature=UnitContext().convert(0., dimension='temperature', to=units),
                free_water_fraction=0., initial_snow=0., initial_free_water=0.)
    return kind(**args, **({'fraction': 0.} if kind is h.PlowableSnow else {'full_cover_depth': 0.}))


def surface(context, name, units):
    value = getattr(context.record, name)
    kind = SURFACES[name]
    if value is None:
        return known(default_surface(kind, units), 'Omitted native snow-surface parameter defaults; not consumer-area state')
    if type(value) is not kind:
        return FieldFact(reason='Extension snow surface requires its own contract')
    if min(value.minimum_melt, value.maximum_melt, value.initial_snow, value.initial_free_water) < 0:
        return invalid('Snow depths and melt coefficients must be nonnegative for physical simulation')
    if value.minimum_melt > value.maximum_melt:
        return invalid('Minimum melt coefficient exceeds maximum')
    if kind is h.DepletableSnow and value.full_cover_depth < 0:
        return invalid('Full-cover depth must be nonnegative')
    return known(replace(value, initial_free_water=min(value.initial_free_water, value.free_water_fraction * value.initial_snow)),
                 'Initial free water capped by holding capacity; each consumer applies its own surface-area fractions')


def removal(context):
    value = context.record.removal
    if value is None:
        return known(None, 'Omitted removal has zero transfer fractions and no effective removal')
    if type(value) is not h.SnowRemoval:
        return FieldFact(reason='Extension snow removal requires its own contract')
    fractions = (value.out_of_system, value.to_impervious, value.to_pervious, value.immediate_melt, value.to_subcatchment or 0.)
    if value.threshold < 0 or min(fractions) < 0 or sum(fractions) > 1:
        return invalid('Physical removal requires nonnegative threshold/fractions and a total fraction at most one')
    if value.destination is not None:
        target = reference(context, value.destination)
        if target.status != 'known':
            return target
    elif (value.to_subcatchment or 0) > 0:
        return invalid('Positive snow transfer requires a destination')
    return known(replace(value, to_subcatchment=value.to_subcatchment or 0.),
                 'Configured removal fractions; applicability depends on each consumer and transfer destination')


def transfer_destination(context, value):
    if not value.to_subcatchment:
        return na('No snow fraction is transferred to another catchment')
    target = context.store.collection(value.destination.collection)[value.destination.key]
    if type(target) is not h.Subcatchment:
        return FieldFact(reason='Extension recipient requires its own snow-transfer contract')
    if target.snowpack is None or target.impervious_percent >= 100:
        return na('Native transfer is inactive without a recipient snowpack and pervious area fraction')
    checked = reference(context, target.snowpack)
    if checked.status != 'known':
        return checked
    if type(context.store.collection(target.snowpack.collection)[target.snowpack.key]) is not h.Snowpack:
        return FieldFact(reason='Extension recipient snowpack requires its own transfer contract')
    return known(value.destination, 'Configured recipient; transfer depth is not a mass-conserving area-rescaled volume')


def snow_field(context):
    root, container, name = context.record, context.container, context.field
    options = get_options(context.store)
    issues = tuple(validate_fields(root)) + tuple(validate_fields(options))
    if not ValidationReport(diagnostics=issues).is_valid:
        return FieldSemantics(effective=invalid('Invalid snow record or options'), diagnostics=issues)
    units = UnitContext(flow_units=options.flow_units or context.profile.option_default('flow_units'))
    unit = na('Non-numeric snow declaration')
    default = FieldFact(status='required')
    effective = known(context.value)
    dimension = next(f for f in fields(container) if f.name == name).metadata.get('dimension')
    if dimension:
        unit = known(units.unit(dimension))
    group = context.path[0]
    if group in SURFACES:
        fact = surface(context, group, units)
        if container is root:
            default = known(default_surface(SURFACES[group], units), 'Native defaults for an omitted surface row')
            effective = fact
        elif fact.status != 'known':
            effective = fact
        else:
            effective = known(getattr(fact.value, name), fact.reason)
    elif group == 'removal':
        fact = removal(context)
        if container is root:
            default = known(None, 'No effective removal when the row is omitted')
            effective = fact
        elif fact.status != 'known':
            effective = fact
        else:
            value = fact.value
            if type(container) is h.SnowRemoval:
                effective = known(getattr(value, name), fact.reason)
                if name == 'to_subcatchment':
                    default = known(0., 'Model normalization supplies native mandatory Fsub for the documented short row')
                elif name == 'destination':
                    default = known(None, 'No declared snow-transfer recipient')
                if name in ('destination', 'to_subcatchment'):
                    destination = transfer_destination(context, value)
                    if name == 'destination' or destination.status != 'known' and value.to_subcatchment:
                        effective = destination
            elif type(container) is Ref:
                if name == 'collection':
                    default = na('The snow-transfer grammar fixes the reference namespace')
                destination = transfer_destination(context, value)
                if destination.status != 'known':
                    effective = destination
    if effective.status == 'invalid':
        issues += (Diagnostic(code='snowpack.field_context', message=effective.reason, object_id=root.id, field=name),)
    return FieldSemantics(unit=unit, default=default, effective=effective, diagnostics=issues)


def adjustment_field(context):
    root, container, name, value = context.record, context.container, context.field, context.value
    issues = tuple(validate_fields(root))
    if not ValidationReport(diagnostics=issues).is_valid:
        return FieldSemantics(effective=invalid('Invalid local adjustment declaration'), diagnostics=issues)
    default = FieldFact(status='required')
    unit = na('Configured reference, not a scalar monthly multiplier')
    effective = known(value, 'Configured monthly-pattern assignment; month selection and runtime factor rules are not evaluated')
    group = context.path[0]
    if container is root and name != 'subcatchment':
        default = known(None, 'No local assignment; corresponding base/global behavior remains in effect')
    elif type(container) is Ref and name == 'collection':
        default = na('Reference namespace fixed by the adjustment grammar')
    target = reference(context, root.subcatchment)
    if target.status != 'known':
        effective = target
    else:
        ref = getattr(root, group)
        if ref is not None:
            target = reference(context, ref)
            if target.status != 'known':
                effective = target
            elif group != 'subcatchment':
                pattern = context.store.collection(ref.collection)[ref.key]
                if type(pattern) is not Pattern:
                    effective = FieldFact(reason='Extension pattern requires its own local-adjustment contract')
                elif pattern.kind != 'MONTHLY':
                    effective = invalid('Local catchment adjustments require a MONTHLY pattern')
    if effective.status == 'invalid':
        issues += (Diagnostic(code='adjustment.field_context', message=effective.reason, object_id=root.subcatchment.key, field=name),)
    return FieldSemantics(unit=unit, default=default, effective=effective, diagnostics=issues)


SNOW_FIELD_RULES = tuple(FieldRule(value_type=t, field=f.name, root_type=h.Snowpack, resolve=snow_field)
    for t in (h.Snowpack, h.PlowableSnow, h.DepletableSnow, h.SnowRemoval, Ref) for f in fields(t))
ADJUSTMENT_FIELD_RULES = tuple(FieldRule(value_type=t, field=f.name, root_type=c.SubcatchmentAdjustments, resolve=adjustment_field)
    for t in (c.SubcatchmentAdjustments, Ref) for f in fields(t))
