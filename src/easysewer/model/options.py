"""Typed explicit analysis options. None always means absent from the input."""

from dataclasses import dataclass, fields, replace
from datetime import date, time, timedelta
from typing import Literal

from ..validation import Diagnostic, DiagnosticSubject
from .fields import number, validate_fields
from .store import CollectionSpec
from .values import FileReference


@dataclass(frozen=True, kw_only=True)
class DayTime:
    """Local clock time beyond midnight, such as day_offset=1 for 24:00."""
    clock: time = time()
    day_offset: int = 0

    def __post_init__(self):
        if not isinstance(self.clock, time) or self.clock.tzinfo is not None:
            raise ValueError("SWMM uses local model time without a timezone")
        if type(self.day_offset) is not int or self.day_offset < 0:
            raise ValueError("day_offset must be a nonnegative integer")

    def as_duration(self):
        return timedelta(days=self.day_offset, hours=self.clock.hour, minutes=self.clock.minute,
                         seconds=self.clock.second, microseconds=self.clock.microsecond)


@dataclass(frozen=True, kw_only=True)
class MonthDay:
    month: int
    day: int

    def __post_init__(self):
        date(1947, self.month, self.day)  # Native sweeping calendar is non-leap.


FlowUnits = Literal["CFS", "GPM", "MGD", "CMS", "LPS", "MLD"]
OffsetMode = Literal["DEPTH", "ELEVATION"]
ForceMainEquation = Literal["H-W", "D-W"]


@dataclass(frozen=True, kw_only=True)
class Options:
    flow_units: FlowUnits | None = None
    infiltration: Literal["HORTON", "MODIFIED_HORTON", "GREEN_AMPT", "MODIFIED_GREEN_AMPT", "CURVE_NUMBER"] | None = None
    flow_routing: Literal["STEADY", "KINWAVE", "DYNWAVE"] | None = None
    link_offsets: OffsetMode | None = None
    force_main_equation: ForceMainEquation | None = None
    ignore_rainfall: bool | None = None
    ignore_snowmelt: bool | None = None
    ignore_groundwater: bool | None = None
    ignore_rdii: bool | None = None
    ignore_routing: bool | None = None
    ignore_quality: bool | None = None
    allow_ponding: bool | None = None
    skip_steady_state: bool | None = None
    sys_flow_tol: float | None = number("percent", None)
    lat_flow_tol: float | None = number("percent", None)
    start_date: date | None = None
    start_time: time | DayTime | None = None
    end_date: date | None = None
    end_time: time | DayTime | None = None
    report_start_date: date | None = None
    report_start_time: time | DayTime | None = None
    sweep_start: MonthDay | None = None
    sweep_end: MonthDay | None = None
    dry_days: float | None = number("days", None, minimum=0)
    report_step: timedelta | None = None
    wet_step: timedelta | None = None
    dry_step: timedelta | None = None
    routing_step: timedelta | None = None
    rule_step: timedelta | None = None
    lengthening_step: timedelta | None = None
    variable_step: float | None = number("ratio", None, minimum=0, maximum=2)
    minimum_step: timedelta | None = None
    inertial_damping: Literal["NONE", "PARTIAL", "FULL"] | None = None
    normal_flow_limited: Literal["SLOPE", "FROUDE", "BOTH", "NONE"] | None = None
    surcharge_method: Literal["EXTRAN", "SLOT"] | None = None
    min_surface_area: float | None = number("area", None, minimum=0)
    min_slope: float | None = number("percent", None, minimum=0)
    max_trials: int | None = number("count", None, minimum=0, maximum=2_147_483_647, integer=True)
    head_tolerance: float | None = number("length", None)
    threads: int | None = number("count", None, minimum=0, maximum=2_147_483_647, integer=True)
    slope_weighting: bool | None = None
    compatibility: Literal[3, 4, 5] | None = None
    temp_directory: FileReference | None = None

    def validate_local(self):
        positive = {"report_step", "wet_step", "dry_step", "routing_step"}
        integer_seconds = {"report_step", "wet_step", "dry_step", "rule_step"}
        for item in fields(self):
            value = getattr(self, item.name)
            error = None
            if isinstance(value, timedelta):
                if value < timedelta(0) or (item.name in positive and value == timedelta(0)):
                    error = "Time step must be positive" if item.name in positive else "Time step cannot be negative"
                elif item.name in integer_seconds and value.microseconds:
                    error = "This SWMM option requires a whole number of seconds"
                elif item.name in integer_seconds and value.total_seconds() > 2_147_483_647:
                    error = "Time step exceeds the native signed integer range"
            elif isinstance(value, time) and value.tzinfo is not None:
                error = "SWMM uses local model time without a timezone"
            if error:
                yield Diagnostic(code="options.invalid_value", message=error, field=item.name)
        if self.min_slope is not None and self.min_slope >= 100:
            yield Diagnostic(code="options.invalid_value", message="Minimum slope must be less than 100 percent", field="min_slope")
        if self.temp_directory is not None and self.temp_directory.direction != "output":
            yield Diagnostic(code="options.invalid_value", message="Temporary directory must have output direction", field="temp_directory")


