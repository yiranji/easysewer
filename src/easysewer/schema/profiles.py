"""Explicit engine profiles; recognition is not a claim of model support."""

from dataclasses import dataclass

from ..io.inp.document import section_key
from ..model.identity import require_immutable
from ..model.units import UnitRules
from .option_profile import OPTION_DEFINITIONS
from ..model.report import REPORT_DEFAULTS


@dataclass(frozen=True, kw_only=True)
class SwmmProfile:
    key: str
    engine_version: str
    sections: frozenset[str]
    option_defaults: tuple[tuple[str, object], ...] = ()
    unit_rules: UnitRules | None = None
    section_terminators: tuple[tuple[str, str], ...] = ()
    transect_offset_power: int = 1
    transect_mixed_width_scaling: bool = False
    climate_defaults: tuple[tuple[str, object], ...] = ()
    report_defaults: tuple[tuple[str, object], ...] = ()

    def __post_init__(self):
        if not self.key or not self.engine_version:
            raise ValueError("A profile requires an identity and engine version")
        object.__setattr__(self, "sections", frozenset(section_key(s) for s in self.sections))
        defaults = tuple(tuple(item) for item in self.option_defaults)
        if len(dict(defaults)) != len(defaults):
            raise ValueError("Profile option defaults must be unique")
        object.__setattr__(self, "option_defaults", defaults)
        require_immutable(defaults)
        climate = tuple(tuple(item) for item in self.climate_defaults)
        if len(dict(climate)) != len(climate):
            raise ValueError("Profile climate defaults must be unique")
        object.__setattr__(self, "climate_defaults", climate)
        require_immutable(climate)
        reporting = tuple(tuple(item) for item in self.report_defaults)
        if len(dict(reporting)) != len(reporting):
            raise ValueError("Profile report defaults must be unique")
        object.__setattr__(self, "report_defaults", reporting)
        require_immutable(reporting)
        require_immutable(self.unit_rules)
        terminators = tuple((section_key(before), section_key(after)) for before, after in self.section_terminators)
        if any(before == after or before not in self.sections or after not in self.sections for before, after in terminators):
            raise ValueError("Section terminators must name different recognized sections")
        object.__setattr__(self, "section_terminators", terminators)
        if type(self.transect_offset_power) is not int or self.transect_offset_power < 1:
            raise ValueError("Transect offset conversion power must be a positive integer")
        if type(self.transect_mixed_width_scaling) is not bool:
            raise TypeError("Transect width-scaling behavior must be a boolean")

    def option_default(self, name):
        try:
            return dict(self.option_defaults)[name]
        except KeyError:
            raise ValueError(f"Profile {self.key} does not define option {name}") from None


# Fixed v5.2.4 input/GUI catalog and analysis-option defaults. Other domain
# defaults are added by their feature profiles as those modules are migrated.
EPA_SWMM_5_2_4 = SwmmProfile(
    key="epa-swmm:5.2.4",
    engine_version="5.2.4",
    section_terminators=(("TRANSECTS", "REPORT"),),
    transect_offset_power=2,
    transect_mixed_width_scaling=True,
    climate_defaults=(("ambient_temperature_f", 70.0), ("snowfall_temperature_f", 34.0),
                      ("antecedent_weight", .5), ("negative_melt_ratio", .6), ("latitude", 40.0),
                      ("file_units_us", "F"), ("file_units_si", "C")),
    option_defaults=tuple((item.field, item.default) for item in OPTION_DEFINITIONS),
    report_defaults=REPORT_DEFAULTS,
    # Tagged v5.2.4 swmm5.c Ucf/Qcf constants. Their deliberate rounding is
    # necessary when preserving internal engine quantities across unit systems.
    unit_rules=UnitRules(key="epa-swmm:5.2.4:units",
        flow_from_cfs=(("CFS", 1.0), ("GPM", 448.831), ("MGD", .64632),
                       ("CMS", .02832), ("LPS", 28.317), ("MLD", 2.4466)),
        si_per_us=(("catchment_area", .92903e-5 / 2.2956e-5), ("volume", .02832),
                   ("weir_coefficient", .028317 / .3048 ** 2.5), ("roadway_coefficient", .552),
                   ("wind_speed", 1.608), ("snow_melt_coefficient", 25.4 / 1.8),
                   ("mass", 1e-6/2.203e-6), ("mass_per_curb_day", 1e-6/2.203e-6),
                   ("mass_per_area_day", (1e-6/2.203e-6)/(.92903e-5/2.2956e-5)),
                   ("count_per_area_day", 2.2956e-5/.92903e-5), ("groundwater_flux",3048./43560.))),
    sections=frozenset("""
        TITLE OPTIONS REPORT FILES RAINGAGES EVAPORATION TEMPERATURE ADJUSTMENTS
        SUBCATCHMENTS SUBAREAS INFILTRATION LID_CONTROLS LID_USAGE AQUIFERS
        GROUNDWATER GWF SNOWPACKS JUNCTIONS OUTFALLS DIVIDERS STORAGE CONDUITS
        PUMPS ORIFICES WEIRS OUTLETS XSECTIONS TRANSECTS STREETS INLETS INLET_USAGE
        LOSSES CONTROLS POLLUTANTS LANDUSES COVERAGES LOADINGS BUILDUP WASHOFF
        TREATMENT INFLOWS DWF RDII HYDROGRAPHS CURVES TIMESERIES PATTERNS MAP
        COORDINATES VERTICES POLYGONS SYMBOLS LABELS BACKDROP TAGS PROFILES EVENTS
    """.split()),
)
