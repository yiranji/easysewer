"""Historical NWS/Canadian rainfall documents and explicit native interpretation."""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
import math
import struct

from ..validation import Diagnostic, Severity, ValidationReport
from ..validation._cooperative import checkpointed
from ._data_text import TextData, encode_lines
from .rainfall import RainfallData, RainfallReading

FORMATS=('NWS_TAPE','NWS_SPACE_DELIMITED','NWS_COMMA_DELIMITED',
         'NWS_ONLINE_60','NWS_ONLINE_15','AES_HLY','CMC_HLY','CMC_FIF')
CANADIAN=('AES_HLY','CMC_HLY','CMC_FIF')
ONLINE=('NWS_ONLINE_60','NWS_ONLINE_15')


def _f32(value):
    return struct.unpack('<f',struct.pack('<f',value))[0]


def _ascii(value,width=None):
    if not isinstance(value,str) or not value.isascii() or any(c in value for c in '\r\n\x00\x1a'):
        raise ValueError('Historical rainfall metadata requires single-line ASCII text')
    if width is not None and len(value)!=width:
        raise ValueError(f'Historical rainfall field requires {width} characters')


@dataclass(frozen=True,kw_only=True)
class ArchiveRainfallObservation:
    hour: int
    minute: int
    value: int | float
    condition: str = ' '
    quality: str = ' '
    decimal_inches: bool = False

    def __post_init__(self):
        if type(self.hour) is not int or not 0<=self.hour<=99 or type(self.minute) is not int or not 0<=self.minute<(60 if self.hour<25 else 100):
            raise ValueError('Historical rainfall requires valid clock fields or a two-digit hour>=25 native terminator')
        if type(self.value) not in (int,float) or not math.isfinite(self.value):
            raise ValueError('Historical rainfall values must be finite')
        _ascii(self.condition,1);_ascii(self.quality,1)
        if type(self.decimal_inches) is not bool:
            raise TypeError('decimal_inches must be boolean')


@dataclass(frozen=True,kw_only=True)
class ArchiveRainfallRecord:
    station: str
    date: date
    element: str
    observations: tuple[ArchiveRainfallObservation,...]
    division: str = '00'
    units_code: str = 'HI'
    record_type: str = 'HPD'
    station_name: str | None = None

    def __post_init__(self):
        if type(self.date) is not date:
            raise TypeError('Historical rainfall record requires a calendar date')
        _ascii(self.station);_ascii(self.element)
        _ascii(self.division,2);_ascii(self.units_code,2);_ascii(self.record_type,3)
        if self.station_name is not None:_ascii(self.station_name)
        if type(self.observations) is not tuple or not self.observations or any(type(v) is not ArchiveRainfallObservation for v in self.observations):
            raise TypeError('Historical rainfall observations require a nonempty immutable tuple')


@dataclass(frozen=True,kw_only=True)
class RainfallInterval:
    time: datetime
    record_date: date
    depth_inches: float | None
    status: str
    saved: bool
    station: str

    def __post_init__(self):
        if type(self.time) is not datetime or self.time.tzinfo is not None or self.time.fold or self.time.second or self.time.microsecond:
            raise ValueError('Archive interval starts require local whole-minute datetimes')
        if type(self.record_date) is not date:raise TypeError('record_date requires a calendar date')
        if self.depth_inches is not None and (type(self.depth_inches) not in (int,float) or not math.isfinite(self.depth_inches)):
            raise ValueError('Interpreted rain depth must be finite or missing')
        if self.status not in ('observed','missing','deleted','accumulated') or type(self.saved) is not bool:
            raise ValueError('Invalid interpreted rainfall status')
        if self.saved and (self.depth_inches is None or self.depth_inches<=0):raise ValueError('A saved archive interval requires positive depth')
        _ascii(self.station)


