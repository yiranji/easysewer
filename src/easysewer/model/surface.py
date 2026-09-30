"""Transects, streets and inlet designs/usages in model units."""

from dataclasses import dataclass, replace
from typing import ClassVar, Literal

from ..validation import Diagnostic, Severity, ValidationReport
from .fields import number, reference, validate_fields
from .identity import Ref
from .resources import Resource
from .store import CollectionSpec
from .units import UnitTransform


@dataclass(frozen=True, kw_only=True)
class TransectRoughness:
    left: float = number("manning", positive=True)
    right: float = number("manning", positive=True)
    channel: float = number("manning", positive=True)


@dataclass(frozen=True, kw_only=True)
class TransectPoint:
    elevation: float = number("elevation")
    station: float = number("length")


@dataclass(frozen=True, kw_only=True)
class Transect(Resource):
    kind: ClassVar[str] = "TRANSECT"
    roughness: TransectRoughness
    left_bank: float = number("length")
    right_bank: float = number("length")
    stations: tuple[TransectPoint, ...]
    meander_factor: float = number("ratio", 0, minimum=0)
    width_factor: float = number("ratio", 0, minimum=0)
    elevation_offset: float = number("transect_elevation_offset", 0)

    def validate_local(self):
        if not 2 <= len(self.stations) <= 1499:
            yield Diagnostic(code="transect.station_count", message="SWMM 5.2.4 requires 2 to 1499 stations", field="stations")
        if self.left_bank > self.right_bank:
            yield Diagnostic(code="transect.bank_order", message="Left bank must not exceed right bank", field="left_bank")
        if self.stations:
            if max(point.elevation for point in self.stations) <= min(point.elevation for point in self.stations):
                yield Diagnostic(code="transect.no_depth", message="Transect must have positive depth", field="stations")
            if self.stations[-1].station <= self.stations[0].station:
                yield Diagnostic(code="transect.no_width", message="Transect must have positive width", field="stations")
            for index, (before, after) in enumerate(zip(self.stations, self.stations[1:]), 1):
                if after.station < before.station:
                    yield Diagnostic(code="transect.station_order", message="Stations must be nondecreasing (vertical sides are allowed)",
                                     field=f"stations[{index}].station")
            positions = {point.station for point in self.stations}
            for name in ("left_bank", "right_bank"):
                if getattr(self, name) not in positions:
                    yield Diagnostic(code="transect.bank_not_station", severity=Severity.WARNING,
                                     message="Bank position does not coincide with a station", field=name)


@dataclass(frozen=True, kw_only=True)
class StreetSection(Resource):
    kind: ClassVar[str] = "STREET"
    crown_width: float = number("length", positive=True)
    curb_height: float = number("depth", positive=True)
    cross_slope: float = number("percent", positive=True)
    road_roughness: float = number("manning", positive=True)
    gutter_depression: float | None = number("length", None, minimum=0)
    gutter_width: float | None = number("length", None, minimum=0)
    sides: Literal[1, 2] | None = number("count", None, integer=True)
    backing_width: float | None = number("length", None, minimum=0)
    backing_slope: float | None = number("percent", None, minimum=0)
    backing_roughness: float | None = number("manning", None, minimum=0)

    def validate_local(self):
        if self.backing_width and (not self.backing_slope or not self.backing_roughness):
            yield Diagnostic(code="street.incomplete_backing", message="Positive backing width requires positive backing slope and roughness", field="backing_width")
        if self.gutter_depression and not self.gutter_width:
            yield Diagnostic(code="street.incomplete_gutter", message="Positive gutter depression requires positive width", field="gutter_width")
        if self.gutter_width is not None and self.gutter_width > self.crown_width:
            yield Diagnostic(code="street.gutter_too_wide", message="Gutter width cannot exceed curb-to-crown width", field="gutter_width")


@dataclass(frozen=True, kw_only=True)
class GrateType:
    pass


@dataclass(frozen=True, kw_only=True)
class StandardGrate(GrateType):
    kind: Literal["P_BAR-50", "P_BAR-50X100", "P_BAR-30", "CURVED_VANE", "TILT_BAR-45", "TILT_BAR-30", "RETICULINE"]


@dataclass(frozen=True, kw_only=True)
class GenericGrate(GrateType):
    kind: ClassVar[str] = "GENERIC"
    open_fraction: float = number("ratio", positive=True, maximum=1)
    splash_velocity: float | None = number("velocity", None, minimum=0)


@dataclass(frozen=True, kw_only=True)
class InletStructure:
    pass


