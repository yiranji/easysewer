"""Analysis-option definitions and effective defaults for the tagged engine.

Primary implementation baseline: EPA SWMM v5.2.4 project.c and dynwave.c.
Input defaults are separate from adjusted effective values and explicit values.
"""

from dataclasses import dataclass, fields, replace
from datetime import date, datetime, time, timedelta
from decimal import Decimal, ROUND_HALF_EVEN, localcontext
import math
from types import MappingProxyType

from ..model.options import DayTime, MonthDay, Options
from ..model.units import UnitContext
from ..validation import Diagnostic, Severity, ValidationReport


@dataclass(frozen=True, kw_only=True)
class OptionDefinition:
    field: str
    keyword: str
    kind: str
    default: object
    choices: tuple[str, ...] = ()


def _definition(name, kind, default, *, keyword=None, choices=()):
    return OptionDefinition(field=name, keyword=keyword or name.upper(), kind=kind, default=default,
                            choices=tuple(choices))


OPTION_DEFINITIONS = (
    _definition("flow_units", "choice", "CFS", choices=("CFS", "GPM", "MGD", "CMS", "LPS", "MLD")),
    _definition("infiltration", "choice", "HORTON", choices=("HORTON", "MODIFIED_HORTON", "GREEN_AMPT", "MODIFIED_GREEN_AMPT", "CURVE_NUMBER")),
    _definition("flow_routing", "choice", "DYNWAVE", choices=("STEADY", "KINWAVE", "DYNWAVE", "NONE")),
    _definition("link_offsets", "choice", "DEPTH", choices=("DEPTH", "ELEVATION")),
    _definition("force_main_equation", "choice", "H-W", choices=("H-W", "D-W")),
    *(_definition(name, "boolean", False) for name in (
        "ignore_rainfall", "ignore_snowmelt", "ignore_groundwater", "ignore_rdii", "ignore_routing",
        "ignore_quality", "allow_ponding", "skip_steady_state")),
    _definition("sys_flow_tol", "number", 5.0),
    _definition("lat_flow_tol", "number", 5.0),
    _definition("start_date", "date", date(2004, 1, 1)),
    _definition("start_time", "clock", time()),
    _definition("end_date", "date", date(2004, 1, 1)),
    _definition("end_time", "clock", time()),
    _definition("report_start_date", "date", None),
    _definition("report_start_time", "clock", None),
    _definition("sweep_start", "month_day", MonthDay(month=1, day=1)),
    _definition("sweep_end", "month_day", MonthDay(month=12, day=31)),
    _definition("dry_days", "number", 0.0),
    _definition("report_step", "scheduled_step", timedelta(minutes=15)),
    _definition("wet_step", "scheduled_step", timedelta(minutes=5)),
    _definition("dry_step", "scheduled_step", timedelta(hours=1)),
    _definition("rule_step", "scheduled_step", timedelta()),
    _definition("routing_step", "seconds_or_clock", timedelta(seconds=20)),
    _definition("lengthening_step", "seconds_or_clock", timedelta()),
    _definition("minimum_step", "seconds", timedelta(seconds=.5)),
    _definition("variable_step", "number", .75),
    _definition("inertial_damping", "choice", "PARTIAL", choices=("NONE", "PARTIAL", "FULL")),
    _definition("normal_flow_limited", "choice", "BOTH", choices=("SLOPE", "FROUDE", "BOTH", "NONE")),
    _definition("surcharge_method", "choice", "EXTRAN", choices=("EXTRAN", "SLOT")),
    _definition("min_surface_area", "number", 0.0, keyword="MIN_SURFAREA"),
    _definition("min_slope", "number", 0.0),
    _definition("max_trials", "integer", 0),
    _definition("head_tolerance", "number", 0.0),
    _definition("threads", "integer", 1),
    _definition("slope_weighting", "boolean", True),
    _definition("compatibility", "compatibility", 4),
    _definition("temp_directory", "directory", None, keyword="TEMPDIR"),
)
OPTIONS_BY_FIELD = MappingProxyType({item.field: item for item in OPTION_DEFINITIONS})
OPTIONS_BY_KEYWORD = MappingProxyType({item.keyword: item for item in OPTION_DEFINITIONS})


@dataclass(frozen=True, kw_only=True)
class OptionAdjustment:
    field: str
    requested: object
    effective: object
    reason: str


