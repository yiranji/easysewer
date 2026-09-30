"""Typed cross-section variants. INP positional parameters live in codecs."""

from dataclasses import dataclass
from typing import ClassVar

from .fields import number, reference
from .identity import Ref
from ..validation import Diagnostic


@dataclass(frozen=True, kw_only=True)
class Geometry:
    kind: ClassVar[str] = "geometry"


@dataclass(frozen=True, kw_only=True)
class Circular(Geometry):
    kind: ClassVar[str] = "CIRCULAR"
    diameter: float = number("length", positive=True)


@dataclass(frozen=True, kw_only=True)
class ForceMain(Geometry):
    kind: ClassVar[str] = "FORCE_MAIN"
    diameter: float = number("length", positive=True)
    roughness: float = number("force_main_roughness", positive=True)


@dataclass(frozen=True, kw_only=True)
class FilledCircular(Geometry):
    kind: ClassVar[str] = "FILLED_CIRCULAR"
    diameter: float = number("length", positive=True)
    filled_depth: float = number("length", minimum=0)

    def validate_local(self):
        if self.filled_depth >= self.diameter:
            yield Diagnostic(code="geometry.invalid_fill", message="Filled depth must be less than diameter",
                             field="filled_depth")


@dataclass(frozen=True, kw_only=True)
class RectClosed(Geometry):
    kind: ClassVar[str] = "RECT_CLOSED"
    full_depth: float = number("length", positive=True)
    width: float = number("length", positive=True)


@dataclass(frozen=True, kw_only=True)
class RectOpen(Geometry):
    kind: ClassVar[str] = "RECT_OPEN"
    full_depth: float = number("length", positive=True)
    width: float = number("length", positive=True)
    ignored_sides: float | None = number("ratio", None, minimum=0, maximum=2)


@dataclass(frozen=True, kw_only=True)
class Trapezoidal(Geometry):
    kind: ClassVar[str] = "TRAPEZOIDAL"
    full_depth: float = number("length", positive=True)
    bottom_width: float = number("length", minimum=0)
    left_slope: float = number("run/rise", minimum=0)
    right_slope: float = number("run/rise", minimum=0)

    def validate_local(self):
        if self.bottom_width == self.left_slope == self.right_slope == 0:
            yield Diagnostic(code="geometry.zero_width", message="Trapezoid must have a positive top width")


@dataclass(frozen=True, kw_only=True)
class Triangular(Geometry):
    kind: ClassVar[str] = "TRIANGULAR"
    full_depth: float = number("length", positive=True)
    top_width: float = number("length", positive=True)


@dataclass(frozen=True, kw_only=True)
class StandardSize(Geometry):
    """Use either dimensions or a size code; the codec preserves that choice."""
    full_depth: float | None = number("length", None, positive=True)
    width: float | None = number("length", None, positive=True)
    size_code: int | None = number("code", None, minimum=1, integer=True)

    def validate_local(self):
        if self.size_code is None:
            if self.full_depth is None or self.width is None:
                yield Diagnostic(code="geometry.missing_size", message="Specify both depth and width, or a size code")
        elif self.full_depth is not None or self.width is not None:
            yield Diagnostic(code="geometry.ambiguous_size", message="Size code and dimensions are mutually exclusive")


@dataclass(frozen=True, kw_only=True)
class HorizontalEllipse(StandardSize):
    kind: ClassVar[str] = "HORIZ_ELLIPSE"


@dataclass(frozen=True, kw_only=True)
class VerticalEllipse(StandardSize):
    kind: ClassVar[str] = "VERT_ELLIPSE"


@dataclass(frozen=True, kw_only=True)
class Arch(StandardSize):
    kind: ClassVar[str] = "ARCH"


@dataclass(frozen=True, kw_only=True)
class Parabolic(Geometry):
    kind: ClassVar[str] = "PARABOLIC"
    full_depth: float = number("length", positive=True)
    top_width: float = number("length", positive=True)


