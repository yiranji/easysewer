"""Land-use configuration facts, distinct from simulated surface loading."""
from dataclasses import fields, replace

from ..model import quality as q, hydrology as h
from ..model.fields import validate_fields
from ..model.identity import Ref
from ..model.inspection import FieldFact, FieldSemantics
from ..model.options import get_options
from ..model.resources import InlineTimeSeries, FileTimeSeries
from ..model.units import UnitContext
from ..validation import Diagnostic, ValidationReport
from .field_contracts import FieldRule
from .hydrology_fields import known, invalid, na, reference
from .resource_fields import dimensions

BUILDS = (q.NoBuildup, q.PowerBuildup, q.ExponentialBuildup, q.SaturationBuildup, q.ExternalBuildup)
WASHES = (q.NoWashoff, q.ExponentialWashoff, q.RatingWashoff, q.EventMeanConcentration)


def landuse_field(context):
    root, container, name = context.record, context.container, context.field
    options = get_options(context.store)
    issues = tuple(validate_fields(root)) + tuple(validate_fields(options))
    if not ValidationReport(diagnostics=issues).is_valid:
        return FieldSemantics(effective=invalid('Invalid land-use configuration or options'), diagnostics=issues)
    unit = na('Non-numeric land-use declaration')
    default = FieldFact(status='required')
    effective = known(context.value, 'Configured input, not instantaneous surface loading')

    def finish(fact=effective):
        diagnostics = (Diagnostic(code='quality.field_context', message=fact.reason),) if fact.status in ('invalid', 'ambiguous') else ()
        return FieldSemantics(unit=unit, default=default, effective=fact, diagnostics=diagnostics)

    pollutant = None
    for field, types in (('subcatchment', (h.Subcatchment,)), ('landuse', (q.LandUse,)), ('pollutant', (q.Pollutant,))):
        if not hasattr(root, field):
            continue
        ref = getattr(root, field); checked = reference(context, ref)
        if checked.status != 'known':
            return finish(checked)
        row = context.store.collection(ref.collection)[ref.key]
        if type(row) not in types:
            return finish(FieldFact(reason='Extension reference target requires its own land-use contract'))
        if field == 'pollutant':
            pollutant = row
    if type(root) is q.Coverage:
        peers = [r for r in context.store.collection('swmm:coverages').values()
                 if r.subcatchment.canonical == root.subcatchment.canonical]
        if any(type(r) is not q.Coverage for r in peers):
            return finish(FieldFact(reason='Extension coverage requires its own aggregate constraint'))
        if any(not ValidationReport(diagnostics=tuple(validate_fields(r))).is_valid for r in peers):
            return finish(invalid('Invalid coverage at this subcatchment'))
        if sum(r.percent for r in peers) > 100.000001:
            return finish(invalid('Land-use coverage exceeds 100 percent'))
    function = getattr(root, 'function', None)
    if type(root) in (q.Buildup, q.Washoff):
        if type(function) not in (BUILDS if type(root) is q.Buildup else WASHES):
            return finish(FieldFact(reason='Extension formula requires its own land-use contract'))
    if type(function) is q.ExternalBuildup:
        checked = reference(context, function.series)
        if checked.status != 'known':
            return finish(checked)
        series = context.store.collection(function.series.collection)[function.series.key]
        if type(series) not in (InlineTimeSeries, FileTimeSeries):
            return finish(FieldFact(reason='Extension time series requires its own buildup contract'))
        axes = dimensions(replace(context, owner=function.series, record=series))
        if axes.status != 'known':
            return finish(axes)
    units = UnitContext(flow_units=options.flow_units or context.profile.option_default('flow_units'))
    if type(container) is Ref:
        if name == 'collection':
            default = na('Reference namespace fixed by the quality grammar')
    elif type(root) is q.LandUse and name != 'id':
        unit = known('1' if name == 'sweep_availability' else 'day')
        default = known(0., 'Omitted sweeping parameters are zero')
        effective = known(context.value if context.value is not None else 0.)
    elif type(root) is q.Coverage and name == 'percent':
        unit = known('%')
    elif type(root) is q.InitialLoading and name == 'mass_per_area':
        mass = 'count' if pollutant.units == '#/L' else units.unit('mass')
        unit = known(mass + '/' + units.unit('catchment_area'))
        effective = known(context.value, 'Only a positive loading overrides the native antecedent-dry-period initial buildup')
    elif type(root) is q.Buildup:
        if container is root and name == 'normalizer':
            if type(function) is q.NoBuildup:
                effective = na('No active buildup formula to normalize')
                default = na('AREA is a model placeholder for NoBuildup')
        if container is function:
            mass = 'count' if pollutant.units == '#/L' else units.unit('mass')
            per = units.unit('catchment_area') if root.normalizer == 'AREA' else 'user curb'
            if name == 'maximum':
                unit = known(f'{mass}/{per}')
            elif name == 'coefficient':
                unit = known(f'{mass}/{per}/day^{function.exponent:g}')
            elif name in ('rate', 'half_saturation_days'):
                unit = known('1/day' if name == 'rate' else 'day')
            elif name in ('exponent', 'scale_factor'):
                unit = known('1')
            elif name == 'unused_parameter':
                unit = na('Parsed nonnegative numeric placeholder')
                effective = na('Native parses this slot but does not use it in the buildup formula')
    elif type(root) is q.Washoff:
        if container is root and name in ('sweeping_removal', 'bmp_removal'):
            if type(function) is q.NoWashoff:
                unit = na('No active washoff removal'); effective = na('NONE ignores removal parameters')
                default = na('No removal parameter in a NONE row')
            else:
                unit = known('%'); default = known(0., 'Omitted removal efficiency is zero percent')
                effective = known(context.value if context.value is not None else 0.)
        elif container is function:
            if name == 'coefficient':
                if type(function) is q.ExponentialWashoff:
                    unit = known(f'1/h/({units.unit("rain_intensity")})^{function.exponent:g}')
                else:
                    mass = {'MG/L': 'mg', 'UG/L': 'ug', '#/L': 'count'}[pollutant.units]
                    unit = known(f'{mass}/s/({units.unit("flow")})^{function.exponent:g}')
            elif name == 'exponent':
                unit = known('1')
            elif name == 'concentration':
                unit = known(units.unit(q.concentration_dimension(pollutant)))
            elif name == 'unused_exponent':
                unit = na('Parsed exponent placeholder')
                effective = na('Native validates this slot but EMC ignores it')
    return finish(effective)


LANDUSE_FIELD_RULES = tuple(FieldRule(value_type=t, field=f.name, root_type=root, resolve=landuse_field)
    for root, nested in ((q.LandUse, ()), (q.Coverage, (Ref,)), (q.InitialLoading, (Ref,)),
                         (q.Buildup, (Ref,) + BUILDS), (q.Washoff, (Ref,) + WASHES))
    for t in (root,) + nested for f in fields(t))
