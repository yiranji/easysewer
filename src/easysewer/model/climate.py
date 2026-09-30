"""Climate configuration and consumer roles, independent of INP and file IO."""

from dataclasses import dataclass, replace
from datetime import date
import math
from typing import ClassVar, Literal

from ..validation import Diagnostic, DiagnosticSubject, Severity, ValidationReport
from .fields import number, reference, validate_fields
from .identity import Ref
from .resources import InlineTimeSeries
from .store import CollectionSpec
from .units import UnitContext, UnitTransform
from .usage import ResourceUse
from .values import FileReference


@dataclass(frozen=True, kw_only=True)
class MonthlyValues:
    values: tuple[float, ...]
    dimension: ClassVar[str] = "ratio"

    def validate_local(self):
        if len(self.values) != 12:
            yield Diagnostic(code="climate.month_count", message="Monthly data requires exactly twelve values", field="values")
        for index, value in enumerate(self.values):
            if not math.isfinite(value):
                yield Diagnostic(code="climate.nonfinite_value", message="Monthly values must be finite", field=f"values[{index}]")


@dataclass(frozen=True, kw_only=True)
class MonthlyFactors(MonthlyValues):
    pass


@dataclass(frozen=True, kw_only=True)
class MonthlyWindSpeeds(MonthlyValues):
    dimension: ClassVar[str] = "wind_speed"


@dataclass(frozen=True, kw_only=True)
class MonthlyEvaporation(MonthlyValues):
    dimension: ClassVar[str] = "evaporation"


@dataclass(frozen=True, kw_only=True)
class MonthlyTemperatureChanges(MonthlyValues):
    dimension: ClassVar[str] = "temperature_change"


@dataclass(frozen=True, kw_only=True)
class FileTemperature:
    """Use min/max daily temperatures in the shared climate file."""


@dataclass(frozen=True, kw_only=True)
class SeriesTemperature:
    series: Ref = reference("swmm:timeseries")


@dataclass(frozen=True, kw_only=True)
class ClimateFile:
    file: FileReference
    start_date: date | None = None
    units: Literal["C10", "C", "F"] | None = None

    def validate_local(self):
        if self.file.direction != "input":
            yield Diagnostic(code="climate.file_direction", message="Climate files must have input direction", field="file")


@dataclass(frozen=True, kw_only=True)
class FileWind:
    pass


@dataclass(frozen=True, kw_only=True)
class Snowmelt:
    snowfall_temperature: float = number("temperature")
    antecedent_weight: float = number("ratio", minimum=0, maximum=1)
    negative_melt_ratio: float = number("ratio", minimum=0, maximum=1)
    elevation: float = number("elevation")
    latitude: float = number("angle")
    solar_time_correction: float = number("minutes")

    def validate_local(self):
        if not -89.99 < self.latitude < 89.99:
            yield Diagnostic(code="climate.latitude_range", message="Native latitude must lie strictly between -89.99 and 89.99", field="latitude")


@dataclass(frozen=True, kw_only=True)
class ArealDepletion:
    fractions: tuple[float, ...]

    def validate_local(self):
        if len(self.fractions) != 10:
            yield Diagnostic(code="climate.adc_count", message="Areal depletion requires ten fractions", field="fractions")
        for index, value in enumerate(self.fractions):
            if not math.isfinite(value) or not 0 <= value <= 1:
                yield Diagnostic(code="climate.adc_fraction", message="Depletion fractions must be finite and between zero and one", field=f"fractions[{index}]")


@dataclass(frozen=True, kw_only=True)
class ConstantEvaporation:
    rate: float = number("evaporation")


@dataclass(frozen=True, kw_only=True)
class SeriesEvaporation:
    series: Ref = reference("swmm:timeseries")


@dataclass(frozen=True, kw_only=True)
class TemperatureEvaporation:
    pass


@dataclass(frozen=True, kw_only=True)
class FileEvaporation:
    pan_coefficients: MonthlyFactors | None = None