@dataclass(frozen=True, kw_only=True)
class Power(Geometry):
    kind: ClassVar[str] = "POWER"
    full_depth: float = number("length", positive=True)
    top_width: float = number("length", positive=True)
    exponent: float = number("ratio", positive=True)


@dataclass(frozen=True, kw_only=True)
class RectTriangular(Geometry):
    kind: ClassVar[str] = "RECT_TRIANGULAR"
    full_depth: float = number("length", positive=True)
    top_width: float = number("length", positive=True)
    triangle_depth: float = number("length", positive=True)

    def validate_local(self):
        if self.triangle_depth > self.full_depth:
            yield Diagnostic(code="geometry.invalid_bottom", message="Triangle depth exceeds full depth",
                             field="triangle_depth")


@dataclass(frozen=True, kw_only=True)
class RectRound(Geometry):
    kind: ClassVar[str] = "RECT_ROUND"
    full_depth: float = number("length", positive=True)
    top_width: float = number("length", positive=True)
    bottom_radius: float = number("length", minimum=0)


@dataclass(frozen=True, kw_only=True)
class ModifiedBasketHandle(Geometry):
    kind: ClassVar[str] = "MODBASKETHANDLE"
    full_depth: float = number("length", positive=True)
    bottom_width: float = number("length", positive=True)
    top_radius: float = number("length", minimum=0)


@dataclass(frozen=True, kw_only=True)
class DepthShape(Geometry):
    full_depth: float = number("length", positive=True)


@dataclass(frozen=True, kw_only=True)
class Egg(DepthShape):
    kind: ClassVar[str] = "EGG"


@dataclass(frozen=True, kw_only=True)
class Horseshoe(DepthShape):
    kind: ClassVar[str] = "HORSESHOE"


@dataclass(frozen=True, kw_only=True)
class Gothic(DepthShape):
    kind: ClassVar[str] = "GOTHIC"


@dataclass(frozen=True, kw_only=True)
class Catenary(DepthShape):
    kind: ClassVar[str] = "CATENARY"


@dataclass(frozen=True, kw_only=True)
class SemiElliptical(DepthShape):
    kind: ClassVar[str] = "SEMIELLIPTICAL"


@dataclass(frozen=True, kw_only=True)
class BasketHandle(DepthShape):
    kind: ClassVar[str] = "BASKETHANDLE"


@dataclass(frozen=True, kw_only=True)
class SemiCircular(DepthShape):
    kind: ClassVar[str] = "SEMICIRCULAR"


@dataclass(frozen=True, kw_only=True)
class Custom(Geometry):
    kind: ClassVar[str] = "CUSTOM"
    full_depth: float = number("length", positive=True)
    curve: Ref = reference("swmm:curves")


@dataclass(frozen=True, kw_only=True)
class Irregular(Geometry):
    kind: ClassVar[str] = "IRREGULAR"
    transect: Ref = reference("swmm:transects")


@dataclass(frozen=True, kw_only=True)
class Street(Geometry):
    kind: ClassVar[str] = "STREET"
    street: Ref = reference("swmm:streets")


@dataclass(frozen=True, kw_only=True)
class Dummy(Geometry):
    kind: ClassVar[str] = "DUMMY"


@dataclass(frozen=True, kw_only=True)
class CrossSection:
    geometry: Geometry
    barrels: int | None = number("count", None, minimum=1, integer=True)
    culvert: int | None = number("code", None, minimum=0, integer=True)


BUILTIN_GEOMETRIES = (
    Circular, ForceMain, FilledCircular, RectClosed, RectOpen, Trapezoidal, Triangular,
    HorizontalEllipse, VerticalEllipse, Arch, Parabolic, Power, RectTriangular,
    RectRound, ModifiedBasketHandle, Egg, Horseshoe, Gothic, Catenary,
    SemiElliptical, BasketHandle, SemiCircular, Custom, Irregular, Street, Dummy,
)
