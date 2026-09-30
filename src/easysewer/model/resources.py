"""Shared model resources; their consumers own purpose and interpolation rules."""

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
import math
from typing import Literal

from ..validation import Diagnostic, Severity, ValidationReport
from ..validation._cooperative import checkpointed
from .fields import number, validate_fields
from .identity import Ref, validate_identifier
from .store import CollectionSpec
from .values import FileReference
from .units import UnitTransform

CURVE_KINDS = ("STORAGE", "SHAPE", "DIVERSION", "TIDAL", "PUMP1", "PUMP2",
               "PUMP3", "PUMP4", "PUMP5", "RATING", "CONTROL", "WEIR")
PATTERN_LENGTHS = {"MONTHLY": 12, "DAILY": 7, "HOURLY": 24, "WEEKEND": 24}


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


@dataclass(frozen=True, kw_only=True)
class Resource:
    id: str

    def __post_init__(self):
        validate_identifier(self.id)


@dataclass(frozen=True, kw_only=True)
class CurvePoint:
    x: float = number("curve_axis")
    y: float = number("curve_axis")


@dataclass(frozen=True, kw_only=True)
class Curve(Resource):
    kind: Literal["STORAGE", "SHAPE", "DIVERSION", "TIDAL", "PUMP1", "PUMP2",
                  "PUMP3", "PUMP4", "PUMP5", "RATING", "CONTROL", "WEIR"]
    points: tuple[CurvePoint, ...] = ()

    def validate_local(self):
        if not self.points:
            yield Diagnostic(code="resource.empty_curve", severity=Severity.WARNING,
                             message="Curve has no points; consumers may require a nonempty curve", field="points")
        for index, (before, after) in enumerate(zip(self.points, self.points[1:]), 1):
            if _finite(before.x) and _finite(after.x) and after.x <= before.x:
                yield Diagnostic(code="resource.curve_order", message="Curve x values must increase strictly",
                                 field=f"points[{index}].x")


@dataclass(frozen=True, kw_only=True)
class SeriesPoint:
    time: datetime | timedelta
    value: float = number("series_value")

    def validate_local(self):
        if isinstance(self.time, datetime) and self.time.tzinfo is not None:
            yield Diagnostic(code="resource.timezone", message="Series timestamps must use local model time", field="time")


@dataclass(frozen=True, kw_only=True)
class TimeSeries(Resource):
    """Base class for explicitly distinct inline and external-file variants."""


@dataclass(frozen=True, kw_only=True)
class InlineTimeSeries(TimeSeries):
    points: tuple[SeriesPoint, ...]

    def validate_local(self):
        yield from _series_time_diagnostics(point.time for point in self.points)


def _series_time_diagnostics(times):
    """Shared sequence rules for editable points and external-file inspection."""
    calendar_seen = False
    count = 0
    before = None
    for index, when in enumerate(checkpointed(times)):
        count += 1
        if isinstance(when, datetime):
            calendar_seen = True
        elif calendar_seen and isinstance(when, timedelta):
            yield Diagnostic(code="resource.relative_after_calendar",
                message="Relative points must precede calendar points; use explicit calendar timestamps after switching", field=f"points[{index}].time")
        if index:
            if (type(when) is type(before) and isinstance(before, (timedelta, datetime))
                    and not (isinstance(before, datetime) and (before.tzinfo or when.tzinfo))
                    and when <= before):
                yield Diagnostic(code="resource.series_order", message="Series times must increase strictly",
                                 field=f"points[{index}].time")
        before = when
    if not count:
        yield Diagnostic(code="resource.empty_series", message="An inline series requires at least one point", field="points")


@dataclass(frozen=True, kw_only=True)
class FileTimeSeries(TimeSeries):
    file: FileReference

    def validate_local(self):
        if self.file.direction != "input":
            yield Diagnostic(code="resource.file_direction", message="A series file must have input direction", field="file")