@dataclass(frozen=True, kw_only=True)
class Evaporation:
    source: ConstantEvaporation | MonthlyEvaporation | SeriesEvaporation | TemperatureEvaporation | FileEvaporation | None = None
    recovery_pattern: Ref | None = reference("swmm:patterns", None)
    dry_only: bool | None = None


@dataclass(frozen=True, kw_only=True)
class ClimateAdjustments:
    temperature: MonthlyTemperatureChanges | None = None
    evaporation: MonthlyEvaporation | None = None
    rainfall: MonthlyFactors | None = None
    conductivity: MonthlyFactors | None = None


@dataclass(frozen=True, kw_only=True)
class SubcatchmentAdjustments:
    subcatchment: Ref = reference("swmm:subcatchments")
    infiltration: Ref | None = reference("swmm:patterns", None)
    depression_storage: Ref | None = reference("swmm:patterns", None)
    pervious_roughness: Ref | None = reference("swmm:patterns", None)


@dataclass(frozen=True, kw_only=True)
class Climate:
    temperature: FileTemperature | SeriesTemperature | None = None
    file: ClimateFile | None = None
    wind: MonthlyWindSpeeds | FileWind | None = None
    snowmelt: Snowmelt | None = None
    impervious_depletion: ArealDepletion | None = None
    pervious_depletion: ArealDepletion | None = None
    evaporation: Evaporation | None = None
    adjustments: ClimateAdjustments | None = None


def get_climate(store):
    try:
        return store.collection("swmm:climate")["settings"]
    except KeyError:
        return Climate()


CLIMATE_COLLECTIONS = (
    CollectionSpec(key="swmm:climate", record_type=Climate, key_of=lambda row: "settings", validate=validate_fields),
    CollectionSpec(key="swmm:subcatchment_adjustments", record_type=SubcatchmentAdjustments,
                   key_of=lambda row: row.subcatchment.key, validate=validate_fields),
)


@dataclass(frozen=True, kw_only=True)
class ConstantTemperature:
    """Resolved native ambient temperature, not an arbitrary INP source mode."""
    value: float = number("temperature")


@dataclass(frozen=True, kw_only=True)
class ResolvedClimate:
    temperature: FileTemperature | SeriesTemperature | ConstantTemperature
    file: ClimateFile | None
    wind: MonthlyWindSpeeds | FileWind
    snowmelt: Snowmelt
    impervious_depletion: ArealDepletion
    pervious_depletion: ArealDepletion
    evaporation: Evaporation
    adjustments: ClimateAdjustments


