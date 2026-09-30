"""Editable SWMM climate documents, preserving format-specific observations.

Values are in the source format's units. ``daily`` is an explicit conversion to
US or SI daily values; it does not interpolate temperature or fill missing days.
"""

from dataclasses import dataclass, replace
from datetime import date, timedelta
import calendar
import math

from ..validation import Diagnostic, Severity, ValidationReport
from ..validation._cooperative import checkpointed
from ._data_text import TextData, encode_lines

PARAMETERS = ('TMAX', 'TMIN', 'EVAP', 'WDMV', 'AWND')
FORMATS = ('USER_PREPARED', 'GHCND', 'TD3200', 'DLY0204')


def _text(value, width=None):
    if not isinstance(value, str) or any(c in value for c in '\r\n\x00\x1a'):
        raise ValueError('Climate metadata must be single-line text')
    if width is not None and (len(value) != width or not value.isascii()):
        raise ValueError(f'Fixed-width metadata requires exactly {width} ASCII characters')


@dataclass(frozen=True, kw_only=True)
class ClimateObservation:
    parameter: str
    value: float | None
    day: int | None = None
    sign: str = '+'
    measurement_flag: str = ''
    quality_flag: str = ''
    time_code: str = ''

    def __post_init__(self):
        _text(self.parameter)
        if not self.parameter or any(c.isspace() for c in self.parameter):
            raise ValueError('Climate parameter requires one token')
        if self.value is not None and (type(self.value) not in (int, float) or not math.isfinite(self.value)):
            raise ValueError('Climate value must be finite or None for missing data')
        if self.day is not None and (type(self.day) is not int or not 1 <= self.day <= 31):
            raise ValueError('Monthly observation day must be in 1..31')
        if self.sign not in ('+', '-', ' '):
            raise ValueError('Climate sign must be +, - or space')
        for item in (self.measurement_flag, self.quality_flag, self.time_code):
            _text(item)


@dataclass(frozen=True, kw_only=True)
class ClimateRecord:
    station: str
    date: date
    observations: tuple[ClimateObservation, ...]
    parameter: str | None = None
    units_code: str = '  '
    extra_fields: tuple[tuple[str, str], ...] = ()

    def __post_init__(self):
        _text(self.station)
        if type(self.date) is not date or not 1 <= self.date.year <= 9999:
            raise TypeError('Climate record requires a calendar date')
        if type(self.observations) is not tuple or any(type(v) is not ClimateObservation for v in self.observations):
            raise TypeError('Climate observations require an immutable tuple')
        if self.parameter is not None: _text(self.parameter)
        _text(self.units_code, 2)
        if type(self.extra_fields) is not tuple:
            raise TypeError('Extra fields require an immutable tuple')
        seen=set()
        for pair in self.extra_fields:
            if type(pair) is not tuple or len(pair) != 2:
                raise TypeError('Extra fields require (column, text) pairs')
            name, value=pair
            _text(name); _text(value)
            if not name or name in seen or any(c.isspace() for c in name) or name in ('DATE', 'STATION', *PARAMETERS):
                raise ValueError('Extra climate columns must have distinct, unreserved names')
            seen.add(name)


@dataclass(frozen=True, kw_only=True)
class ClimateDay:
    date: date
    maximum: float | None = None
    minimum: float | None = None
    evaporation: float | None = None
    wind: float | None = None

    def __post_init__(self):
        if type(self.date) is not date:
            raise TypeError('A daily climate value requires a calendar date')
        for name in ('maximum','minimum','evaporation','wind'):
            value=getattr(self,name)
            if value is not None and (type(value) not in (int,float) or not math.isfinite(value)):
                raise ValueError('Converted daily climate values must be finite or missing')