def get_options(store) -> Options:
    try:
        return store.collection("swmm:options")["settings"]
    except KeyError:
        return Options()


def _context_change(before, after, store):
    before, after = before or Options(), after or Options()
    has_data = any(len(store.collection(spec.key)) for spec in store.specifications if spec.key != "swmm:options")
    for field, permission in (("flow_units", "units"), ("link_offsets", "offsets"),
                              ("force_main_equation", "force_main_equation")):
        # The collection is profile-independent: even setting/removing an
        # explicit default is routed through an intentional context operation.
        if getattr(before, field) != getattr(after, field):
            if (has_data or store.opaque_constraints) and not store._context_change_allowed(permission):
                yield Diagnostic(code="options.context_change_requires_intent", field=field,
                                 subject=DiagnosticSubject(path=(field,)),
                                 message=f"Changing {field} requires an explicit convert or reinterpret operation")


def option_diagnostics(issues):
    """Bind profile diagnostics at the model boundary, not in snapshot data."""
    calendar = ('start_date', 'start_time', 'end_date', 'end_time')
    related = {
        'options.invalid_period': calendar,
        'options.invalid_report_start': (*calendar, 'report_start_date', 'report_start_time'),
        'options.report_step_too_small': ('routing_step', *calendar),
    }
    adjustments = {
        'report_step': calendar,
        'dry_step': ('wet_step',),
        'routing_step': ('wet_step',),
        'minimum_step': ('flow_routing', 'routing_step', 'wet_step'),
        'min_surface_area': ('flow_routing', 'flow_units'),
        'head_tolerance': ('flow_routing', 'flow_units'),
        'max_trials': ('flow_routing',),
    }
    names = {item.name for item in fields(Options)}
    def subject(name):
        return DiagnosticSubject(collection='swmm:options', key='settings', path=(name,))
    for issue in issues:
        if issue.code not in (*related, 'options.calendar_overflow', 'options.effective_adjustment') or issue.field not in names:
            yield issue
            continue
        dependencies = related.get(issue.code, ())
        if issue.code == 'options.calendar_overflow':
            dependencies = {'start_time': ('start_date',), 'end_time': ('end_date',),
                            'report_start_time': ('report_start_date',)}.get(issue.field, ())
        elif issue.code == 'options.effective_adjustment':
            dependencies = adjustments.get(issue.field, ())
        yield replace(issue, subject=subject(issue.field),
            related=tuple(subject(name) for name in dependencies if name != issue.field))


OPTIONS_COLLECTION = CollectionSpec(key="swmm:options", record_type=Options,
                                    key_of=lambda _: "settings", validate=validate_fields,
                                    validate_change=_context_change)