def resolve_climate(climate, options, profile):
    """Return effective defaults without materializing them into the source model."""
    ValidationReport(diagnostics=tuple(validate_fields(climate)) + tuple(validate_fields(options))).raise_for_errors()
    units = UnitContext(flow_units=options.flow_units or profile.option_default("flow_units"))
    defaults = dict(profile.climate_defaults)
    if not defaults:
        raise ValueError(f"Profile {profile.key} has no climate defaults")
    temperature = climate.temperature or (FileTemperature() if climate.file else ConstantTemperature(
        value=UnitContext().convert(defaults["ambient_temperature_f"], dimension="temperature", to=units)))
    climate_file = climate.file
    if climate_file is not None:
        climate_file = replace(climate_file,
            units=climate_file.units or defaults["file_units_us" if units.system == "US" else "file_units_si"],
            start_date=climate_file.start_date or options.start_date or profile.option_default("start_date"))
    evaporation = climate.evaporation or Evaporation()
    evaporation = replace(evaporation, source=evaporation.source or ConstantEvaporation(rate=0),
                          dry_only=False if evaporation.dry_only is None else evaporation.dry_only)
    if isinstance(evaporation.source, FileEvaporation) and evaporation.source.pan_coefficients is None:
        evaporation = replace(evaporation, source=FileEvaporation(pan_coefficients=MonthlyFactors(values=(1.0,) * 12)))
    adjustments = climate.adjustments or ClimateAdjustments()
    return ResolvedClimate(temperature=temperature, file=climate_file,
        wind=climate.wind or MonthlyWindSpeeds(values=(0.0,) * 12),
        snowmelt=climate.snowmelt or Snowmelt(
            snowfall_temperature=UnitContext().convert(defaults["snowfall_temperature_f"], dimension="temperature", to=units),
            antecedent_weight=defaults["antecedent_weight"], negative_melt_ratio=defaults["negative_melt_ratio"],
            elevation=0, latitude=defaults["latitude"], solar_time_correction=0),
        impervious_depletion=climate.impervious_depletion or ArealDepletion(fractions=(1.0,) * 10),
        pervious_depletion=climate.pervious_depletion or ArealDepletion(fractions=(1.0,) * 10), evaporation=evaporation,
        adjustments=ClimateAdjustments(temperature=adjustments.temperature or MonthlyTemperatureChanges(values=(0.0,) * 12),
            evaporation=adjustments.evaporation or MonthlyEvaporation(values=(0.0,) * 12),
            rainfall=adjustments.rainfall or MonthlyFactors(values=(1.0,) * 12),
            conductivity=MonthlyFactors(values=tuple(value if value > 0 else 1.0 for value in adjustments.conductivity.values))
            if adjustments.conductivity else MonthlyFactors(values=(1.0,) * 12)))


def climate_resource_uses(store):
    climate = get_climate(store)
    owner = Ref(collection="swmm:climate", key="settings")
    if ValidationReport(diagnostics=tuple(validate_fields(climate))).is_valid:
        if isinstance(climate.temperature, SeriesTemperature):
            yield ResourceUse(owner=owner, target=climate.temperature.series, path=("temperature", "series"),
                              role="air temperature", dimensions=("temperature",))
        evaporation = climate.evaporation
        if evaporation is not None:
            if isinstance(evaporation.source, SeriesEvaporation):
                yield ResourceUse(owner=owner, target=evaporation.source.series, path=("evaporation", "source", "series"),
                                  role="potential evaporation", dimensions=("evaporation",))
            if evaporation.recovery_pattern is not None:
                yield ResourceUse(owner=owner, target=evaporation.recovery_pattern, path=("evaporation", "recovery_pattern"),
                                  role="infiltration recovery", accepted_kinds=("MONTHLY",))


def subcatchment_resource_uses(store):
    for row in store.collection("swmm:subcatchment_adjustments").values():
        if not ValidationReport(diagnostics=tuple(validate_fields(row))).is_valid:
            continue
        for field in ("infiltration", "depression_storage", "pervious_roughness"):
            target = getattr(row, field)
            if target is not None:
                yield ResourceUse(owner=Ref(collection="swmm:subcatchment_adjustments", key=row.subcatchment.key),
                    target=target, path=(field,), role=f"subcatchment {field} adjustment", accepted_kinds=("MONTHLY",))


def _convert_monthly(value, context):
    return replace(value, values=tuple(context.number(item, value.dimension) for item in value.values))


def _convert_file(value, context):
    if context.source.system != context.target.system:
        ValidationReport(diagnostics=(Diagnostic(code="units.external_climate_requires_conversion",
            message="Climate file formats have different temperature, evaporation and wind rules; explicitly inspect/convert the external data before changing unit systems"),)).raise_for_errors()
    return value


CLIMATE_UNIT_TRANSFORMS = tuple(UnitTransform(value_type=kind, convert=_convert_monthly) for kind in (
    MonthlyFactors, MonthlyWindSpeeds, MonthlyEvaporation, MonthlyTemperatureChanges)) + (
    UnitTransform(value_type=ArealDepletion, convert=lambda value, context: value),
    UnitTransform(value_type=ClimateFile, convert=_convert_file),
)


