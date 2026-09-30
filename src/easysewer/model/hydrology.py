"""Rainfall, catchment and snow parameter objects, independent of input syntax."""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
import math
from typing import Literal

from ..validation import Diagnostic, DiagnosticSubject, Severity, ValidationReport
from .fields import number, reference, validate_fields
from .identity import Ref, canonical_key
from .network import Entity
from .options import DayTime, get_options
from .store import CollectionSpec
from .resources import InlineTimeSeries
from .usage import ResourceUse
from .values import FileReference, Point

InfiltrationMethod = Literal["HORTON", "MODIFIED_HORTON", "GREEN_AMPT", "MODIFIED_GREEN_AMPT", "CURVE_NUMBER"]


@dataclass(frozen=True, kw_only=True)
class SeriesRainfall:
    series: Ref = reference("swmm:timeseries")


@dataclass(frozen=True, kw_only=True)
class FileRainfall:
    file: FileReference
    station: str
    units: Literal["IN", "MM"]
    start_date: date | None = None

    def validate_local(self):
        if self.file.direction != "input":
            yield Diagnostic(code="rainfall.file_direction", message="Rainfall files must have input direction", field="file")
        if not self.station or any(char in self.station for char in "\r\n\x00"):
            yield Diagnostic(code="rainfall.station", message="Station must be a nonempty single-line name", field="station")


@dataclass(frozen=True, kw_only=True)
class RainGage(Entity):
    form: Literal["INTENSITY", "VOLUME", "CUMULATIVE"]
    interval: timedelta
    snow_factor: float = number("ratio")
    source: SeriesRainfall | FileRainfall
    position: Point | None = None

    def validate_local(self):
        if self.interval.microseconds or not 1 <= self.interval.total_seconds() <= 2_147_483_647:
            yield Diagnostic(code="rainfall.interval", message="Gage interval must be a positive native whole-second duration", field="interval")


@dataclass(frozen=True, kw_only=True)
class Horton:
    maximum_rate: float = number("rain_intensity", minimum=0)
    minimum_rate: float = number("rain_intensity", minimum=0)
    decay: float = number("inverse_hours", minimum=0)
    drying_time: float = number("days", minimum=0)
    maximum_volume: float | None = number("rain_depth", None, minimum=0)

    def validate_local(self):
        if self.maximum_rate < self.minimum_rate:
            yield Diagnostic(code="infiltration.horton_rates", message="Maximum Horton rate cannot be less than minimum", field="maximum_rate")


@dataclass(frozen=True, kw_only=True)
class GreenAmpt:
    suction: float = number("rain_depth", minimum=0)
    conductivity: float = number("conductivity", positive=True)
    initial_deficit: float = number("ratio", minimum=0, maximum=1)


@dataclass(frozen=True, kw_only=True)
class CurveNumber:
    curve_number: float = number("ratio")
    drying_time: float = number("days", positive=True)


@dataclass(frozen=True, kw_only=True)
class Infiltration:
    parameters: Horton | GreenAmpt | CurveNumber
    # None inherits the project's method; it is not inferred from parameters.
    method: InfiltrationMethod | None = None


@dataclass(frozen=True, kw_only=True)
class Subareas:
    impervious_roughness: float = number("manning", minimum=0)
    pervious_roughness: float = number("manning", minimum=0)
    impervious_storage: float = number("rain_depth", minimum=0)
    pervious_storage: float = number("rain_depth", minimum=0)
    zero_storage_percent: float = number("percent", minimum=0)
    route_to: Literal["OUTLET", "IMPERVIOUS", "PERVIOUS"] = "OUTLET"
    routed_percent: float | None = number("percent", None, minimum=0, maximum=100)


@dataclass(frozen=True, kw_only=True)
class Subcatchment(Entity):
    rain_gage: Ref = reference("swmm:raingages")
    outlet: Ref
    area: float = number("catchment_area", minimum=0)
    impervious_percent: float = number("percent", minimum=0)
    width: float = number("length", minimum=0)
    slope: float = number("percent", minimum=0)
    curb_length: float = number("user_length", minimum=0)
    snowpack: Ref | None = reference("swmm:snowpacks", None)
    subareas: Subareas | None = None
    infiltration: Infiltration | None = None
    polygon: tuple[Point, ...] = ()

    def validate_local(self):
        if self.outlet.collection not in ("swmm:nodes", "swmm:subcatchments"):
            yield Diagnostic(code="subcatchment.outlet_namespace", message="Outlet must reference a node or subcatchment", field="outlet")


