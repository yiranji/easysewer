"""Ordered seasonal unit hydrographs and nodal rainfall-dependent inflows."""

from dataclasses import dataclass
import math
from typing import Literal

from ..validation import Diagnostic, DiagnosticSubject, Severity, ValidationReport
from .fields import number, reference, validate_fields
from .identity import Ref, canonical_key
from .network import Entity
from .options import get_options
from .store import CollectionSpec

MONTHS = ('JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN', 'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC')
RESPONSES = ('SHORT', 'MEDIUM', 'LONG')


@dataclass(frozen=True, kw_only=True)
class HydrographResponse:
    month: Literal['ALL', 'JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN', 'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC']
    response: Literal['SHORT', 'MEDIUM', 'LONG']
    fraction: float = number('ratio', minimum=0)
    time_to_peak: float = number('hours', minimum=0)
    recession_ratio: float = number('ratio', minimum=0)
    maximum_abstraction: float | None = number('rain_depth', None, minimum=0)
    recovery_rate: float | None = number('rain_depth_per_day', None, minimum=0)
    initial_abstraction: float | None = number('rain_depth', None, minimum=0)

    @property
    def native_peak_seconds(self):
        return int(self.time_to_peak * 3600)

    @property
    def native_base_seconds(self):
        return int(self.time_to_peak * (1 + self.recession_ratio) * 3600)

    def validate_local(self):
        for name, value in (('time_to_peak', self.time_to_peak*3600),
                            ('recession_ratio', self.time_to_peak*(1+self.recession_ratio)*3600)):
            if not math.isfinite(value) or value > 2147483647:
                yield Diagnostic(code='rdii.native_time_range', message='Unit hydrograph duration exceeds portable native signed-long seconds', field=name)
        if (self.initial_abstraction or 0) > (self.maximum_abstraction or 0):
            yield Diagnostic(code='rdii.initial_abstraction_range', severity=Severity.WARNING,
                message='Initial abstraction already used exceeds its maximum; native retains it until recovery', field='initial_abstraction')


@dataclass(frozen=True, kw_only=True)
class UnitHydrograph(Entity):
    rain_gage: Ref | None = reference('swmm:raingages', None)
    prior_rain_gages: tuple[Ref, ...] = ()
    responses: tuple[HydrographResponse, ...] = ()

    def validate_local(self):
        if self.rain_gage is None and not self.responses:
            yield Diagnostic(code='rdii.empty_group', message='A hydrograph needs a gage declaration or response to be represented in INP')
        if self.prior_rain_gages and self.rain_gage is None:
            yield Diagnostic(code='rdii.final_gage_required', message='Prior gage assignments require a final rain gage assignment', field='rain_gage')
        for index, gage in enumerate(self.prior_rain_gages):
            if gage.collection != 'swmm:raingages':
                yield Diagnostic(code='model.invalid_field', message='Expected a reference to swmm:raingages', field=f'prior_rain_gages[{index}]')

    def for_month(self, month):
        """Return SHORT/MEDIUM/LONG after applying assignments in file order."""
        if type(month) is not int or not 1 <= month <= 12:
            raise ValueError('month must be an integer from 1 to 12')
        ValidationReport(diagnostics=tuple(validate_fields(self))).raise_for_errors()
        values = [None, None, None]
        for row in self.responses:
            if row.month in ('ALL', MONTHS[month-1]):
                values[RESPONSES.index(row.response)] = row
        return tuple(values)


@dataclass(frozen=True, kw_only=True)
class RdiiInflow:
    node: Ref = reference('swmm:nodes')
    hydrograph: Ref = reference('swmm:hydrographs')
    sewer_area: float = number('catchment_area', minimum=0)


RDII_COLLECTIONS = (
    CollectionSpec(key='swmm:hydrographs', record_type=UnitHydrograph, key_of=lambda row: row.id,
                   identity_field='id', validate=validate_fields),
    CollectionSpec(key='swmm:rdii', record_type=RdiiInflow, key_of=lambda row: row.node.key, validate=validate_fields),
)


def validate_rdii(store, profile, *, for_run=False):
    groups = store.collection('swmm:hydrographs')
    options = get_options(store)
    used = {}
    for binding in store.collection('swmm:rdii').values():
        if ValidationReport(diagnostics=tuple(validate_fields(binding))).is_valid:
            used.setdefault(canonical_key(binding.hydrograph.key), []).append(
                DiagnosticSubject(collection='swmm:rdii', key=binding.node.key, path=('hydrograph',)))
    has_files = 'swmm:files' in {s.key for s in store.specifications}
    generation = not (has_files and ('RDII', 'USE') in store.collection('swmm:files'))
    generation = generation and not options.ignore_rainfall and not options.ignore_rdii
    for row in groups.values():
        if not ValidationReport(diagnostics=tuple(validate_fields(row))).is_valid:
            continue
        def subject(path):
            return DiagnosticSubject(collection='swmm:hydrographs', key=row.id, path=path)

        positions = {id(response): index for index, response in enumerate(row.responses)}
        if for_run and generation and canonical_key(row.id) in used and row.rain_gage is None:
            yield Diagnostic(code='rdii.missing_gage', message='Generating RDII requires a rain gage for each used unit hydrograph', object_id=row.id,
                subject=subject(('rain_gage',)), related=tuple(used[canonical_key(row.id)]))
        for month in range(1, 13):
            active = [r for r in row.for_month(month) if r is not None and r.native_base_seconds > 0]
            ratio = sum(r.fraction for r in active)
            if ratio > 1.01:
                yield Diagnostic(code='rdii.response_sum', message=f'{MONTHS[month-1]} response ratios exceed native 1.01 limit', object_id=row.id, field='responses',
                    subject=subject(('responses',)), related=tuple(subject(('responses',positions[id(r)])) for r in active))
            elif ratio > 1:
                yield Diagnostic(code='rdii.response_tolerance', severity=Severity.WARNING,
                    subject=subject(('responses',)), related=tuple(subject(('responses',positions[id(r)])) for r in active),
                    message=f'{MONTHS[month-1]} response ratios exceed one within native tolerance', object_id=row.id, field='responses')
        if for_run and generation:
            for index, response in enumerate(row.responses):
                peak = response.time_to_peak*3600
                base = response.time_to_peak*(1+response.recession_ratio)*3600
                if peak != int(peak) or base != int(base):
                    yield Diagnostic(code='rdii.truncated_seconds', severity=Severity.WARNING,
                        subject=subject(('responses',index)), related=(subject(('responses',)),),
                        message='Native hydrograph peak/base durations truncate to whole seconds', object_id=row.id, field=f'responses[{index}]')
                if response.fraction and response.native_base_seconds == 0:
                    yield Diagnostic(code='rdii.inactive_response', severity=Severity.WARNING,
                        subject=subject(('responses',index)), related=(subject(('responses',)),),
                        message='Zero native base duration disables this response despite a positive fraction', object_id=row.id, field=f'responses[{index}]')