@dataclass(frozen=True, kw_only=True)
class ResolvedOptions:
    values: Options
    start: datetime
    end: datetime
    report_start: datetime
    duration: timedelta
    defaults_used: frozenset[str]
    adjustments: tuple[OptionAdjustment, ...]
    report: ValidationReport


def clock_duration(value):
    if isinstance(value, DayTime):
        return value.as_duration()
    return timedelta(hours=value.hour, minutes=value.minute, seconds=value.second, microseconds=value.microsecond)


def decimal_clock_hours(duration):
    """Encode integral microseconds without first rounding a large float."""
    microseconds = (duration.days * 86400 + duration.seconds) * 1_000_000 + duration.microseconds
    with localcontext() as context:
        context.prec = 40
        context.rounding = ROUND_HALF_EVEN
        return format(Decimal(microseconds) / Decimal(3_600_000_000), 'f')


def native_clock_days(value):
    """Use the same arithmetic as the native parser for our clock encoding."""
    duration = clock_duration(value)
    seconds = duration.total_seconds()
    if duration.microseconds or seconds > 2_147_483_647:
        return float(decimal_clock_hours(duration)) / 24
    return seconds / 86400


def resolve_options(explicit: Options, profile) -> ResolvedOptions:
    defaults = dict(profile.option_defaults)
    missing = set(OPTIONS_BY_FIELD) - defaults.keys()
    if missing:
        raise ValueError(f"Profile {profile.key} has no defaults for {sorted(missing)}")
    values = {item.name: getattr(explicit, item.name) if getattr(explicit, item.name) is not None
              else defaults[item.name] for item in fields(explicit)}
    defaulted = frozenset(item.name for item in fields(explicit) if getattr(explicit, item.name) is None)
    adjustments, issues = [], []

    def adjusted(name, value, reason):
        if values[name] != value:
            adjustments.append(OptionAdjustment(field=name, requested=values[name], effective=value, reason=reason))
            values[name] = value

    def combined(day, clock, field):
        try:
            return datetime.combine(day, time()) + clock_duration(clock)
        except OverflowError:
            ValidationReport(diagnostics=(Diagnostic(code="options.calendar_overflow",
                message="Date and clock exceed the supported calendar range", field=field),)).raise_for_errors()

    start = combined(values["start_date"], values["start_time"], "start_time")
    end = combined(values["end_date"], values["end_time"], "end_time")
    # The engine stores dates as double-precision days and floors the difference
    # to whole seconds. Integer datetime subtraction differs at some boundaries.
    native_start = (values["start_date"].toordinal() - 693594) + native_clock_days(values["start_time"])
    native_end = (values["end_date"].toordinal() - 693594) + native_clock_days(values["end_time"])
    duration = timedelta(seconds=math.floor((native_end - native_start) * 86400))
    report_date, report_clock = values["report_start_date"], values["report_start_time"]
    # Both omitted components use the finite NO_DATE sentinel, including the
    # clock. A sufficiently large supplied component can outweigh that sentinel.
    try:
        native_report = max(native_start,
            (-693594 if report_date is None else report_date.toordinal() - 693594)
            + (-693594 if report_clock is None else native_clock_days(report_clock)))
    except OverflowError:
        ValidationReport(diagnostics=(Diagnostic(code="options.calendar_overflow",
            message="Date and clock exceed the supported calendar range", field="report_start_time"),)).raise_for_errors()
    if native_report <= native_start:
        report_start = start
    elif report_date is None or report_clock is None:
        try:
            day = math.floor(native_report)
            report_start = (datetime.combine(date.fromordinal(day + 693594), time())
                            + timedelta(seconds=(native_report - day) * 86400))
        except (OverflowError, ValueError):
            ValidationReport(diagnostics=(Diagnostic(code="options.calendar_overflow",
                message="Date and clock exceed the supported calendar range", field="report_start_time"),)).raise_for_errors()
    else:
        report_start = max(start, combined(report_date, report_clock, "report_start_time"))
    if native_end <= native_start:
        issues.append(Diagnostic(code="options.invalid_period", message="Simulation end must follow its start", field="end_date"))
    elif native_end <= native_report:
        issues.append(Diagnostic(code="options.invalid_report_start", message="Reporting must begin before simulation end", field="report_start_date"))
    else:
        if values["report_step"] > duration:
            adjusted("report_step", duration, "Reporting step is limited to the simulation duration")
        # Native checks report vs routing BEFORE adjusting routing to wet step.
        if values["report_step"] < values["routing_step"]:
            issues.append(Diagnostic(code="options.report_step_too_small", message="Report step cannot be smaller than requested routing step", field="report_step"))
    if values["dry_step"] < values["wet_step"]:
        adjusted("dry_step", values["wet_step"], "Dry step cannot be shorter than wet step")
    if values["routing_step"] > values["wet_step"]:
        adjusted("routing_step", values["wet_step"], "Routing step is limited to wet step")
    if values["flow_routing"] == "DYNWAVE":
        adjusted("minimum_step", max(timedelta(milliseconds=1), min(values["minimum_step"], values["routing_step"])),
                 "Dynamic-wave minimum step is clamped to routing step and at least one millisecond")
        units = UnitContext(flow_units=values["flow_units"])
        us = UnitContext()
        if values["min_surface_area"] == 0:
            adjusted("min_surface_area", us.convert(12.566, dimension="area", to=units), "Native default manhole surface area")
        if values["head_tolerance"] == 0:
            adjusted("head_tolerance", us.convert(.005, dimension="length", to=units), "Native default convergence tolerance")
        if values["max_trials"] == 0:
            adjusted("max_trials", 8, "Native default iteration limit")
    for item in adjustments:
        issues.append(Diagnostic(code="options.effective_adjustment", severity=Severity.INFO,
                                 field=item.field, message=item.reason))
    return ResolvedOptions(values=Options(**values), start=start, end=end, report_start=report_start,
                           duration=duration,
                           defaults_used=defaulted, adjustments=tuple(adjustments),
                           report=ValidationReport(diagnostics=tuple(issues)))