def validate_climate(store, profile, *, for_run=False):
    climate = get_climate(store)
    if not ValidationReport(diagnostics=tuple(validate_fields(climate))).is_valid:
        return

    def subject(path):
        return DiagnosticSubject(collection="swmm:climate", key="settings", path=path)

    def issue(code, message, field=None, severity=Severity.ERROR, *, path=None, related=()):
        return Diagnostic(code=code, message=message, field=field, object_id="climate", severity=severity,
            subject=subject(path if path is not None else tuple(field.split('.')) if field else ()), related=related)

    evaporation = climate.evaporation.source if climate.evaporation else None
    if isinstance(climate.temperature, FileTemperature) and climate.file is None:
        yield issue("climate.missing_temperature_file", "A file temperature declaration cannot be written without its climate file", "file",
            related=(subject(("temperature",)),))
    if for_run:
        if climate.file is None and (isinstance(climate.temperature, FileTemperature) or isinstance(climate.wind, FileWind)
                                    or isinstance(evaporation, (FileEvaporation, TemperatureEvaporation))):
            consumers = tuple(subject(path) for value, kinds, path in (
                (climate.temperature, (FileTemperature,), ("temperature",)),
                (climate.wind, (FileWind,), ("wind",)),
                (evaporation, (FileEvaporation, TemperatureEvaporation), ("evaporation", "source")))
                if isinstance(value, kinds))
            yield issue("climate.missing_file", "File temperature/wind/evaporation and temperature-derived evaporation require a climate file", "file",
                related=consumers)
        if isinstance(evaporation, TemperatureEvaporation) and isinstance(climate.temperature, SeriesTemperature):
            yield issue("climate.temperature_evap_source", "Native temperature-derived evaporation only updates from file-based daily temperature, not a temperature series", "temperature",
                path=("temperature", "series", "key"), related=(subject(("evaporation", "source")),
                    DiagnosticSubject(collection=climate.temperature.series.collection, key=climate.temperature.series.key)))
        values = (evaporation.rate,) if isinstance(evaporation, ConstantEvaporation) else evaporation.values if isinstance(evaporation, MonthlyEvaporation) else ()
        if isinstance(evaporation, SeriesEvaporation):
            try:
                series = store.collection("swmm:timeseries")[evaporation.series.key]
            except KeyError:
                series = None  # Reference validation reports the missing target.
            if isinstance(series, InlineTimeSeries) and ValidationReport(diagnostics=tuple(validate_fields(series))).is_valid:
                values = tuple(point.value for point in series.points)
        if any(value < 0 for value in values):
            if isinstance(evaporation, SeriesEvaporation):
                path = ("evaporation", "source", "series", "key")
                related = (DiagnosticSubject(collection=evaporation.series.collection,
                                             key=evaporation.series.key, path=("points",)),)
            else:
                path = ("evaporation", "source", "rate" if isinstance(evaporation, ConstantEvaporation) else "values")
                related = ()
            yield issue("climate.negative_evaporation", "Potential evaporation rates must be nonnegative for physical simulation", "evaporation.source",
                path=path, related=related)
        if isinstance(climate.wind, MonthlyWindSpeeds) and any(value < 0 for value in climate.wind.values):
            yield issue("climate.negative_wind", "Wind speeds must be nonnegative for physical simulation", "wind", path=("wind", "values"))
        if isinstance(evaporation, FileEvaporation) and evaporation.pan_coefficients and any(value < 0 for value in evaporation.pan_coefficients.values):
            yield issue("climate.negative_pan_coefficient", "Pan coefficients must be nonnegative for physical simulation", "evaporation.source",
                path=("evaporation", "source", "pan_coefficients", "values"))
    elif climate.adjustments and climate.adjustments.conductivity and any(value <= 0 for value in climate.adjustments.conductivity.values):
        yield issue("climate.conductivity_default", "Native replaces nonpositive monthly conductivity factors with 1", "adjustments.conductivity", Severity.WARNING,
            path=("adjustments", "conductivity", "values"))
