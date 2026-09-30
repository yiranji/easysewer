"""Hydraulic entities and parameter variants, with no file IO or native state."""

from dataclasses import dataclass
from datetime import timedelta
from typing import ClassVar, Literal

from .fields import number, reference, validate_fields
from .geometry import CrossSection
from .identity import Ref, validate_identifier
from .store import CollectionSpec
from .values import Offset, Point
from ..validation import Diagnostic


@dataclass(frozen=True, kw_only=True)
class Entity:
    id: str

    def __post_init__(self):
        validate_identifier(self.id)


@dataclass(frozen=True, kw_only=True)
class Node(Entity):
    elevation: float = number("elevation")
    position: Point | None = None


@dataclass(frozen=True, kw_only=True)
class Junction(Node):
    kind: ClassVar[str] = "JUNCTIONS"
    max_depth: float | None = number("depth", None, minimum=0)
    initial_depth: float | None = number("depth", None, minimum=0)
    surcharge_depth: float | None = number("depth", None, minimum=0)
    ponded_area: float | None = number("area", None, minimum=0)


@dataclass(frozen=True, kw_only=True)
class Boundary:
    kind: ClassVar[str] = "boundary"


@dataclass(frozen=True, kw_only=True)
class FreeBoundary(Boundary):
    kind: ClassVar[str] = "FREE"


@dataclass(frozen=True, kw_only=True)
class NormalBoundary(Boundary):
    kind: ClassVar[str] = "NORMAL"


@dataclass(frozen=True, kw_only=True)
class FixedBoundary(Boundary):
    kind: ClassVar[str] = "FIXED"
    stage: float = number("elevation")


@dataclass(frozen=True, kw_only=True)
class TidalBoundary(Boundary):
    kind: ClassVar[str] = "TIDAL"
    curve: Ref = reference("swmm:curves")


@dataclass(frozen=True, kw_only=True)
class SeriesBoundary(Boundary):
    kind: ClassVar[str] = "TIMESERIES"
    series: Ref = reference("swmm:timeseries")


@dataclass(frozen=True, kw_only=True)
class Outfall(Node):
    kind: ClassVar[str] = "OUTFALLS"
    boundary: Boundary
    gated: bool | None = None
    route_to: Ref | None = reference("swmm:subcatchments", None)


@dataclass(frozen=True, kw_only=True)
class StorageShape:
    kind: ClassVar[str] = "storage_shape"


@dataclass(frozen=True, kw_only=True)
class FunctionalStorage(StorageShape):
    kind: ClassVar[str] = "FUNCTIONAL"
    coefficient: float = number("storage_coefficient")
    exponent: float = number("ratio")
    constant: float = number("area", minimum=0)


@dataclass(frozen=True, kw_only=True)
class TabularStorage(StorageShape):
    kind: ClassVar[str] = "TABULAR"
    curve: Ref = reference("swmm:curves")


@dataclass(frozen=True, kw_only=True)
class CylindricalStorage(StorageShape):
    kind: ClassVar[str] = "CYLINDRICAL"
    major_axis: float = number("length", positive=True)
    minor_axis: float = number("length", positive=True)


@dataclass(frozen=True, kw_only=True)
class ConicalStorage(StorageShape):
    kind: ClassVar[str] = "CONICAL"
    base_major_axis: float = number("length", positive=True)
    base_minor_axis: float = number("length", positive=True)
    side_slope: float = number("run/rise", minimum=0)


@dataclass(frozen=True, kw_only=True)
class ParaboloidStorage(StorageShape):
    kind: ClassVar[str] = "PARABOLOID"
    top_major_axis: float = number("length", positive=True)
    top_minor_axis: float = number("length", positive=True)
    full_height: float = number("length", positive=True)


@dataclass(frozen=True, kw_only=True)
class PyramidalStorage(StorageShape):
    kind: ClassVar[str] = "PYRAMIDAL"
    base_length: float = number("length", positive=True)
    base_width: float = number("length", positive=True)
    side_slope: float = number("run/rise", minimum=0)


@dataclass(frozen=True, kw_only=True)
class StorageSeepage:
    """Base for the distinct native constant and Green-Ampt input forms."""


@dataclass(frozen=True, kw_only=True)
class ConstantSeepage(StorageSeepage):
    conductivity: float = number("conductivity", minimum=0)


@dataclass(frozen=True, kw_only=True)
class Seepage(StorageSeepage):
    suction: float = number("rain_depth", minimum=0)
    conductivity: float = number("conductivity", minimum=0)
    initial_deficit: float = number("ratio", minimum=0, maximum=1)


@dataclass(frozen=True, kw_only=True)
class Storage(Node):
    kind: ClassVar[str] = "STORAGE"
    max_depth: float = number("depth", minimum=0)
    initial_depth: float = number("depth", minimum=0)
    shape: StorageShape
    surcharge_depth: float | None = number("depth", None, minimum=0)
    evaporation_fraction: float | None = number("ratio", None, minimum=0, maximum=1)
    seepage: StorageSeepage | None = None
    polygon: tuple[Point, ...] = ()


@dataclass(frozen=True, kw_only=True)
class DividerLaw:
    kind: ClassVar[str] = "divider_law"


@dataclass(frozen=True, kw_only=True)
class OverflowDivider(DividerLaw):
    kind: ClassVar[str] = "OVERFLOW"


