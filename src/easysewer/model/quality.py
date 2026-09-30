"""Pollutants, land-use relations and dimensioned buildup/washoff variants."""

from dataclasses import dataclass, replace
from typing import Literal

from ..validation import Diagnostic, DiagnosticSubject, Severity, ValidationReport
from .fields import number, reference, validate_fields
from .identity import Ref, canonical_key
from .network import Entity
from .store import CollectionSpec
from .units import UnitTransform
from .pollutant_units import PollutantUnitConversion, PollutantUnitTransform
from .usage import ResourceUse


@dataclass(frozen=True, kw_only=True)
class Pollutant(Entity):
    units: Literal['MG/L', 'UG/L', '#/L']
    rainfall_concentration: float = number('concentration', minimum=0)
    groundwater_concentration: float = number('concentration', minimum=0)
    rdii_concentration: float = number('concentration', minimum=0)
    decay_rate: float = number('inverse_days')
    snow_only: bool | None = None
    co_pollutant: Ref | None = reference('swmm:pollutants', None)
    co_fraction: float | None = number('ratio', None, minimum=0)
    dwf_concentration: float | None = number('concentration', None, minimum=0)
    initial_concentration: float | None = number('concentration', None, minimum=0)

    def validate_local(self):
        if canonical_key(self.id) == 'FLOW':
            yield Diagnostic(code='quality.reserved_id', message='FLOW is reserved and cannot name a pollutant', field='id')
        if self.co_pollutant is None and self.co_fraction is not None:
            yield Diagnostic(code='quality.no_co_pollutant', message='A co-fraction requires a co-pollutant reference', field='co_fraction')
        if self.co_pollutant is not None and self.co_pollutant.key == '*':
            yield Diagnostic(code='quality.co_placeholder', message='Asterisk denotes no co-pollutant and cannot be a co-pollutant reference')


@dataclass(frozen=True, kw_only=True)
class LandUse(Entity):
    sweep_interval: float | None = number('days', None, minimum=0)
    sweep_availability: float | None = number('ratio', None, minimum=0, maximum=1)
    days_since_sweeping: float | None = number('days', None, minimum=0)


@dataclass(frozen=True, kw_only=True)
class Coverage:
    subcatchment: Ref = reference('swmm:subcatchments')
    landuse: Ref = reference('swmm:landuses')
    percent: float = number('percent', minimum=0, maximum=100)


@dataclass(frozen=True, kw_only=True)
class InitialLoading:
    subcatchment: Ref = reference('swmm:subcatchments')
    pollutant: Ref = reference('swmm:pollutants')
    mass_per_area: float = number('pollutant_surface_loading', minimum=0)


@dataclass(frozen=True, kw_only=True)
class BuildupFunction:
    """Extension base for a pollutant's surface accumulation formula."""


@dataclass(frozen=True, kw_only=True)
class NoBuildup(BuildupFunction):
    pass


@dataclass(frozen=True, kw_only=True)
class PowerBuildup(BuildupFunction):
    maximum: float = number('pollutant_buildup', minimum=0)
    coefficient: float = number('pollutant_buildup_rate', minimum=0)
    exponent: float = number('ratio', minimum=0, maximum=10)

    def validate_local(self):
        if 0 < self.exponent < .01:
            yield Diagnostic(code='quality.buildup_exponent', message='Native power exponent must be zero or between .01 and 10', field='exponent')


@dataclass(frozen=True, kw_only=True)
class ExponentialBuildup(BuildupFunction):
    maximum: float = number('pollutant_buildup', minimum=0)
    rate: float = number('inverse_days', minimum=0)
    unused_parameter: float = number('ratio', 0., minimum=0)


@dataclass(frozen=True, kw_only=True)
class SaturationBuildup(BuildupFunction):
    maximum: float = number('pollutant_buildup', minimum=0)
    half_saturation_days: float = number('days', minimum=0)
    unused_parameter: float = number('ratio', 0., minimum=0)


@dataclass(frozen=True, kw_only=True)
class ExternalBuildup(BuildupFunction):
    maximum: float = number('pollutant_buildup', minimum=0)
    scale_factor: float = number('ratio', minimum=0)
    series: Ref = reference('swmm:timeseries')


@dataclass(frozen=True, kw_only=True)
class Buildup:
    landuse: Ref = reference('swmm:landuses')
    pollutant: Ref = reference('swmm:pollutants')
    function: BuildupFunction
    normalizer: Literal['AREA', 'CURBLENGTH'] = 'AREA'

    def validate_local(self):
        if type(self.function) is NoBuildup and self.normalizer != 'AREA':
            yield Diagnostic(code='quality.no_buildup_normalizer', message='NoBuildup has no stored normalizer; use its default AREA value')


@dataclass(frozen=True, kw_only=True)
class WashoffFunction:
    """Extension base; coefficients have formula-specific dimensions."""