@dataclass(frozen=True, kw_only=True)
class Pattern(Resource):
    kind: Literal["MONTHLY", "DAILY", "HOURLY", "WEEKEND"]
    factors: tuple[float, ...] = ()

    @property
    def effective_factors(self):
        count = PATTERN_LENGTHS[self.kind]
        return (self.factors + (1.0,) * count)[:count]

    def validate_local(self):
        if len(self.factors) > 24:
            yield Diagnostic(code="resource.pattern_capacity", message="The native pattern stores at most 24 factors", field="factors")
        for index, value in enumerate(self.factors):
            if not _finite(value):
                yield Diagnostic(code="resource.invalid_factor", message="Pattern factors must be finite numbers", field=f"factors[{index}]")
        if len(self.factors) > PATTERN_LENGTHS[self.kind]:
            yield Diagnostic(code="resource.unused_factors", severity=Severity.WARNING,
                             message="Factors beyond the pattern period are retained but not used by SWMM", field="factors")


def resource_collections():
    return tuple(CollectionSpec(key=f"swmm:{name}", record_type=kind, key_of=lambda row: row.id,
                                identity_field="id", validate=validate_fields)
                 for name, kind in (("curves", Curve), ("timeseries", TimeSeries), ("patterns", Pattern)))


CURVE_DIMENSIONS = {
    "STORAGE": ("depth", "area"), "SHAPE": ("ratio", "ratio"),
    "DIVERSION": ("flow", "flow"), "TIDAL": ("hours", "elevation"),
    "PUMP1": ("volume", "flow"), "PUMP2": ("depth", "flow"),
    "PUMP3": ("depth", "flow"), "PUMP4": ("depth", "flow"),
    "PUMP5": ("depth", "flow"), "RATING": ("depth", "flow"),
    "WEIR": ("depth", "weir_coefficient"),
}


def _conversion_error(resource, code, message):
    ValidationReport(diagnostics=(Diagnostic(code=code, message=message, object_id=resource.id),)).raise_for_errors()


def _consumer_dimensions(resource, namespace, context):
    target = Ref(collection=namespace, key=resource.id)
    uses = context.resource_uses(target)
    declared = {(use.owner.canonical, use.path) for use in uses}
    references = {(use.owner.canonical, use.path) for use in context.referenced_by(target)}
    if not uses or references != declared or any(not use.dimensions for use in uses):
        _conversion_error(resource, "units.unknown_resource_dimensions",
                          "Resource conversion requires dimensional declarations for every consumer")
    dimensions = {use.dimensions for use in uses}
    if len(dimensions) != 1:
        _conversion_error(resource, "units.conflicting_dimensions", "Shared resource consumers disagree on units")
    return next(iter(dimensions))


def _convert_curve(curve, context):
    dimensions = CURVE_DIMENSIONS.get(curve.kind)
    if dimensions is None:
        dimensions = _consumer_dimensions(curve, "swmm:curves", context)
    if len(dimensions) != 2:
        _conversion_error(curve, "units.invalid_curve_dimensions", "A curve requires units for two axes")
    return replace(curve, points=tuple(CurvePoint(x=context.number(point.x, dimensions[0]),
                                                y=context.number(point.y, dimensions[1])) for point in curve.points))


def _convert_series(series, context):
    dimensions = _consumer_dimensions(series, "swmm:timeseries", context)
    if len(dimensions) != 1:
        _conversion_error(series, "units.invalid_series_dimensions", "A series requires one value dimension")
    dimension, = dimensions
    if isinstance(series, FileTimeSeries):
        if context.number(0, dimension) != 0 or context.number(1, dimension) != 1:
            _conversion_error(series, "units.external_data_requires_conversion",
                              "External series data requires explicit conversion; changing its path cannot convert its values")
        return series
    return replace(series, points=tuple(replace(point, value=context.number(point.value, dimension)) for point in series.points))


RESOURCE_UNIT_TRANSFORMS = (
    UnitTransform(value_type=Curve, convert=_convert_curve),
    UnitTransform(value_type=InlineTimeSeries, convert=_convert_series),
    UnitTransform(value_type=FileTimeSeries, convert=_convert_series),
    UnitTransform(value_type=Pattern, convert=lambda value, context: value),
)
