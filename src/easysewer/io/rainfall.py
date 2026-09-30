"""User-prepared rainfall files; station units/form are owned by rain gages."""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
import math

from ..model.identity import canonical_key
from ..model.resources import SeriesPoint
from ..validation import Diagnostic, Severity, SourceSpan
from ..validation._cooperative import checkpointed
from ._data_text import TextData, decode_lines, encode_lines, error, tokens
from .inp.geometry import finite_number, number_text
from .timeseries import TimeSeriesData


@dataclass(frozen=True, kw_only=True)
class RainfallReading:
    station: str
    time: datetime
    value: float
    record_date: date | None = None

    def __post_init__(self):
        if not isinstance(self.station,str) or not self.station or any(c.isspace() or c in '\x00;' for c in self.station):
            raise ValueError('A rainfall station must be a nonempty single token')
        if type(self.time) is not datetime or self.time.tzinfo is not None or self.time.fold or self.time.second or self.time.microsecond:
            raise ValueError('User rainfall timestamps require local whole-minute datetimes')
        if type(self.value) not in (int,float) or not math.isfinite(self.value):
            raise ValueError('Rainfall values must be finite numbers')
        if self.record_date is not None:
            if type(self.record_date) is not date:
                raise TypeError('Rainfall record_date must be a calendar date')
            seconds=(self.time-datetime.combine(self.record_date,time())).total_seconds()
            if not 0 <= seconds <= 2147483647:
                raise ValueError('Rainfall clock exceeds the native nonnegative signed-second range')

    @property
    def file_date(self):
        return self.record_date or self.time.date()


@dataclass(frozen=True, kw_only=True)
class RainfallData(TextData):
    readings: tuple[RainfallReading, ...]

    def __post_init__(self):
        if type(self.readings) is not tuple or any(type(v) is not RainfallReading for v in self.readings):
            raise TypeError('Rainfall readings must be an immutable tuple')
        if not self.readings:
            raise ValueError('A rainfall file requires at least one reading')
        last={}
        for reading in checkpointed(self.readings):
            key=canonical_key(reading.station)
            if key in last and reading.time <= last[key]:
                raise ValueError('Rainfall timestamps must increase strictly within each station')
            last[key]=reading.time

    @classmethod
    def from_bytes(cls,data,*,encoding='utf-8',source=None):
        readings,issues=[],[]
        for number,row in decode_lines(data,encoding=encoding,source=source):
            parts=tokens(row)
            if not parts or parts[0].startswith(';'):
                continue
            try:
                if len(parts)<7:
                    raise ValueError('User rainfall requires station/year/month/day/hour/minute/value')
                if not readings and number>5:
                    raise ValueError('Native rainfall format detection requires a data row within the first five physical lines')
                day=date(*(int(v) for v in parts[1:4]))
                hour,minute=(int(v) for v in parts[4:6])
                if hour<0 or not 0<=minute<60:
                    raise ValueError('Rainfall hours must be nonnegative and minutes less than 60')
                stamp=datetime.combine(day,time())+timedelta(hours=hour,minutes=minute)
                readings.append(RainfallReading(station=parts[0],time=stamp,value=finite_number(parts[6]),record_date=day if hour>=24 else None))
                if len(parts)>7:
                    issues.append(Diagnostic(code='rainfall.ignored_columns',severity=Severity.WARNING,
                        message='Native user rainfall ignores trailing columns',span=SourceSpan(source=source,line=number,column=1,end_column=len(row)+1)))
            except (ValueError,OverflowError) as exc:
                error(str(exc),source=source,line=number,text=row)
        return cls(readings=tuple(readings))._retain(data,encoding,issues)

    def station_series(self,station,*,start_date=None):
        points=tuple(SeriesPoint(time=r.time,value=r.value) for r in checkpointed(self.readings)
                     if canonical_key(r.station)==canonical_key(station) and (start_date is None or r.file_date>=start_date))
        return TimeSeriesData(points=points)

    def to_bytes(self,*,encoding=None,normalize=False):
        encoding=encoding or self._encoding
        if self._original is not None and not normalize and encoding==self._encoding:
            return self._original
        rows=[]
        for r in self.readings:
            day=r.file_date
            hour=int((r.time-datetime.combine(day,time())).total_seconds()//3600)
            rows.append(f'{r.station} {day.year:04} {day.month:02} {day.day:02} {hour:02} {r.time.minute:02} {number_text(r.value)}')
        return encode_lines(rows,encoding)