@dataclass(frozen=True, kw_only=True)
class CutoffDivider(DividerLaw):
    kind: ClassVar[str] = "CUTOFF"
    cutoff_flow: float = number("flow", minimum=0)


@dataclass(frozen=True, kw_only=True)
class TabularDivider(DividerLaw):
    kind: ClassVar[str] = "TABULAR"
    curve: Ref = reference("swmm:curves")


@dataclass(frozen=True, kw_only=True)
class WeirDivider(DividerLaw):
    kind: ClassVar[str] = "WEIR"
    minimum_flow: float = number("flow", minimum=0)
    height: float = number("depth", positive=True)
    coefficient: float = number("divider_weir_coefficient", positive=True)


@dataclass(frozen=True, kw_only=True)
class Divider(Node):
    kind: ClassVar[str] = "DIVIDERS"
    diverted_link: Ref | None = reference("swmm:links", None)
    law: DividerLaw
    max_depth: float | None = number("depth", None, minimum=0)
    initial_depth: float | None = number("depth", None, minimum=0)
    surcharge_depth: float | None = number("depth", None, minimum=0)
    ponded_area: float | None = number("area", None, minimum=0)


@dataclass(frozen=True, kw_only=True)
class Link(Entity):
    inlet: Ref = reference("swmm:nodes")
    outlet: Ref = reference("swmm:nodes")
    vertices: tuple[Point, ...] = ()


@dataclass(frozen=True, kw_only=True)
class ConduitLosses:
    entry: float = number("ratio", minimum=0)
    exit: float = number("ratio", minimum=0)
    average: float = number("ratio", minimum=0)
    flap_gate: bool | None = None
    seepage: float | None = number("conductivity", None, minimum=0)


@dataclass(frozen=True, kw_only=True)
class Conduit(Link):
    kind: ClassVar[str] = "CONDUITS"
    length: float = number("length", positive=True)
    roughness: float = number("manning", positive=True)
    inlet_offset: float | Offset | None = number("offset", None, markers=(Offset.NODE_INVERT,))
    outlet_offset: float | Offset | None = number("offset", None, markers=(Offset.NODE_INVERT,))
    initial_flow: float | None = number("flow", None)
    maximum_flow: float | None = number("flow", None, minimum=0)
    section: CrossSection | None = None
    losses: ConduitLosses | None = None


@dataclass(frozen=True, kw_only=True)
class Pump(Link):
    kind: ClassVar[str] = "PUMPS"
    curve: Ref | None = reference("swmm:curves", None)
    initially_on: bool | None = None
    startup_depth: float | None = number("depth", None, minimum=0)
    shutoff_depth: float | None = number("depth", None, minimum=0)


@dataclass(frozen=True, kw_only=True)
class Orifice(Link):
    kind: ClassVar[str] = "ORIFICES"
    orientation: Literal["SIDE", "BOTTOM"]
    offset: float | Offset | None = number("offset", None, markers=(Offset.NODE_INVERT,))
    coefficient: float = number("ratio", minimum=0)
    gated: bool | None = None
    opening_time: timedelta | None = None
    section: CrossSection | None = None

    def validate_local(self):
        if self.opening_time is not None and self.opening_time < timedelta(0):
            yield Diagnostic(code="orifice.negative_opening_time", message="Opening time must be nonnegative", field="opening_time")


@dataclass(frozen=True, kw_only=True)
class Weir(Link):
    kind: ClassVar[str] = "WEIRS"
    weir_type: Literal["TRANSVERSE", "SIDEFLOW", "V-NOTCH", "TRAPEZOIDAL", "ROADWAY"]
    crest_height: float | Offset | None = number("offset", None, markers=(Offset.NODE_INVERT,))
    coefficient: float = number("weir_coefficient", minimum=0)
    gated: bool | None = None
    end_contractions: float | None = number("count", None, minimum=0)
    end_coefficient: float | None = number("weir_coefficient", None, minimum=0)
    can_surcharge: bool | None = None
    road_width: float | None = number("length", None, minimum=0)
    road_surface: Literal["PAVED", "GRAVEL"] | None = None
    coefficient_curve: Ref | None = reference("swmm:curves", None)
    section: CrossSection | None = None


@dataclass(frozen=True, kw_only=True)
class OutletRating:
    basis: Literal["DEPTH", "HEAD"]


@dataclass(frozen=True, kw_only=True)
class FunctionalRating(OutletRating):
    kind: ClassVar[str] = "FUNCTIONAL"
    coefficient: float = number("outlet_coefficient")
    exponent: float = number("ratio")


@dataclass(frozen=True, kw_only=True)
class TabularRating(OutletRating):
    kind: ClassVar[str] = "TABULAR"
    curve: Ref = reference("swmm:curves")


@dataclass(frozen=True, kw_only=True)
class Outlet(Link):
    kind: ClassVar[str] = "OUTLETS"
    offset: float | Offset | None = number("offset", None, markers=(Offset.NODE_INVERT,))
    rating: OutletRating
    gated: bool | None = None


def network_collections() -> tuple[CollectionSpec, ...]:
    return (
        CollectionSpec(key="swmm:nodes", record_type=Node, key_of=lambda record: record.id,
                       identity_field="id", validate=validate_fields),
        CollectionSpec(key="swmm:links", record_type=Link, key_of=lambda record: record.id,
                       identity_field="id", validate=validate_fields),
    )