def inspect_option(context):
    """Reuse the profile resolver, keeping input defaults and adjusted values apart."""
    from ..model.fields import validate_fields
    from ..model.inspection import FieldFact, FieldSemantics
    from ..validation import ValidationError
    definition = OPTIONS_BY_FIELD[context.field]
    default = FieldFact(status='known', value=context.profile.option_default(context.field),
                        reason=f'Input default declared by {context.profile.key}')
    dimension = next(f for f in fields(Options) if f.name == context.field).metadata.get('dimension')
    try:
        if dimension:
            units = (context.container.flow_units if context.container.flow_units is not None
                     else context.profile.option_default('flow_units'))
            unit = FieldFact(status='known', value=UnitContext(flow_units=units).unit(dimension))
        elif definition.kind in ('scheduled_step', 'seconds_or_clock', 'seconds'):
            unit = FieldFact(status='known', value='s', reason='Public timedelta values; INP syntax may use clocks or decimal hours')
        elif definition.kind in ('clock', 'date', 'month_day'):
            unit = FieldFact(status='known', value={'clock': 'local model clock', 'date': 'local model date',
                                                  'month_day': 'month/day'}[definition.kind])
        else:
            unit = FieldFact(status='not_applicable', reason='Categorical, boolean or path option')
    except (ValueError, AttributeError):
        unit = FieldFact(status='invalid', reason='Invalid flow-unit context')
    diagnostics = tuple(validate_fields(context.container))
    if diagnostics:
        return FieldSemantics(unit=unit, default=default,
            effective=FieldFact(status='invalid', reason='Invalid option fields'), diagnostics=diagnostics)
    try:
        resolved = resolve_options(context.container, context.profile)
    except ValidationError as error:
        return FieldSemantics(unit=unit, default=default,
            effective=FieldFact(status='invalid', reason='Invalid option context'), diagnostics=error.report.diagnostics)
    reasons = [a.reason for a in resolved.adjustments if a.field == context.field]
    value = getattr(resolved.values, context.field)
    if context.field in ('report_start_date', 'report_start_time'):
        value = resolved.report_start.date() if context.field.endswith('date') else resolved.report_start.time()
        reasons.append('Resolved reporting calendar, including start-time fallback and clamping')
    effective = (FieldFact(status='invalid', reason='Option context fails run validation') if resolved.report.errors
                 else FieldFact(status='known', value=value, reason='; '.join(reasons)))
    return FieldSemantics(unit=unit, default=default, effective=effective,
                          diagnostics=resolved.report.diagnostics)