@dataclass(frozen=True, kw_only=True)
class GrateInlet(InletStructure):
    kind: Literal["GRATE", "DROP_GRATE"]
    length: float = number("length", positive=True)
    width: float = number("length", positive=True)
    grate: GrateType


@dataclass(frozen=True, kw_only=True)
class CurbInlet(InletStructure):
    kind: Literal["CURB", "DROP_CURB"]
    length: float = number("length", positive=True)
    height: float = number("depth", positive=True)
    throat: Literal["HORIZONTAL", "INCLINED", "VERTICAL"] | None = None

    def validate_local(self):
        if self.kind == "DROP_CURB" and self.throat is not None:
            yield Diagnostic(code="inlet.unused_throat", message="DROP_CURB has no throat-angle parameter", field="throat")


@dataclass(frozen=True, kw_only=True)
class CombinationInlet(InletStructure):
    kind: ClassVar[str] = "COMBINATION"
    grate: GrateInlet
    curb: CurbInlet

    def validate_local(self):
        if self.grate.kind != "GRATE" or self.curb.kind != "CURB":
            yield Diagnostic(code="inlet.invalid_combination", message="A combination inlet requires one GRATE and one CURB")


@dataclass(frozen=True, kw_only=True)
class SlottedInlet(InletStructure):
    kind: ClassVar[str] = "SLOTTED"
    length: float = number("length", positive=True)
    width: float = number("length", positive=True)


@dataclass(frozen=True, kw_only=True)
class CustomInlet(InletStructure):
    kind: ClassVar[str] = "CUSTOM"
    curve: Ref = reference("swmm:curves")


@dataclass(frozen=True, kw_only=True)
class InletDesign(Resource):
    design: InletStructure


@dataclass(frozen=True, kw_only=True)
class InletUsage:
    link: Ref = reference("swmm:links")
    inlet: Ref = reference("swmm:inlets")
    node: Ref = reference("swmm:nodes")
    count: int | None = number("count", None, minimum=1, integer=True)
    percent_clogged: float | None = number("percent", None, minimum=0, maximum=99)
    maximum_flow: float | None = number("flow", None, minimum=0)
    local_depression: float | None = number("length", None, minimum=0)
    local_width: float | None = number("length", None, minimum=0)
    placement: Literal["AUTOMATIC", "ON_GRADE", "ON_SAG"] | None = None


TRANSECTS_COLLECTION = CollectionSpec(key="swmm:transects", record_type=Transect, key_of=lambda row: row.id,
                                      identity_field="id", validate=validate_fields)
SURFACE_COLLECTIONS = (
    CollectionSpec(key="swmm:streets", record_type=StreetSection, key_of=lambda row: row.id, identity_field="id", validate=validate_fields),
    CollectionSpec(key="swmm:inlets", record_type=InletDesign, key_of=lambda row: row.id, identity_field="id", validate=validate_fields),
    CollectionSpec(key="swmm:inlet_usage", record_type=InletUsage, key_of=lambda row: row.link.key, validate=validate_fields),
)


def _convert_transect(value, context):
    if context.source.system == context.target.system:
        return value
    factor = context.number(1, "length")
    power = context.profile.transect_offset_power if context.rules is not None else 1
    if context.rules is not None and context.profile.transect_mixed_width_scaling and value.width_factor not in (0, 1):
        if context.source.system == "SI":
            ValidationReport(diagnostics=(Diagnostic(
                code="units.transect_width_rounding", object_id=value.id, field="width_factor",
                message="Cannot guarantee engine-preserving conversion of an SI transect with a non-unit width factor: "
                        "native bank/station rounding can change overbank roughness. Explicit physical conversion "
                        "or an intentional geometry edit requires hydraulic revalidation.",
            ),)).raise_for_errors()
        # Native setParams uses (bank / UCF) * width, while addStation uses
        # (station * width) / UCF. getFlow compares them with exact equality.
        # Materialize width in US coordinates, where UCF is 1, before converting;
        # banks and coincident stations then share the same arithmetic in SI.
        width = value.width_factor
        value = replace(value, width_factor=1, left_bank=value.left_bank * width, right_bank=value.right_bank * width,
                        stations=tuple(replace(point, station=point.station * width) for point in value.stations))
    return replace(value, left_bank=value.left_bank * factor, right_bank=value.right_bank * factor,
                   elevation_offset=value.elevation_offset * factor ** power,
                   stations=tuple(TransectPoint(elevation=point.elevation * factor, station=point.station * factor)
                                  for point in value.stations))


TRANSECT_UNIT_TRANSFORMS = (UnitTransform(value_type=Transect, convert=_convert_transect),)