@dataclass(frozen=True, kw_only=True)
class NoWashoff(WashoffFunction):
    pass


@dataclass(frozen=True, kw_only=True)
class ExponentialWashoff(WashoffFunction):
    coefficient: float = number('washoff_intensity_coefficient', minimum=0)
    exponent: float = number('ratio', minimum=-10, maximum=10)


@dataclass(frozen=True, kw_only=True)
class RatingWashoff(WashoffFunction):
    coefficient: float = number('washoff_flow_coefficient', minimum=0)
    exponent: float = number('ratio', minimum=-10, maximum=10)


@dataclass(frozen=True, kw_only=True)
class EventMeanConcentration(WashoffFunction):
    concentration: float = number('concentration', minimum=0)
    unused_exponent: float = number('ratio', 0., minimum=-10, maximum=10)


@dataclass(frozen=True, kw_only=True)
class Washoff:
    landuse: Ref = reference('swmm:landuses')
    pollutant: Ref = reference('swmm:pollutants')
    function: WashoffFunction
    sweeping_removal: float | None = number('percent', None, minimum=0, maximum=100)
    bmp_removal: float | None = number('percent', None, minimum=0, maximum=100)

    def validate_local(self):
        if type(self.function) is NoWashoff and (self.sweeping_removal is not None or self.bmp_removal is not None):
            yield Diagnostic(code='quality.no_washoff_removal', message='NoWashoff ignores all removal parameters; leave them absent')


def relation_key(value):
    if isinstance(value, Coverage):
        return value.subcatchment.key, value.landuse.key
    if isinstance(value, InitialLoading):
        return value.subcatchment.key, value.pollutant.key
    return value.landuse.key, value.pollutant.key


QUALITY_COLLECTIONS = (
    CollectionSpec(key='swmm:pollutants', record_type=Pollutant, key_of=lambda v: v.id, identity_field='id', validate=validate_fields),
    CollectionSpec(key='swmm:landuses', record_type=LandUse, key_of=lambda v: v.id, identity_field='id', validate=validate_fields),
    *(CollectionSpec(key='swmm:'+name, record_type=kind, key_of=relation_key, validate=validate_fields)
      for name, kind in (('coverages',Coverage), ('loadings',InitialLoading), ('buildup',Buildup), ('washoff',Washoff))),
)


def concentration_dimension(pollutant):
    return {'MG/L':'concentration_mg_l', 'UG/L':'concentration_ug_l', '#/L':'concentration_count_l'}[pollutant.units]


def accumulation_dimension(pollutant, normalizer):
    return ('count' if pollutant.units == '#/L' else 'mass') + ('_per_area_day' if normalizer == 'AREA' else '_per_curb_day')


def quality_resource_uses(store):
    for key, row in store.collection('swmm:buildup').items():
        if type(row.function) is not ExternalBuildup:
            continue
        if not ValidationReport(diagnostics=tuple(validate_fields(row))).is_valid:
            continue
        if not store.contains(row.pollutant):
            continue
        pollutant = store.collection('swmm:pollutants')[row.pollutant.key]
        if not ValidationReport(diagnostics=tuple(validate_fields(pollutant))).is_valid:
            continue
        yield ResourceUse(owner=Ref(collection='swmm:buildup', key=key), target=row.function.series,
            path=('function','series'), role='external pollutant buildup', dimensions=(accumulation_dimension(pollutant,row.normalizer),))


def _buildup_factor(row, context, normalizer):
    pollutant = context.record(row.pollutant)
    mass = 1. if pollutant.units == '#/L' else context.number(1., 'mass')
    return mass / (context.number(1., 'catchment_area') if normalizer == 'AREA' else 1.)


def _convert_buildup(row, context):
    function = row.function
    if type(function) is NoBuildup:
        return row
    if type(function) not in (PowerBuildup,ExponentialBuildup,SaturationBuildup,ExternalBuildup):
        raise ValueError('Unknown buildup variant requires its own unit transform')
    factor = _buildup_factor(row, context, row.normalizer)
    changes = {'maximum':function.maximum*factor}
    if type(function) is PowerBuildup:
        changes['coefficient'] = function.coefficient*factor
    elif type(function) not in (ExponentialBuildup, SaturationBuildup, ExternalBuildup):
        raise ValueError('Unknown buildup variant requires its own unit transform')
    return replace(row, function=replace(function, **changes))


def _convert_washoff(row, context):
    function = row.function
    if type(function) in (NoWashoff, EventMeanConcentration):
        return row
    dimension = 'rain_intensity' if type(function) is ExponentialWashoff else 'flow' if type(function) is RatingWashoff else None
    if dimension is None:
        raise ValueError('Unknown washoff variant requires its own unit transform')
    return replace(row, function=replace(function, coefficient=function.coefficient/context.number(1.,dimension)**function.exponent))


