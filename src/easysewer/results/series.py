"""Portable series values retain their identities, units and sampling provenance."""

from dataclasses import asdict, dataclass
import codecs
from datetime import datetime
import math
import re

from ..model.identity import Ref, namespace_key
from ..io.json import JsonDocument
from .applicability import ResultApplicability


def digest(value):
    if value is not None and (type(value) is not str or not re.fullmatch('[0-9a-f]{64}', value)):
        raise ValueError('Result digests require lowercase SHA-256 hex')


@dataclass(frozen=True, kw_only=True)
class ResultSource:
    format: str
    path: str | None = None
    sha256: str | None = None
    run_id: str | None = None
    input_sha256: str | None = None
    backend_sha256: str | None = None
    engine_version: int | None = None
    encoding: str | None = None

    def __post_init__(self):
        namespace_key(self.format)
        if self.encoding is not None:codecs.lookup(self.encoding)
        for name in ('sha256', 'input_sha256', 'backend_sha256'):digest(getattr(self, name))
        for name in ('path', 'run_id'):
            if getattr(self, name) is not None and type(getattr(self, name)) is not str:
                raise TypeError('Result source identities must be strings')
        if self.engine_version is not None and (type(self.engine_version) is not int or self.engine_version <= 0):
            raise ValueError('Invalid result engine version')


@dataclass(frozen=True, kw_only=True)
class SampleMaximum:
    """A maximum among saved observations, never a solver's internal-step peak."""
    value: float | None
    time: datetime | None
    period: int | None
    missing_count: int
    source: ResultSource
    target: Ref | None
    variable: str
    pollutant: Ref | None
    unit: str | None
    sampling: str
    semantics: str
    reason: str | None = None
    statistic: str = 'easysewer:saved-observation-maximum'
    applicability: ResultApplicability = ResultApplicability()


@dataclass(frozen=True, kw_only=True)
class ResultSeries:
    source: ResultSource
    target: Ref | None
    variable: str
    pollutant: Ref | None
    unit: str | None
    sampling: str
    semantics: str
    periods: tuple[int, ...]
    times: tuple[datetime, ...]
    values: tuple[float | None, ...]
    missing: tuple[str | None, ...]
    applicability: ResultApplicability = ResultApplicability()

    def __post_init__(self):
        if not isinstance(self.applicability, ResultApplicability):raise TypeError('Expected result applicability')
        if not isinstance(self.source, ResultSource):raise TypeError('Expected a result source')
        namespace_key(self.variable)
        if self.target is not None and not isinstance(self.target, Ref):raise TypeError('Expected an object reference')
        if self.pollutant is not None and (not isinstance(self.pollutant, Ref) or self.pollutant.collection != 'swmm:pollutants'):
            raise TypeError('Expected a pollutant reference')
        for name in ('sampling', 'semantics'):
            if type(getattr(self, name)) is not str or not getattr(self, name):raise ValueError('Missing result semantics')
        if self.unit is not None and (type(self.unit) is not str or not self.unit):raise ValueError('Invalid result unit')
        for name in ('periods', 'times', 'values', 'missing'):
            if type(getattr(self, name)) is not tuple:raise TypeError('Series data must be immutable tuples')
        if len({len(self.periods), len(self.times), len(self.values), len(self.missing)}) != 1:
            raise ValueError('Series arrays have different lengths')
        if self.applicability.unavailable and any(v is not None for v in self.values):
            raise ValueError('Uncomputed observations cannot be published as numeric values')
        for i, (period, when, value, reason) in enumerate(zip(self.periods, self.times, self.values, self.missing)):
            if type(period) is not int or period < 0 or (i and period <= self.periods[i-1]):
                raise ValueError('Periods must be increasing nonnegative integers')
            if type(when) is not datetime or when.tzinfo is not None or (i and when <= self.times[i-1]):
                raise ValueError('Times must increase in the timezone-free model calendar')
            if value is None:
                if type(reason) is not str or not reason:raise ValueError('Missing observations need a reason')
            elif type(value) not in (int, float) or not math.isfinite(value) or reason is not None:
                raise ValueError('Observed values must be finite numbers without a missing reason')

    def maximum(self):
        index = max((i for i, value in enumerate(self.values) if value is not None),
                    key=lambda i:self.values[i], default=None)
        return SampleMaximum(value=self.values[index] if index is not None else None,
            time=self.times[index] if index is not None else None,
            period=self.periods[index] if index is not None else None,
            missing_count=sum(value is None for value in self.values), source=self.source, target=self.target,
            variable=self.variable, pollutant=self.pollutant, unit=self.unit, sampling=self.sampling,
            semantics=self.semantics, applicability=self.applicability,
            reason=None if index is not None else ('no_observations' if not self.values else 'all_observations_missing'))

    def to_json_document(self):
        def reference(value):
            if value is None:return None
            return dict(collection=value.collection, key=list(value.key) if isinstance(value.key, tuple) else value.key)
        return JsonDocument.from_data(dict(schema_version='1.1', kind='easysewer:result-series',
            applicability=self.applicability.to_data(),
            source=asdict(self.source), target=reference(self.target), variable=self.variable,
            pollutant=reference(self.pollutant), unit=self.unit, sampling=self.sampling, semantics=self.semantics,
            periods=list(self.periods), times=[when.isoformat(timespec='microseconds') for when in self.times],
            values=list(self.values), missing=list(self.missing)))

    @classmethod
    def from_json_document(cls, document):
        data=document.data
        fields={'schema_version', 'kind', 'source', 'target', 'variable', 'pollutant', 'unit',
                'sampling', 'semantics', 'periods', 'times', 'values', 'missing'}
        if type(data) is not dict or data.get('schema_version') not in ('1.0','1.1') or data.get('kind')!='easysewer:result-series':
            raise ValueError('Unsupported result-series JSON contract')
        expected = fields | ({'applicability'} if data['schema_version']=='1.1' else set())
        if set(data)!=expected:raise ValueError('Invalid result-series fields')
        applicability = ResultApplicability.from_data(data['applicability']) if 'applicability' in data else ResultApplicability()
        def reference(value):
            if value is None:return None
            if type(value) is not dict or set(value)!={'collection', 'key'}:raise ValueError('Invalid result reference')
            return Ref(collection=value['collection'], key=tuple(value['key']) if type(value['key']) is list else value['key'])
        for key in ('periods', 'times', 'values', 'missing'):
            if type(data[key]) is not list:raise ValueError('Expected result arrays')
        return cls(source=ResultSource(**data['source']), applicability=applicability, target=reference(data['target']), variable=data['variable'],
            pollutant=reference(data['pollutant']), unit=data['unit'], sampling=data['sampling'], semantics=data['semantics'],
            periods=tuple(data['periods']), times=tuple(datetime.fromisoformat(value) for value in data['times']),
            values=tuple(data['values']), missing=tuple(data['missing']))