@dataclass(frozen=True, kw_only=True)
class SnowSurface:
    minimum_melt: float = number("snow_melt_coefficient")
    maximum_melt: float = number("snow_melt_coefficient")
    base_temperature: float = number("temperature")
    free_water_fraction: float = number("ratio", minimum=0, maximum=1)
    initial_snow: float = number("rain_depth")
    initial_free_water: float = number("rain_depth")


@dataclass(frozen=True, kw_only=True)
class PlowableSnow(SnowSurface):
    fraction: float = number("ratio", minimum=0, maximum=1)


@dataclass(frozen=True, kw_only=True)
class DepletableSnow(SnowSurface):
    full_cover_depth: float = number("rain_depth")


@dataclass(frozen=True, kw_only=True)
class SnowRemoval:
    threshold: float = number("rain_depth")
    out_of_system: float = number("ratio")
    to_impervious: float = number("ratio")
    to_pervious: float = number("ratio")
    immediate_melt: float = number("ratio")
    to_subcatchment: float | None = number("ratio", None)
    destination: Ref | None = reference("swmm:subcatchments", None)


@dataclass(frozen=True, kw_only=True)
class Snowpack(Entity):
    plowable: PlowableSnow | None = None
    impervious: DepletableSnow | None = None
    pervious: DepletableSnow | None = None
    removal: SnowRemoval | None = None

    def validate_local(self):
        if all(getattr(self, field) is None for field in ("plowable", "impervious", "pervious", "removal")):
            yield Diagnostic(code="snowpack.empty", message="A named snowpack needs at least one parameter row")


HYDROLOGY_COLLECTIONS = tuple(CollectionSpec(key=f"swmm:{name}", record_type=kind, key_of=lambda row: row.id,
    identity_field="id", validate=validate_fields) for name, kind in (
    ("raingages", RainGage), ("subcatchments", Subcatchment), ("snowpacks", Snowpack)))


def effective_infiltration_method(infiltration, options, profile):
    return infiltration.method or options.infiltration or profile.option_default("infiltration")


def hydrology_resource_uses(store):
    for row in store.collection("swmm:raingages").values():
        if ValidationReport(diagnostics=tuple(validate_fields(row))).is_valid and isinstance(row.source, SeriesRainfall):
            yield ResourceUse(owner=Ref(collection="swmm:raingages", key=row.id), target=row.source.series,
                path=("source", "series"), role="rainfall", dimensions=("rain_intensity" if row.form == "INTENSITY" else "rain_depth",))


