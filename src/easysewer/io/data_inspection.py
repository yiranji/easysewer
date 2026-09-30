"""External data inspectors using the same declarations as file preflight."""

from datetime import timedelta
import math

from ..validation import Diagnostic, Severity, ValidationReport
from ..validation._cooperative import checkpointed
from ._data_text import decode_lines, tokens
from .interface_inspection import InterfaceInspection
from .rainfall import RainfallData
from .timeseries import TimeSeriesData, _scan_series
from .climate_data import ClimateData
from .historical_rainfall import HistoricalRainfallData


def unsupported(format):
    return InterfaceInspection(format=format,status='unsupported',report=ValidationReport(diagnostics=(Diagnostic(
        code='files.format_not_implemented',severity=Severity.WARNING,message=f'Data-format inspection remains pending for {format}'),)))


def _series(data, use, model, encoding, source):
    scan=_scan_series(data,encoding=encoding,source=source)
    resolved=None
    if scan is not None:
        try:
            resolved=scan.resolved(model.effective_options.start)
        except (ValueError,OverflowError):
            pass  # Preserve the original full-document error and its precedence.
    if resolved is None:
        return _series_document(data,use,model,encoding,source)
    first,last,minimum,basis=resolved
    facts=(('points',scan.count),('first',first.isoformat()),('last',last.isoformat()),('time_basis',basis))
    return facts,_series_consumers(use,model,list(scan.issues),scan.negative,minimum)


def _series_document(data, use, model, encoding, source):
    """Detailed validation fallback retains all original diagnostic coordinates."""
    document=TimeSeriesData.from_bytes(data,encoding=encoding,source=source)
    points=document.resolved(model.effective_options.start).points
    issues=list(document.report.diagnostics)
    gaps=[(b.time-a.time).total_seconds() for a,b in checkpointed(zip(points,points[1:]))]
    facts=(('points',len(points)),('first',points[0].time.isoformat()),('last',points[-1].time.isoformat()),
           ('time_basis','relative' if all(isinstance(p.time,timedelta) for p in document.points) else 'mixed' if isinstance(document.points[0].time,timedelta) else 'calendar'))
    return facts,_series_consumers(use,model,issues,any(p.value<0 for p in points),min(gaps) if gaps else None)


def _series_consumers(use, model, issues, negative, minimum_gap):
    collections={spec.key for spec in model._store.specifications}
    used_gages={catchment.rain_gage.canonical for catchment in model.subcatchments.values()} if 'swmm:subcatchments' in collections else set()
    for consumer in checkpointed(model.resource_uses(use.owner)):
        if consumer.role != 'rainfall':
            continue
        if consumer.owner.canonical not in used_gages and not model._store.opaque_constraints:
            continue
        gage=model.collection(consumer.owner.collection)[consumer.owner.key]
        if negative:
            issues.append(Diagnostic(code='rainfall.negative_series',message='A rainfall time series contains negative values',object_id=gage.id))
        minimum=math.floor(minimum_gap+.5) if minimum_gap is not None else 0
        if minimum>0 and gage.interval.total_seconds()>minimum:
            issues.append(Diagnostic(code='rainfall.interval_exceeds_series',message='Gage interval exceeds the shortest external-series interval',object_id=gage.id))
    return issues


def _rainfall(data,use,model,encoding,source):
    document=RainfallData.from_bytes(data,encoding=encoding,source=source)
    gage=model.collection(use.owner.collection)[use.owner.key]
    points=document.station_series(gage.source.station,start_date=gage.source.start_date).points
    if any(p.value<0 for p in points):
        raise ValueError('User-prepared rainfall values must be nonnegative for physical simulation')
    factor=gage.interval.total_seconds()/3600 if gage.form=='INTENSITY' else 1
    if any(p.value>3.4028234663852886e38 or p.value*factor>3.4028234663852886e38 for p in points):
        raise ValueError('User rainfall overflows the native single-precision data path')
    facts=(('format','USER'),('station',gage.source.station),('readings',len(points)),
           ('first',points[0].time.isoformat()),('last',points[-1].time.isoformat()),('rain_form',gage.form),('rain_units',gage.source.units))
    return facts,list(document.report.diagnostics)


def _historical_rainfall(data,use,model,encoding,source):
    document=HistoricalRainfallData.from_bytes(data,encoding=encoding,source=source)
    gage=model.collection(use.owner.collection)[use.owner.key]
    interpreted=document.interpret(start_date=gage.source.start_date)
    periods=tuple(p for p in interpreted.periods if p.saved)
    if not periods:raise ValueError('Historical rainfall has no native cache records after date/quality selection')
    issues=list(interpreted.report.diagnostics)
    if any(p.depth_inches is None for p in interpreted.periods):
        issues.append(Diagnostic(code='rainfall.archive_missing_values',severity=Severity.WARNING,
            message='Missing/deleted archive readings are omitted by the native cache; they are not measured zeros',object_id=gage.id))
    facts=(('format',document.format),('records',len(document.records)),('cache_periods',len(periods)),
        ('interval_seconds',document.interval.total_seconds()),('native_units','IN'),('native_form','VOLUME'),
        ('first',periods[0].time.isoformat()),('last',periods[-1].time.isoformat()),('station_selection','all source stations'))
    return facts,issues