@dataclass(frozen=True,kw_only=True)
class InterpretedRainfall:
    interval: timedelta
    periods: tuple[RainfallInterval,...]
    report: ValidationReport

    def __post_init__(self):
        if type(self.interval) is not timedelta or self.interval<=timedelta():raise ValueError('Rainfall interval must be positive')
        if type(self.periods) is not tuple or any(type(p) is not RainfallInterval for p in self.periods):raise TypeError('Interpreted periods require an immutable tuple')
        if not isinstance(self.report,ValidationReport):raise TypeError('Interpreted rainfall requires a ValidationReport')


@dataclass(frozen=True,kw_only=True)
class HistoricalRainfallData(TextData):
    format: str
    records: tuple[ArchiveRainfallRecord,...]

    def __post_init__(self):
        if self.format not in FORMATS:
            raise ValueError('Unknown historical rainfall format')
        if type(self.records) is not tuple or not self.records or any(type(r) is not ArchiveRainfallRecord for r in self.records):
            raise TypeError('Historical rainfall records require a nonempty immutable tuple')
        previous=None
        for r in checkpointed(self.records):
            digits=7 if self.format in CANADIAN else 6
            if len(r.station)!=digits or not r.station.isdecimal():
                raise ValueError(f'This rainfall format requires a {digits}-digit station')
            if previous is not None and r.date<previous:
                raise ValueError('Historical rainfall record dates must not move backwards')
            previous=r.date
            if self.format in CANADIAN:
                if len(r.element)!=3 or not r.element.isdecimal():
                    raise ValueError('Canadian rainfall element must be a three-digit code')
                if r.station_name is not None:
                    raise ValueError('Canadian rainfall records do not contain station names')
                if self.format=='AES_HLY' and not 1100<=r.date.year<=2099:
                    raise ValueError('Native three-digit AES years represent 1100..2099')
                count=96 if self.format=='CMC_FIF' else 24
                minutes=15 if self.format=='CMC_FIF' else 60
                if len(r.observations)!=count:
                    raise ValueError('Canadian records require every daily slot')
                if tuple((v.hour,v.minute) for v in r.observations)!=tuple(divmod(i*minutes,60) for i in range(count)):
                    raise ValueError('Canadian daily slots have a fixed clock order beginning at 00:00')
            else:
                _ascii(r.element,4)
                if not r.division.isdecimal():raise ValueError('NWS division must be numeric')
                if r.station_name is not None and (self.format!='NWS_SPACE_DELIMITED' or len(r.station_name)>30):
                    raise ValueError('Only the native NWS space format supports an optional 30-character station name')
                if self.format in ONLINE:
                    if len(r.observations)!=1 or r.element!=('HPCP' if self.format=='NWS_ONLINE_60' else 'QPCP'):
                        raise ValueError('Online records require one observation of the header element')
                elif r.element not in ('HPCP','QPCP','QGAG'):
                    raise ValueError('NWS rainfall element requires HPCP, QPCP or QGAG')
            for v in r.observations:
                if v.decimal_inches:
                    if self.format not in ONLINE:
                        raise ValueError('Only online rainfall supports decimal-inch tokens')
                    scaled=_f32(_f32(_f32(v.value)*100)+.5)
                    if not math.isfinite(scaled) or not -2147483648<=scaled<=2147483647:
                        raise ValueError('Online decimal rainfall overflows the native signed-long conversion')
                elif int(v.value)!=v.value or (not -2147483648<=v.value<=2147483647 if self.format in ONLINE else len(str(int(v.value)))>6):
                    raise ValueError('Historical integer rainfall exceeds its native field width or signed-long range')
                if self.format in CANADIAN and v.condition!=' ':
                    raise ValueError('Canadian slots have a quality flag, not a condition field')
                if self.format in ONLINE and v.quality!=' ':
                    raise ValueError('Online records have a condition field, not a second quality code')
        if self.format in CANADIAN and self.records[0].element!=('159' if self.format=='CMC_FIF' else '123'):
            raise ValueError('Native format detection requires a rainfall element in the first record')
        if self.format=='NWS_SPACE_DELIMITED' and len({r.station_name is not None for r in self.records})!=1:
            raise ValueError('Native NWS station-name layout is fixed by the first record')

    @property
    def interval(self):
        if self.format in ('CMC_FIF','NWS_ONLINE_15'):return timedelta(minutes=15)
        if self.format in ('AES_HLY','CMC_HLY','NWS_ONLINE_60'):return timedelta(hours=1)
        return timedelta(hours=1) if self.records[0].element=='HPCP' else timedelta(minutes=15)

    @property
    def report(self):
        issues=list(self._diagnostics)
        def warn(code,message):issues.append(Diagnostic(code=code,message=message,severity=Severity.WARNING))
        if len({r.station for r in self.records})>1:
            warn('rainfall.archive_multiple_stations','Native historical readers do not filter station identifiers; all records are consumed')
        if len({r.element for r in self.records})>1:
            warn('rainfall.archive_mixed_elements','Native interval is fixed by the first record; Canadian non-rainfall elements are ignored')
        if self.format in CANADIAN and any(v.quality.strip() for r in self.records for v in r.observations):
            warn('rainfall.archive_ignored_quality','Canadian quality flags are retained but native missing data is selected only by -99999')
        if self.format=='NWS_SPACE_DELIMITED' and any(not v.condition.strip() and v.quality.strip() for r in self.records for v in r.observations):
            warn('rainfall.archive_shifted_flag','Native space scanning promotes a quality code into a blank condition field')
        if any(v.hour>=25 for r in self.records for v in r.observations):
            warn('rainfall.archive_time_terminator','Native stops the current row at hour>=25; remaining source groups are retained but not consumed')
        return ValidationReport(diagnostics=tuple(issues))

    @classmethod
    def from_bytes(cls,data,*,encoding='utf-8',source=None):
        from ._historical_rain_text import parse
        kind,records,issues=parse(data,encoding=encoding,source=source)
        return cls(format=kind,records=records)._retain(data,encoding,issues)

    def to_bytes(self,*,encoding=None,normalize=False):
        from ._historical_rain_text import render
        encoding=encoding or self._encoding
        if self._original is not None and not normalize and encoding==self._encoding:return self._original
        data=encode_lines(render(self),encoding)
        parsed=type(self).from_bytes(data,encoding=encoding)
        if parsed!=self:
            raise ValueError('Historical rainfall metadata/values cannot be represented exactly')
        return data

    def interpret(self,*,start_date=None,end_date=None,max_periods=1_000_000,arithmetic='reference'):
        """Apply native date filters/flags; return interval starts and inch depths.

        Raw quality/missing/accumulation records remain in the document. This
        view includes unsaved missing and zero periods with explicit status.
        """
        if start_date is not None and type(start_date) is not date or end_date is not None and type(end_date) is not date:
            raise TypeError('Rainfall date filters require calendar dates')
        if start_date is not None and end_date is not None and end_date<start_date:
            raise ValueError('Rainfall end date precedes start date')
        if type(max_periods) is not int or max_periods<=0:raise ValueError('max_periods must be a positive integer')
        if arithmetic not in ('reference','float32_reciprocal_100'):
            raise ValueError('Unknown rainfall arithmetic policy')
        def hundredths(value):
            # /fp:fast builds can replace division by the constant 100.f
            # with a rounded float reciprocal. Keep that build choice explicit.
            return _f32(value*_f32(.01)) if arithmetic=='float32_reciprocal_100' else _f32(value/100)
        periods=[];issues=list(self.report.diagnostics);anchor=None;last=None
        interval=self.interval
        def warning(code,message):issues.append(Diagnostic(code=code,message=message,severity=Severity.WARNING))
        def append(when,r,depth,status,saved):
            nonlocal last
            if len(periods)>=max_periods:raise ValueError('Expanded rainfall exceeds the explicit period budget')
            if saved:
                if last is not None and when<=last:
                    raise ValueError('Native rainfall cache would contain duplicate or decreasing interval starts')
                last=when
            periods.append(RainfallInterval(time=when,record_date=r.date,depth_inches=depth,status=status,saved=saved,station=r.station))
        for r in checkpointed(self.records):
            if start_date is not None and r.date<start_date:continue
            if end_date is not None and r.date>end_date:break
            if self.format in CANADIAN and r.element!=('159' if self.format=='CMC_FIF' else '123'):continue
            for v in r.observations:
                if v.hour>=25:break
                stamp=datetime.combine(r.date,time())+timedelta(hours=v.hour,minutes=v.minute)
                # The online reader changes hour 00 to 24 on the preceding
                # day; the resulting timestamp is identical, including 00:15.
                when=stamp-interval
                if self.format in CANADIAN:
                    missing=v.value==-99999
                    depth=None if missing else _f32(v.value/10/25.4)
                    append(when,r,depth,'missing' if missing else 'observed',not missing and depth>0)
                    continue
                amount=int(_f32(_f32(_f32(v.value)*100)+.5)) if v.decimal_inches else int(v.value)
                flag=v.condition
                if self.format=='NWS_SPACE_DELIMITED' and not flag.strip():flag=v.quality
                if flag=='a':
                    if anchor is not None:warning('rainfall.accumulation_restarted','A new accumulation start replaces the previous unfinished start')
                    anchor=stamp
                    continue
                if flag=='A':
                    if anchor is None:
                        warning('rainfall.accumulation_without_start','Native ignores an accumulation end without a retained start')
                        continue
                    seconds=(stamp-anchor).total_seconds()
                    n=int(seconds/interval.total_seconds())+1
                    if n<=0:raise ValueError('Accumulation end precedes its start')
                    if n>max_periods-len(periods):raise ValueError('Expanded rainfall exceeds the explicit period budget')
                    if seconds%interval.total_seconds():warning('rainfall.accumulation_partial_interval','Native truncates a nonintegral accumulated interval count')
                    depth=None if amount==99999 else hundredths(_f32(_f32(amount)/_f32(n)))
                    for index in checkpointed(range(n)):append(anchor-interval+index*interval,r,depth,'missing' if depth is None else 'accumulated',depth is not None and depth>0)
                    if amount!=99999:anchor=None
                    else:warning('rainfall.missing_accumulation_keeps_start','Native missing accumulated value leaves its start active for later end markers')
                    continue
                missing=flag in '{}[]M' or amount>=9999
                status='deleted' if flag in '{}' else 'missing' if missing else 'observed'
                depth=None if missing else hundredths(_f32(amount))
                append(when,r,depth,status,not missing and depth>0)
        if anchor is not None:warning('rainfall.unfinished_accumulation','An unfinished accumulation remains after the selected records')
        if any(p.depth_inches is not None and p.depth_inches<0 for p in periods):
            warning('rainfall.archive_negative_ignored','Native historical readers omit negative recorded rainfall from the cache')
        return InterpretedRainfall(interval=interval,periods=tuple(periods),report=ValidationReport(diagnostics=tuple(issues)))

    def as_user(self,*,station='Rain',start_date=None,end_date=None,missing='error',max_periods=1_000_000,arithmetic='reference'):
        """Materialize cache values as USER VOLUME/IN at ``self.interval``.

        This deliberately loses raw flags and source record-date filters. Apply
        those filters here and clear the gage file-start date on the new file.
        Missing periods require an explicit omission policy.
        """
        if missing not in ('error','omit'):raise ValueError('missing policy must be error or omit')
        data=self.interpret(start_date=start_date,end_date=end_date,max_periods=max_periods,arithmetic=arithmetic)
        if missing=='error' and any(p.depth_inches is None for p in data.periods):
            raise ValueError('USER rainfall has no missing/deleted marker; explicitly choose missing="omit"')
        data.report.raise_for_errors()
        result=RainfallData(readings=tuple(RainfallReading(station=station,time=p.time,value=p.depth_inches) for p in data.periods if p.saved))
        return result._retain(None,'utf-8',data.report.diagnostics+(Diagnostic(code='rainfall.archive_materialized',severity=Severity.INFO,
            message='Materialized USER data requires VOLUME/IN, the archive interval, and cleared file date filters; original flags and station identities are not retained'),))