@dataclass(frozen=True, kw_only=True)
class ClimateData(TextData):
    format: str
    records: tuple[ClimateRecord, ...]

    def __post_init__(self):
        if self.format not in FORMATS:
            raise ValueError('Unknown SWMM climate format')
        if type(self.records) is not tuple or not self.records or any(type(r) is not ClimateRecord for r in self.records):
            raise TypeError('Climate records require a nonempty immutable tuple')
        previous=None
        for record in checkpointed(self.records):
            month=(record.date.year, record.date.month)
            if previous is not None and month < previous:
                raise ValueError('Climate record months must not move backwards')
            previous=month
            observations=record.observations
            if self.format != 'TD3200' and record.units_code != '  ':
                raise ValueError('Only TD3200 records have a units code')
            if self.format in ('USER_PREPARED', 'GHCND'):
                if not observations or record.parameter is not None:
                    raise ValueError('Daily records require observations and no monthly parameter')
                parameters=[v.parameter for v in observations]
                if len(set(parameters)) != len(parameters) or any(p not in PARAMETERS for p in parameters):
                    raise ValueError('Daily climate parameters must be distinct known variables')
                if any(v.day is not None or v.sign != '+' or v.measurement_flag or v.time_code for v in observations):
                    raise ValueError('Daily values cannot carry monthly record metadata')
                if self.format == 'USER_PREPARED':
                    if any(c.isspace() for c in record.station) or not record.station:
                        raise ValueError('User climate station requires a nonempty token')
                    if any(v.parameter == 'WDMV' or v.quality_flag for v in observations) or record.extra_fields:
                        raise ValueError('User climate format cannot represent these extra fields')
                elif any(v.value is not None and abs(v.value) >= 9999 for v in observations):
                    raise ValueError('GHCND values with magnitude >=9999 are reserved for missing data')
            else:
                _text(record.station, 8 if self.format == 'TD3200' else 7)
                if record.date.day != 1 or record.extra_fields:
                    raise ValueError('Monthly records require month-start dates and no extra columns')
                parameters={v.parameter for v in observations}
                if record.parameter is not None: parameters.add(record.parameter)
                if len(parameters) != 1:
                    raise ValueError('Each monthly record describes one parameter')
                parameter=next(iter(parameters))
                if self.format=='TD3200': _text(parameter,4)
                for v in observations:
                    if v.day is None or v.value is not None and (v.value < 0 or int(v.value) != v.value or v.value > 99998):
                        raise ValueError('Monthly values require a day and unsigned integer magnitude <=99998')
                    _text(v.quality_flag, 1)
                    if self.format == 'TD3200':
                        _text(v.parameter, 4); _text(v.time_code, 2); _text(v.measurement_flag, 1)
                    elif (v.parameter not in ('TMAX', 'TMIN', 'EVAP') and not
                          (len(v.parameter)==3 and v.parameter.isascii() and v.parameter.isdecimal())) or v.time_code or v.measurement_flag:
                        raise ValueError('DLY0204 parameter requires TMAX/TMIN/EVAP or an unconsumed three-digit code')
                if self.format == 'DLY0204' and tuple(v.day for v in observations) != tuple(range(1, 32)):
                    raise ValueError('DLY0204 requires exactly 31 ordered daily slots')
                if self.format == 'TD3200' and len(observations) > 31:
                    raise ValueError('TD3200 requires at most 31 daily items per row')

    @property
    def report(self):
        issues=list(self._diagnostics)
        def warn(code, message):
            issues.append(Diagnostic(code=code, message=message, severity=Severity.WARNING))
        if len({r.station for r in self.records}) > 1:
            warn('climate.multiple_stations', 'Native climate readers do not select stations; later nonmissing values overwrite daily slots')
        if any(v.parameter not in PARAMETERS for r in self.records for v in r.observations) or any(r.parameter is not None and r.parameter not in PARAMETERS for r in self.records):
            warn('climate.ignored_parameter', 'Native climate reader ignores unrecognized monthly parameters')
        if self.format == 'TD3200' and any(v.parameter == 'AWND' for r in self.records for v in r.observations):
            warn('climate.ignored_awnd', 'Native TD3200 reader consumes WDMV, not AWND')
        if self.format == 'GHCND':
            if any(v.parameter=='WDMV' for r in self.records for v in r.observations) and any(v.parameter=='AWND' for r in self.records for v in r.observations):
                warn('climate.wind_precedence', 'Native GHCND reader selects WDMV for the whole file and ignores AWND, including when WDMV is missing')
            if any(v.quality_flag for r in self.records for v in r.observations):
                warn('climate.ignored_quality_flags', 'Native GHCND reader does not apply data quality flags')
        if self.format == 'DLY0204' and any(v.quality_flag.strip(' 0') for r in self.records for v in r.observations):
            warn('climate.ignored_quality_flags', 'Native DLY0204 reader does not apply the daily quality code')
        if self.format == 'DLY0204' and any(v.parameter == 'EVAP' and v.sign == '-' and v.value is not None for r in self.records for v in r.observations):
            warn('climate.ignored_evaporation_sign', 'Native DLY0204 evaporation ignores the sign character')
        return ValidationReport(diagnostics=tuple(issues))

    @classmethod
    def from_bytes(cls, data, *, encoding='utf-8', source=None):
        from ._climate_text import parse
        format, records, issues=parse(data, encoding=encoding, source=source)
        return cls(format=format, records=records)._retain(data, encoding, issues)

    def to_bytes(self, *, encoding=None, normalize=False):
        from ._climate_text import render
        encoding=encoding or self._encoding
        if self._original is not None and encoding == self._encoding and not normalize:
            return self._original
        data=encode_lines(render(self), encoding)
        # Reparse to check fixed-width precision and native detection/buffer rules.
        parsed=type(self).from_bytes(data, encoding=encoding)
        expected=tuple(replace(r,parameter=r.parameter or r.observations[0].parameter) for r in self.records) if self.format in ('TD3200','DLY0204') else ()
        if self.format in ('TD3200', 'DLY0204') and parsed.records != expected:
            raise ValueError('Climate values or metadata cannot be represented exactly in the chosen native format')
        return data

    def daily(self, *, unit_system, ghcnd_units=None):
        """Resolve daily slots in file order, retaining None for missing data.

        Temperatures and evaporation use the requested project system. Wind is
        always mph, matching the native climate-file channel (including USER).
        Station identifiers are intentionally not a filter.
        """
        if unit_system not in ('US', 'SI'):
            raise ValueError('unit_system must be US or SI')
        if self.format == 'GHCND' and ghcnd_units not in ('C10', 'C', 'F'):
            raise ValueError('GHCND conversion requires explicit C10/C/F units')
        values={}
        wdmv=any(v.parameter == 'WDMV' for r in self.records for v in r.observations)
        for record in checkpointed(self.records):
            if not record.observations:
                # A zero-count TD3200 row still establishes its month for
                # climate_openFile. Keep it when materializing daily data.
                values.setdefault(record.date,{})
            for obs in record.observations:
                p, v=obs.parameter, obs.value
                if obs.day is not None and obs.day > calendar.monthrange(record.date.year, record.date.month)[1]:
                    continue
                when=record.date if obs.day is None else record.date.replace(day=obs.day)
                slot=values.setdefault(when, {})
                if v is None or p not in PARAMETERS:
                    continue
                if self.format == 'TD3200':
                    if obs.quality_flag not in ('0', '1') or p == 'AWND':
                        continue
                    v*= -1 if obs.sign == '-' else 1
                    if p in ('TMAX', 'TMIN') and unit_system == 'SI': v=(v-32)*5/9
                    elif p == 'EVAP': v=v/100*(25.4 if unit_system == 'SI' else 1)
                    elif p == 'WDMV': v/=24
                elif self.format == 'DLY0204':
                    v/=10
                    if p in ('TMAX', 'TMIN'):
                        v*= -1 if obs.sign == '-' else 1
                        if unit_system == 'US': v=v*9/5+32
                    elif p == 'EVAP' and unit_system == 'US': v/=25.4
                elif self.format == 'GHCND':
                    if p == 'AWND' and wdmv: continue
                    if p in ('TMAX', 'TMIN'):
                        if ghcnd_units == 'C10': v/=10
                        if ghcnd_units != 'F' and unit_system == 'US': v=v*9/5+32
                        elif ghcnd_units == 'F' and unit_system == 'SI': v=(v-32)*5/9
                    elif p == 'EVAP':
                        if ghcnd_units == 'C10': v/=10
                        if ghcnd_units != 'F' and unit_system == 'US': v/=25.4
                        elif ghcnd_units == 'F' and unit_system == 'SI': v*=25.4
                    elif p == 'WDMV': v=v/24*(.62137 if ghcnd_units != 'F' else 1)
                    elif p == 'AWND' and ghcnd_units != 'F': v=v*.62137*3.6/(10 if ghcnd_units == 'C10' else 1)
                key={'TMAX':'maximum','TMIN':'minimum','EVAP':'evaporation','WDMV':'wind','AWND':'wind'}[p]
                # The engine's internal sentinel is compared after conversion.
                native=v*9/5+32 if key in ('maximum','minimum') and unit_system=='SI' else v
                if not math.isfinite(native):
                    raise ValueError('Climate conversion overflows the native internal numeric channel')
                if native == -1.e10:
                    continue
                slot[key]=v
        return tuple(ClimateDay(date=when, **slot) for when, slot in sorted(values.items()))

    def as_user(self, *, unit_system, ghcnd_units=None, station='Climate'):
        """Explicitly materialize native daily slots; quality/extra fields are lost."""
        days=self.daily(unit_system=unit_system, ghcnd_units=ghcnd_units)
        records=tuple(ClimateRecord(station=station, date=d.date, observations=tuple(
            ClimateObservation(parameter=p, value=getattr(d, field)) for p, field in
            (('TMAX','maximum'),('TMIN','minimum'),('EVAP','evaporation'),('AWND','wind')))) for d in days)
        return type(self)(format='USER_PREPARED', records=records)

    def trajectory(self, *, start, days, unit_system, ghcnd_units=None):
        """Daily file values, before adjustments/PET/temperature interpolation.

        Initial absent values use native 70 F, zero evaporation/wind. Later
        missing values hold the previous day. The start month must exist.
        """
        if type(start) is not date or type(days) is not int or days <= 0:
            raise ValueError('Trajectory requires a date and positive day count')
        if not any((r.date.year, r.date.month) == (start.year, start.month) for r in self.records):
            raise ValueError('Native climate opening requires data in the requested starting month')
        by_date={d.date:d for d in self.daily(unit_system=unit_system, ghcnd_units=ghcnd_units)}
        current=dict(maximum=70.0 if unit_system == 'US' else (70-32)*5/9,
                     minimum=70.0 if unit_system == 'US' else (70-32)*5/9, evaporation=0.0, wind=0.0)
        for offset in checkpointed(range(days)):
            when=start+timedelta(days=offset)
            daily=by_date.get(when)
            if daily:
                for key in current:
                    if (value:=getattr(daily,key)) is not None: current[key]=value
            yield ClimateDay(date=when, **current)
