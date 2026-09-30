"""Editable external SWMM time series with explicit elapsed/calendar semantics."""

from dataclasses import dataclass, replace
from datetime import datetime, time, timedelta
import re

from ..model.fields import validate_fields
from ..model.resources import InlineTimeSeries, SeriesPoint, _series_time_diagnostics
from ..validation import Diagnostic, Severity, SourceSpan, ValidationReport
from ..validation._cooperative import checkpointed
from ._data_text import TextData, decode_lines, encode_lines, error, tokens
from .inp.options import _date
from .inp.resources import _series_time, _time_text
from .inp.geometry import finite_number, number_text


def _file_date(token):
    # Python int also accepts Unicode digits and underscores; the checked
    # native file grammar deliberately accepts only ASCII components.
    if not re.fullmatch(r'(?:[+]?[0-9]+|[A-Za-z]{3})[-/][+]?[0-9]+[-/][+]?[0-9]+', token):
        raise ValueError('Expected ASCII month/day/year')
    return _date(token)


def _file_time(token):
    if ':' in token:
        for component in token.split(':'):
            if int(component.split('.')[0]) > 2_147_483_647:
                raise ValueError('Clock component exceeds the native integer range')
    return _series_time(token)


def _parse_points(data, *, encoding, source, issues):
    """One grammar for editable documents and bounded inspection of valid data."""
    day = None
    for number, row in decode_lines(data, encoding=encoding, source=source):
        parts=tokens(row)
        if not parts or parts[0].startswith(';'):
            continue
        try:
            if len(parts) == 2:
                clock, value=parts
            elif len(parts) >= 3:
                day=_file_date(parts[0]);clock, value=parts[1:3]
            else:
                raise ValueError('Expected time/value or date/time/value')
            elapsed, changed=_file_time(clock)
            when=elapsed if day is None else datetime.combine(day,time())+elapsed
            point=SeriesPoint(time=when, value=finite_number(value))
            for code, message, active in (
                ('timeseries.native_time_coercion','Native clock parsing or microsecond precision changes the supplied time',changed),
                ('timeseries.ignored_columns','Native external-series reader ignores columns after date/time/value',len(parts)>3)):
                if active:
                    issues.append(Diagnostic(code=code,severity=Severity.WARNING,message=message,
                        span=SourceSpan(source=source,line=number,column=1,end_column=len(row)+1)))
        except (ValueError, OverflowError) as exc:
            error(str(exc),source=source,line=number,text=row)
        yield point


@dataclass(frozen=True)
class _SeriesScan:
    count: int
    first: datetime | timedelta
    last: datetime | timedelta
    boundary: tuple[timedelta, datetime] | None
    minimum_gap: float | None
    negative: bool
    issues: tuple[Diagnostic, ...]

    def resolved(self, start):
        if type(start) is not datetime or start.tzinfo is not None or start.fold:
            raise ValueError('An explicit local simulation-start datetime is required')
        resolve=lambda stamp: start+stamp if isinstance(stamp,timedelta) else stamp
        first,last=resolve(self.first),resolve(self.last)
        minimum=self.minimum_gap
        if self.boundary is not None:
            gap=(self.boundary[1]-resolve(self.boundary[0])).total_seconds()
            if gap <= 0:
                return None  # Full document validation supplies exact diagnostic paths.
            minimum=gap if minimum is None else min(minimum,gap)
        basis='relative' if isinstance(self.last,timedelta) else 'mixed' if isinstance(self.first,timedelta) else 'calendar'
        return first,last,minimum,basis


def _scan_series(data, *, encoding, source):
    # _parse_points guarantees finite float values and local datetime/timedelta
    # values without folds. Shared sequence validation is still applied to every
    # point. Keep all warning records, but no complete point or gap collection.
    count=0;first=last=boundary=minimum=None;negative=False;issues=[]

    def times():
        nonlocal count,first,last,boundary,minimum,negative
        for point in _parse_points(data,encoding=encoding,source=source,issues=issues):
            when=point.time
            if not count:
                first=when
            elif type(last) is type(when):
                gap=(when-last).total_seconds()
                minimum=gap if minimum is None else min(minimum,gap)
            else:
                boundary=(last,when)
            count+=1;last=when;negative=negative or point.value<0
            yield when

    invalid=False
    for _ in _series_time_diagnostics(times()):
        invalid=True
    if invalid:
        return None
    return _SeriesScan(count,first,last,boundary,minimum,negative,tuple(issues))


@dataclass(frozen=True, kw_only=True)
class TimeSeriesData(TextData):
    points: tuple[SeriesPoint, ...]

    def __post_init__(self):
        if type(self.points) is not tuple or any(type(p) is not SeriesPoint for p in self.points):
            raise TypeError('Time-series points must be an immutable tuple of SeriesPoint values')
        ValidationReport(diagnostics=tuple(validate_fields(InlineTimeSeries(id='Data', points=self.points)))).raise_for_errors()
        if any(isinstance(p.time, datetime) and p.time.fold for p in self.points):
            raise ValueError('External timestamps require unambiguous local model time')

    @classmethod
    def from_bytes(cls, data, *, encoding='utf-8', source=None):
        issues=[]
        points=tuple(_parse_points(data,encoding=encoding,source=source,issues=issues))
        return cls(points=points)._retain(data,encoding,issues)

    def resolved(self, start):
        """Return calendar points at the caller's intended simulation origin."""
        if type(start) is not datetime or start.tzinfo is not None or start.fold:
            raise ValueError('An explicit local simulation-start datetime is required')
        return replace(self,points=tuple(replace(p,time=start+p.time) if isinstance(p.time,timedelta) else p for p in checkpointed(self.points)))

    def as_inline(self, id, *, start=None):
        points=self.points if start is None else self.resolved(start).points
        return InlineTimeSeries(id=id,points=points)

    def to_bytes(self, *, encoding=None, normalize=False):
        encoding=encoding or self._encoding
        if self._original is not None and not normalize and encoding==self._encoding:
            return self._original
        rows=[]
        for point in self.points:
            if isinstance(point.time, datetime):
                stamp=point.time
                head=f'{stamp.month:02}/{stamp.day:02}/{stamp.year:04} '+_time_text(stamp-datetime.combine(stamp.date(),time()))
            else:
                head=_time_text(point.time)
            rows.append(head+' '+number_text(point.value))
        return encode_lines(rows,encoding)