def _climate(data,use,model,encoding,source):
    from ..model.climate import FileEvaporation, FileTemperature, FileWind, TemperatureEvaporation
    document=ClimateData.from_bytes(data,encoding=encoding,source=source)
    settings=model.effective_climate
    configuration=settings.file
    start=configuration.start_date
    days=(model.effective_options.end.date()-model.effective_options.start.date()).days+1
    # Only the initial day needs filling to prove the required starting month.
    initial=next(document.trajectory(start=start,days=1,unit_system=model.units.system,ghcnd_units=configuration.units))
    daily=document.daily(unit_system=model.units.system,ghcnd_units=configuration.units)
    finish=start+timedelta(days=days-1)
    selected=[row for row in daily if start<=row.date<=finish]
    issues=list(document.report.diagnostics)
    def issue(code,message,severity=Severity.ERROR):
        issues.append(Diagnostic(code=code,message=message,severity=severity,field=source))
    evap=settings.evaporation.source
    for row in checkpointed((initial,*selected)):
        if isinstance(evap,FileEvaporation) and row.evaporation is not None and row.evaporation<0:
            issue('climate.negative_file_evaporation','Climate file contains negative evaporation consumed by the model')
            break
    if isinstance(settings.wind,FileWind) and any(row.wind is not None and row.wind<0 for row in (initial,*selected)):
        issue('climate.negative_file_wind','Climate file contains negative wind consumed by the model')
    if isinstance(settings.temperature,FileTemperature) or isinstance(evap,TemperatureEvaporation):
        high,low=initial.maximum,initial.minimum
        for row in checkpointed(selected):
            if row.maximum is not None: high=row.maximum
            if row.minimum is not None: low=row.minimum
            if high<low:
                issue('climate.inverted_temperature_range','Daily maximum temperature is below the minimum after missing-value carry-forward')
                break
    needed=[]
    if isinstance(settings.temperature,FileTemperature) or isinstance(evap,TemperatureEvaporation): needed.extend(('maximum','minimum'))
    if isinstance(evap,FileEvaporation): needed.append('evaporation')
    if isinstance(settings.wind,FileWind): needed.append('wind')
    first=next((r for r in selected if r.date==start),None)
    missing=tuple(name for name in needed if first is None or getattr(first,name) is None)
    if missing:
        issue('climate.initial_defaults','Starting day lacks '+', '.join(missing)+'; native uses initial defaults, not earlier file days',Severity.WARNING)
    if len(selected)<days or any(getattr(row,name) is None for row in selected for name in needed):
        issue('climate.missing_daily_values','Missing daily values retain preceding simulation-day values (initially defaults)',Severity.WARNING)
    if document.format=='USER_PREPARED' and model.units.system=='SI' and isinstance(settings.wind,FileWind):
        issue('climate.user_wind_mph','SWMM 5.2.4 consumes USER climate wind in mph even in SI projects; this differs from the manual',Severity.WARNING)
    facts=(('format',document.format),('records',len(document.records)),('days',len(daily)),
           ('file_start',start.isoformat()),('file_finish',finish.isoformat()),('unit_system',model.units.system),('wind_units','mph'))
    return facts,issues


def inspect_data(data, *, use, model, encoding='utf-8', source=None):
    handlers={'swmm:timeseries.data':_series,'swmm:rainfall.data':_rainfall,'swmm:climate.data':_climate}
    if use.format not in handlers:
        return unsupported(use.format)
    try:
        if use.format=='swmm:rainfall.data':
            from ._historical_rain_text import detect
            rows=list(decode_lines(data,encoding=encoding,source=source))
            kind,_,_=detect([(n,row+'\n' if n<len(rows) else row) for n,row in rows])
            if kind is not None:
                facts,issues=_historical_rainfall(data,use,model,encoding,source)
                report=ValidationReport(diagnostics=tuple(issues))
                return InterfaceInspection(format=use.format,status='validated' if report.is_valid else 'invalid',facts=facts,report=report)
            # Unrecognized future formats remain visibly unsupported.
            first=next((parts for _,row in decode_lines(data,encoding=encoding,source=source)
                        if (parts:=tokens(row)) and not parts[0].startswith(';')),())
            if not first:
                raise ValueError('Rainfall file contains no data rows')
            if len(first)<6 or not all(v.isascii() and v.isdecimal() for v in first[1:6]):
                return unsupported(use.format)
        facts,issues=handlers[use.format](data,use,model,encoding,source)
        report=ValidationReport(diagnostics=tuple(issues))
        status='validated' if report.is_valid else 'invalid'
        # Representation does not inherit old C buffer/EOF defects. Execution
        # needs a backend that actually implements the checked file reader.
        required={'swmm:climate.data': ('easysewer:climate-io:1',),
                  'swmm:timeseries.data': ('easysewer:timeseries-io:1',)}.get(use.format, ())
        return InterfaceInspection(format=use.format,status=status,facts=facts,report=report,
                                   required_capabilities=required)
    except (ValueError,OverflowError) as exc:
        report=getattr(exc,'report',ValidationReport(diagnostics=(Diagnostic(code='files.invalid_format',message=str(exc),field=source),)))
        return InterfaceInspection(format=use.format,status='invalid',report=report)