QUALITY_TRANSFORMS = (
    UnitTransform(value_type=Pollutant, convert=lambda row,context: row),
    UnitTransform(value_type=InitialLoading, convert=lambda row,context: replace(row,mass_per_area=row.mass_per_area*_buildup_factor(row,context,'AREA'))),
    UnitTransform(value_type=Buildup, convert=_convert_buildup),
    UnitTransform(value_type=Washoff, convert=_convert_washoff),
)


def validate_quality(store):
    totals = {}
    contributors = {}
    for row in store.collection('swmm:coverages').values():
        if ValidationReport(diagnostics=tuple(validate_fields(row))).is_valid:
            key = canonical_key(row.subcatchment.key)
            totals[key] = totals.get(key,0) + row.percent
            contributors.setdefault(key, []).append(DiagnosticSubject(collection='swmm:coverages',
                key=relation_key(row), path=('percent',)))
    for key, total in totals.items():
        if total > 100.000001:
            yield Diagnostic(code='quality.coverage_sum', message='Land-use coverage exceeds 100 percent', object_id=key,
                subject=DiagnosticSubject(collection='swmm:subcatchments', key=key), related=tuple(contributors[key]))
    for row in store.collection('swmm:pollutants').values():
        if not ValidationReport(diagnostics=tuple(validate_fields(row))).is_valid:
            continue
        if row.co_pollutant is not None and store.contains(row.co_pollutant):
            other = store.collection('swmm:pollutants')[row.co_pollutant.key]
            if row.units != other.units:
                yield Diagnostic(code='quality.co_units', severity=Severity.WARNING, object_id=row.id,
                    subject=DiagnosticSubject(collection='swmm:pollutants', key=row.id, path=('co_pollutant','key')),
                    related=(DiagnosticSubject(collection='swmm:pollutants', key=row.id, path=('units',)),
                             DiagnosticSubject(collection='swmm:pollutants', key=row.id, path=('co_fraction',)),
                             DiagnosticSubject(collection='swmm:pollutants', key=other.id, path=('units',))),
                    message='Native adds co-pollutant concentrations numerically without converting their declared units; co_fraction must account for the intended conversion')


def guard_pollutant_units(store, action, targets, *, before=None, after=None):
    if (action=='replace' and type(before) is Pollutant and type(after) is Pollutant and before.units!=after.units
            and not store._context_change_allowed('pollutant_units')):
        yield Diagnostic(code='quality.units_context', object_id=before.id,
            subject=DiagnosticSubject(collection='swmm:pollutants', key=before.id, path=('units',)),
            related=tuple(DiagnosticSubject(collection=use.owner.collection, key=use.owner.key, path=use.path)
                for use in store.referenced_by(Ref(collection='swmm:pollutants', key=before.id))),
            message='Changing concentration units requires convert_pollutant_units() or explicit reinterpret_pollutant_units()')


def _convert_pollutant(row, context):
    own = context.owner.canonical == context.target.canonical
    changes = {}
    if own:
        changes = dict(units=context.target_units, **{name: getattr(row, name)*context.factor for name in
            ('rainfall_concentration', 'groundwater_concentration', 'rdii_concentration', 'dwf_concentration', 'initial_concentration')
            if getattr(row, name) is not None})
    if row.co_fraction is not None:
        multiplier = context.factor if own else 1.
        if row.co_pollutant is not None and row.co_pollutant.canonical == context.target.canonical:
            multiplier /= context.factor
        changes['co_fraction'] = row.co_fraction*multiplier
    return PollutantUnitConversion(value=replace(row, **changes))


def _convert_buildup_concentration(row, context):
    if row.pollutant.canonical == context.target.canonical and type(row.function) not in (
            NoBuildup, PowerBuildup, ExponentialBuildup, SaturationBuildup, ExternalBuildup):
        raise ValueError('An extension buildup formula needs an explicit pollutant-unit transform')
    # Surface mass is lb/kg, independent of the mg/ug concentration label.
    return PollutantUnitConversion(value=row)


def _convert_washoff_concentration(row, context):
    if row.pollutant.canonical == context.target.canonical:
        function = row.function
        if type(function) is EventMeanConcentration:
            row = replace(row, function=replace(function, concentration=function.concentration*context.factor))
        elif type(function) is RatingWashoff:
            row = replace(row, function=replace(function, coefficient=function.coefficient*context.factor))
        elif type(function) not in (NoWashoff, ExponentialWashoff):
            raise ValueError('An extension washoff formula needs an explicit pollutant-unit transform')
    return PollutantUnitConversion(value=row)


QUALITY_POLLUTANT_TRANSFORMS = (
    PollutantUnitTransform(value_type=Pollutant, convert=_convert_pollutant),
    PollutantUnitTransform(value_type=InitialLoading, convert=lambda row, context: PollutantUnitConversion(value=row)),
    PollutantUnitTransform(value_type=Buildup, convert=_convert_buildup_concentration),
    PollutantUnitTransform(value_type=Washoff, convert=_convert_washoff_concentration),
)
