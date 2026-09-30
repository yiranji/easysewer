"""Field dimensions in native SWMM units; conversion is always explicit."""

from dataclasses import dataclass
import math
from typing import Callable

_FLOW_TO_CMS = {
    "CFS": 0.028316846592, "GPM": 0.003785411784 / 60,
    "MGD": 3785.411784 / 86400, "CMS": 1.0, "LPS": 0.001, "MLD": 1000 / 86400,
}
# A factor converts a value to the SI member, not necessarily an SI base unit.
_DIMENSIONS = {
    "length": ("ft", "m", 0.3048),
    "depth": ("ft", "m", 0.3048),
    "elevation": ("ft", "m", 0.3048),
    "area": ("ft2", "m2", 0.09290304),
    "catchment_area": ("acre", "ha", 0.40468564224),
    "volume": ("ft3", "m3", 0.028316846592),
    "velocity": ("ft/s", "m/s", 0.3048),
    "rain_depth": ("in", "mm", 25.4),
    "rain_intensity": ("in/h", "mm/h", 25.4),
    "rain_depth_per_day": ("in/day", "mm/day", 25.4),
    "conductivity": ("in/h", "mm/h", 25.4),
    "evaporation": ("in/day", "mm/day", 25.4),
    "wind_speed": ("mile/h", "km/h", 1.609344),
    "ratio": ("1", "1", 1.0),
    "count": ("count", "count", 1.0),
    "code": ("code", "code", 1.0),
    "run/rise": ("run/rise", "run/rise", 1.0),
    "percent": ("%", "%", 1.0),
    "slope": ("rise/run", "rise/run", 1.0),
    "manning": ("s/m^(1/3)", "s/m^(1/3)", 1.0),
    "days": ("day", "day", 1.0),
    "hours": ("hour", "hour", 1.0),
    "weir_coefficient": ("cfs/ft^2.5", "cms/m^2.5", 0.3048 ** .5),
    "roadway_coefficient": ("cfs/ft^2.5", "cms/m^2.5", 0.3048 ** .5),
    "temperature_change": ("delta F", "delta C", 5 / 9),
    "angle": ("degree", "degree", 1.0),
    "minutes": ("minute", "minute", 1.0),
    "inverse_hours": ("1/hour", "1/hour", 1.0),
    "inverse_days": ("1/day", "1/day", 1.0),
    "mass": ("lb", "kg", .45359237),
    "mass_per_area_day": ("lb/acre/day", "kg/ha/day", .45359237/.40468564224),
    "mass_per_curb_day": ("lb/user curb/day", "kg/user curb/day", .45359237),
    "count_per_area_day": ("count/acre/day", "count/ha/day", 1/.40468564224),
    "count_per_curb_day": ("count/user curb/day", "count/user curb/day", 1.),
    "concentration_mg_l": ("mg/L", "mg/L", 1.),
    "concentration_ug_l": ("ug/L", "ug/L", 1.),
    "concentration_count_l": ("count/L", "count/L", 1.),
    "external_mass_rate": ("user mass-rate units", "user mass-rate units", 1.),
    "groundwater_flux": ("cfs/acre", "cms/ha", 0.028316846592/0.40468564224),
    "user_length": ("user length", "user length", 1.0),
    "snow_melt_coefficient": ("in/hour/F", "mm/hour/C", 25.4 * 1.8),
}


@dataclass(frozen=True, kw_only=True)
class UnitTransform:
    """An explicit extension rule for one exact value type."""
    value_type: type
    convert: Callable

    def __post_init__(self):
        if not isinstance(self.value_type, type) or not callable(self.convert):
            raise TypeError("Unit transforms require a value type and conversion function")


@dataclass(frozen=True, kw_only=True)
class ConversionContext:
    source: "UnitContext"
    target: "UnitContext"
    rules: "UnitRules | None"
    profile: object
    record: Callable
    referenced_by: Callable
    resource_uses: Callable
    convert_value: Callable | None = None

    def number(self, value, dimension):
        return self.source.convert(value, dimension=dimension, to=self.target, rules=self.rules)


@dataclass(frozen=True, kw_only=True)
class UnitRules:
    """Pinned engine factors, distinct from exact physical unit definitions.

    ``flow_from_cfs`` maps internal cfs to each engine flow unit;
    ``si_per_us`` overrides a dimension's US-to-SI factor. No ambient engine or
    platform state is consulted when converting a value.
    """
    key: str
    flow_from_cfs: tuple[tuple[str, float], ...]
    si_per_us: tuple[tuple[str, float], ...] = ()

    def __post_init__(self):
        if not isinstance(self.key, str) or not self.key:
            raise ValueError("Unit rules require a stable identity")
        for name in ("flow_from_cfs", "si_per_us"):
            pairs = tuple(tuple(pair) for pair in getattr(self, name))
            if any(len(pair) != 2 or not isinstance(pair[0], str) or isinstance(pair[1], bool)
                   or not isinstance(pair[1], (int, float)) or not math.isfinite(pair[1]) or pair[1] <= 0
                   for pair in pairs):
                raise ValueError("Unit factors must have a name and a finite positive factor")
            if len(dict(pairs)) != len(pairs):
                raise ValueError("Duplicate unit conversion factor")
            object.__setattr__(self, name, pairs)
        if set(dict(self.flow_from_cfs)) != set(_FLOW_TO_CMS):
            raise ValueError("Unit rules must cover all six SWMM flow units")
        if not set(dict(self.si_per_us)) <= set(_DIMENSIONS):
            raise ValueError("Unit rules contain an unknown dimension")


@dataclass(frozen=True, kw_only=True)
class UnitContext:
    flow_units: str = "CFS"

    def __post_init__(self):
        units = self.flow_units.upper()
        if units not in _FLOW_TO_CMS:
            raise ValueError(f"Unknown SWMM flow units: {self.flow_units}")
        object.__setattr__(self, "flow_units", units)

    @property
    def system(self) -> str:
        return "US" if self.flow_units in ("CFS", "GPM", "MGD") else "SI"

    def unit(self, dimension: str) -> str:
        if dimension == "flow":
            return self.flow_units
        if dimension == "temperature":
            return "F" if self.system == "US" else "C"
        try:
            item = _DIMENSIONS[dimension]
        except KeyError:
            raise ValueError(f"Dimension needs an explicit field-specific unit rule: {dimension}") from None
        return item[0 if self.system == "US" else 1]

    def convert(self, value: float, *, dimension: str, to: "UnitContext", rules: UnitRules | None = None) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError("Unit conversion requires a finite numeric value")
        self.unit(dimension)  # Reject unsupported dimensions even for identical contexts.
        if dimension == "flow":
            if rules is not None:
                factors = dict(rules.flow_from_cfs)
                return value / factors[self.flow_units] * factors[to.flow_units]
            return value * _FLOW_TO_CMS[self.flow_units] / _FLOW_TO_CMS[to.flow_units]
        if self.system == to.system:
            return float(value)
        if dimension == "temperature":
            return (value - 32) * 5 / 9 if self.system == "US" else value * 9 / 5 + 32
        factor = _DIMENSIONS[dimension][2]
        if rules is not None:
            factor = dict(rules.si_per_us).get(dimension, factor)
        return value * factor if self.system == "US" else value / factor