def validate_hydrology(store, profile, *, for_run=False):
    def valid(row):
        return ValidationReport(diagnostics=tuple(validate_fields(row))).is_valid

    def subject(row, path=()):
        collection = next(name for kind, name in ((Subcatchment, "swmm:subcatchments"),
            (RainGage, "swmm:raingages"), (Snowpack, "swmm:snowpacks")) if isinstance(row, kind))
        return DiagnosticSubject(collection=collection, key=row.id, path=path)

    def reference_subject(ref, path=()):
        return DiagnosticSubject(collection=ref.collection, key=ref.key, path=path)

    def issue(row, code, message, field=None, severity=Severity.ERROR, *, path=None, related=()):
        return Diagnostic(code=code, message=message, object_id=row.id, field=field, severity=severity,
            subject=subject(row, path if path is not None else tuple(field.split('.')) if field else ()),
            related=related)

    def lookup(ref):
        if ref is not None and store.contains(ref):
            row = store.collection(ref.collection)[ref.key]
            return row if valid(row) else None
        return None

    options = get_options(store)
    if not valid(options):
        return
    catchments = [row for row in store.collection("swmm:subcatchments").values() if valid(row)]
    for row in catchments:
        other = "swmm:nodes" if row.outlet.collection == "swmm:subcatchments" else "swmm:subcatchments"
        if store.contains(Ref(collection=other, key=row.outlet.key)):
            yield issue(row, "subcatchment.ambiguous_outlet", "Native input cannot distinguish a node and subcatchment with the same outlet ID", "outlet",
                path=("outlet", "key"), related=(reference_subject(row.outlet), DiagnosticSubject(collection=other, key=row.outlet.key)))
        infiltration = row.infiltration
        if infiltration is not None:
            method = effective_infiltration_method(infiltration, options, profile)
            expected = Horton if method in ("HORTON", "MODIFIED_HORTON") else GreenAmpt if method in ("GREEN_AMPT", "MODIFIED_GREEN_AMPT") else CurveNumber
            if not isinstance(infiltration.parameters, expected):
                yield issue(row, "infiltration.method_parameters", "Parameters do not match the explicit or inherited infiltration method", "infiltration",
                    path=("infiltration", "parameters"), related=(subject(row, ("infiltration", "method")),) +
                    ((DiagnosticSubject(collection="swmm:options", key="settings", path=("infiltration",)),)
                     if infiltration.method is None else ()))
            if not for_run and isinstance(infiltration.parameters, CurveNumber) and not 10 <= infiltration.parameters.curve_number <= 99:
                yield issue(row, "infiltration.curve_number_clamp", "Native clamps curve number to [10, 99]", "infiltration.parameters.curve_number", Severity.WARNING)
        if not for_run:
            if row.impervious_percent > 100:
                yield issue(row, "subcatchment.impervious_clamp", "Native caps impervious percentage at 100", "impervious_percent", Severity.WARNING)
            continue
        if row.area > 0 and row.subareas is None:
            yield issue(row, "subcatchment.missing_subareas", "A nonzero catchment requires explicit subarea parameters", "subareas",
                related=(subject(row, ("area",)),))
        if row.area > 0 and row.impervious_percent < 100 and row.infiltration is None:
            yield issue(row, "subcatchment.missing_infiltration", "Pervious area requires infiltration parameters", "infiltration",
                related=(subject(row, ("area",)), subject(row, ("impervious_percent",))))
        if row.subareas and row.subareas.zero_storage_percent > 100:
            yield issue(row, "subcatchment.zero_storage_percent", "Percentage above 100 produces a negative subarea", "subareas.zero_storage_percent")

    gages = [row for row in store.collection("swmm:raingages").values() if valid(row)]
    used_gages = {row.rain_gage.canonical for row in catchments}
    file_stations = {}
    if for_run and not options.ignore_rainfall:
        for row in gages:
            if not isinstance(row.source, FileRainfall):
                continue
            source = row.source
            try:
                path = str(source.file.resolve())
            except ValueError:
                path = str(source.file.path_type(source.file.path))
            # Match the fixed reader's case-insensitive file-conflict check.
            previous = file_stations.setdefault(canonical_key(source.station), (canonical_key(path), row))
            if previous[0] != canonical_key(path):
                yield issue(row, "rainfall.station_file_conflict", "Native rainfall interface cannot use the same station ID from different files", "source",
                    related=(subject(previous[1], ("source",)),))
            elif previous[1] is not row:
                first = previous[1]
                if (row.form, row.interval, source.units, source.start_date) != (first.form, first.interval, first.source.units, first.source.start_date):
                    yield issue(row, "rainfall.shared_file_settings", "Gages using the same file station must agree on format, interval, units and start date; native selects the first station header", "source",
                        related=(subject(row, ("form",)), subject(row, ("interval",)), subject(first, ("source",)),
                                 subject(first, ("form",)), subject(first, ("interval",))))
    shared = {}
    for row in gages:
        if not for_run or (Ref(collection="swmm:raingages", key=row.id).canonical not in used_gages and not store.opaque_constraints):
            continue
        if row.snow_factor < 0:
            yield issue(row, "rainfall.negative_snow_factor", "Snow correction factor must be nonnegative", "snow_factor")
        if not isinstance(row.source, SeriesRainfall):
            continue
        series = lookup(row.source.series)
        if isinstance(series, InlineTimeSeries):
            if any(point.value < 0 for point in series.points):
                yield issue(row, "rainfall.negative_series", "Rainfall series cannot contain negative values for physical simulation", "source",
                    path=("source", "series", "key"), related=(reference_subject(row.source.series, ("points",)),))
            try:
                clock = options.start_time or profile.option_default("start_time")
                start = datetime.combine(options.start_date or profile.option_default("start_date"), time())
                start += clock.as_duration() if isinstance(clock, DayTime) else timedelta(hours=clock.hour, minutes=clock.minute, seconds=clock.second)
                times = [start + point.time if isinstance(point.time, timedelta) else point.time for point in series.points]
                gaps = [(after-before).total_seconds() for before, after in zip(times, times[1:])]
            except OverflowError:
                gaps = []  # The resource validator reports calendar overflow.
            minimum_gap = math.floor(min(gaps) + .5) if gaps else 0
            if minimum_gap > 0 and row.interval.total_seconds() > minimum_gap:
                yield issue(row, "rainfall.interval_exceeds_series", "Recording interval exceeds the smallest series interval", "interval",
                    related=(reference_subject(row.source.series, ("points",)),
                             DiagnosticSubject(collection="swmm:options", key="settings", path=("start_date",)),
                             DiagnosticSubject(collection="swmm:options", key="settings", path=("start_time",))))
        previous = shared.setdefault(row.source.series.canonical, row)
        if previous is not row and (row.form != previous.form or row.interval != previous.interval):
            yield issue(row, "rainfall.shared_gage_settings", "Native gages sharing a series use the first gage's recording state; use consistent settings or separate series", "source",
                path=("source", "series", "key"), related=(subject(row, ("form",)), subject(row, ("interval",)),
                    subject(previous, ("source", "series", "key")), subject(previous, ("form",)),
                    subject(previous, ("interval",)), reference_subject(row.source.series, ("points",))))
        other_uses = [use for use in store.referenced_by(row.source.series) if use.owner.collection != "swmm:raingages"]
        if other_uses:
            yield issue(row, "rainfall.exclusive_series", "Native rainfall series cannot also supply another kind of consumer", "source",
                path=("source", "series", "key"), related=(reference_subject(row.source.series, ("points",)),) +
                    tuple(reference_subject(use.owner, use.path) for use in other_uses))

    for row in store.collection("swmm:snowpacks").values():
        if not valid(row):
            continue
        for name in ("plowable", "impervious", "pervious"):
            surface = getattr(row, name)
            if surface is None:
                continue
            if surface.minimum_melt > surface.maximum_melt:
                yield issue(row, "snowpack.melt_order", "Minimum melt coefficient cannot exceed maximum", name,
                    path=(name, "minimum_melt"), related=(subject(row, (name, "maximum_melt")),))
            if not for_run and surface.initial_free_water > surface.free_water_fraction * surface.initial_snow:
                yield issue(row, "snowpack.free_water_clamp", "Native limits initial free water to its snow holding capacity", name, Severity.WARNING,
                    path=(name, "initial_free_water"), related=(subject(row, (name, "free_water_fraction")),
                                                              subject(row, (name, "initial_snow"))))
            if for_run and (min(surface.minimum_melt, surface.maximum_melt, surface.initial_snow, surface.initial_free_water) < 0
                            or isinstance(surface, DepletableSnow) and surface.full_cover_depth < 0):
                yield issue(row, "snowpack.negative_parameter", "Snow depths and melt coefficients must be nonnegative for physical simulation", name)
        removal = row.removal
        if for_run and removal is not None:
            fractions = (removal.out_of_system, removal.to_impervious, removal.to_pervious, removal.immediate_melt, removal.to_subcatchment or 0)
            if min(fractions) < 0 or sum(fractions) > 1 or removal.threshold < 0:
                yield issue(row, "snowpack.removal_fractions", "Removal fractions must be nonnegative and sum to at most one; threshold must be nonnegative", "removal")
            if (removal.to_subcatchment or 0) > 0:
                if removal.destination is None:
                    yield issue(row, "snowpack.missing_destination", "Positive snow transfer without a destination can access invalid native memory", "removal.destination",
                        related=(subject(row, ("removal", "to_subcatchment")),))
                else:
                    target = lookup(removal.destination)
                    if target is not None and (target.snowpack is None or target.impervious_percent >= 100):
                        yield issue(row, "snowpack.ignored_transfer", "Native ignores transfer to a recipient without pervious snow area", "removal.destination", Severity.WARNING,
                            path=("removal", "destination", "key"), related=(subject(target, ("snowpack",)),
                                                                                 subject(target, ("impervious_percent",))))
                    elif target is not None:
                        owners = [owner for owner in catchments if owner.snowpack and owner.snowpack.canonical == Ref(collection="swmm:snowpacks", key=row.id).canonical]
                        if any(owner.area != target.area for owner in owners):
                            yield issue(row, "snowpack.native_transfer_area", "Native transfers snow depth using surface fractions without a catchment area ratio; unequal catchment areas can violate snow mass conservation", "removal.destination", Severity.WARNING,
                                path=("removal", "destination", "key"), related=(subject(target, ("area",)),) +
                                    tuple(subject(owner, ("area",)) for owner in owners if owner.area != target.area))
