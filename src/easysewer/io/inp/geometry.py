"""SWMM 5.2.4 XSECTIONS positional syntax, separate from domain geometry.

See SWMM 5.2 manual Appendix D (XSECTIONS), and the tagged native readers:
https://github.com/USEPA/Stormwater-Management-Model/blob/v5.2.4/src/solver/link.c
https://github.com/USEPA/Stormwater-Management-Model/blob/v5.2.4/src/solver/xsect.c
"""

from dataclasses import dataclass
import math
import re

from ...model import geometry as g
from ...model.fields import validate_fields
from ...model.identity import Ref
from ...validation import ValidationReport


class UnsupportedGeometry(ValueError):
    """Keep the entire source record opaque when its shape is not registered."""


_NUMBER_TOKEN = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\Z")


def finite_number(token: str) -> float:
    if not isinstance(token, str) or not _NUMBER_TOKEN.fullmatch(token):
        raise ValueError(f"Invalid numeric token: {token!r}")
    try:
        value = float(token)
    except (TypeError, ValueError):
        raise ValueError(f"Invalid number: {token!r}") from None
    if not math.isfinite(value):
        raise ValueError(f"Non-finite number: {token!r}")
    return value


def integer(token: str) -> int:
    value = finite_number(token)
    if value != int(value):
        raise ValueError(f"Expected an integer: {token!r}")
    return int(value)


def number_text(value: float) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("Expected a finite number")
    return str(value) if isinstance(value, int) else repr(value)


@dataclass(frozen=True, kw_only=True)
class GeometrySyntax:
    """Explicit field order; adding a shape does not change CrossSection."""
    kind: str
    record_type: type[g.Geometry]
    fields: tuple[str, ...]
    integer_fields: frozenset[str] = frozenset()


_NUMERIC = (
    GeometrySyntax(kind="CIRCULAR", record_type=g.Circular, fields=("diameter",)),
    GeometrySyntax(kind="FORCE_MAIN", record_type=g.ForceMain, fields=("diameter", "roughness")),
    GeometrySyntax(kind="FILLED_CIRCULAR", record_type=g.FilledCircular, fields=("diameter", "filled_depth")),
    GeometrySyntax(kind="RECT_CLOSED", record_type=g.RectClosed, fields=("full_depth", "width")),
    GeometrySyntax(kind="RECT_OPEN", record_type=g.RectOpen, fields=("full_depth", "width", "ignored_sides")),
    GeometrySyntax(kind="TRAPEZOIDAL", record_type=g.Trapezoidal,
                   fields=("full_depth", "bottom_width", "left_slope", "right_slope")),
    GeometrySyntax(kind="TRIANGULAR", record_type=g.Triangular, fields=("full_depth", "top_width")),
    GeometrySyntax(kind="PARABOLIC", record_type=g.Parabolic, fields=("full_depth", "top_width")),
    GeometrySyntax(kind="POWER", record_type=g.Power, fields=("full_depth", "top_width", "exponent")),
    GeometrySyntax(kind="RECT_TRIANGULAR", record_type=g.RectTriangular,
                   fields=("full_depth", "top_width", "triangle_depth")),
    GeometrySyntax(kind="RECT_ROUND", record_type=g.RectRound, fields=("full_depth", "top_width", "bottom_radius")),
    GeometrySyntax(kind="MODBASKETHANDLE", record_type=g.ModifiedBasketHandle,
                   fields=("full_depth", "bottom_width", "top_radius")),
    *(GeometrySyntax(kind=kind.kind, record_type=kind, fields=("full_depth",))
      for kind in (g.Egg, g.Horseshoe, g.Gothic, g.Catenary, g.SemiElliptical, g.BasketHandle, g.SemiCircular)),
    GeometrySyntax(kind="DUMMY", record_type=g.Dummy, fields=()),
)
_STANDARD = {kind.kind: kind for kind in (g.HorizontalEllipse, g.VerticalEllipse, g.Arch)}
_REFERENCE = {"IRREGULAR": (g.Irregular, "transect", "swmm:transects"),
              "STREET": (g.Street, "street", "swmm:streets")}


class CrossSectionCodec:
    """Pure record codec. The owning feature provides IDs and source locations."""

    def __init__(self, additional: tuple[GeometrySyntax, ...] = ()):
        self._numeric = {syntax.kind: syntax for syntax in _NUMERIC}
        for syntax in additional:
            if syntax.kind in self.kinds or syntax.kind != syntax.kind.upper() or len(syntax.fields) > 4:
                raise ValueError(f"Invalid or duplicate geometry syntax: {syntax.kind}")
            self._numeric[syntax.kind] = syntax

    @property
    def kinds(self) -> frozenset[str]:
        return frozenset(self._numeric) | frozenset(_STANDARD) | frozenset(_REFERENCE) | {"CUSTOM"}

    def field_layout(self, values, *, regulator=False):
        """Explicit syntax paths and token indexes, relative to Shape (no ID).

        Called only after successful parsing. No dataclass field order is used
        to infer positional syntax, including for registered extension shapes.
        """
        kind = values[0].upper()
        coverage = [('geometry',), ('barrels',), ('culvert',)]
        assignments = []

        def add(path, indexes, role='value', contributes=True):
            coverage.append(path)
            assignments.append((path, tuple(indexes), role, contributes))

        if kind in _REFERENCE:
            _, field, _ = _REFERENCE[kind]
            add(('geometry', field), (1,))
            add(('geometry', field, 'key'), (1,))
            add(('geometry', field, 'collection'), (1,), 'derived')
            active = (0, 1)
        elif kind in _STANDARD:
            code_index = 1 if finite_number(values[2]) == 0 else 3
            if finite_number(values[code_index]) > 0:
                add(('geometry', 'size_code'), (code_index,))
                for field, index in (('full_depth', 1), ('width', 2)):
                    # The model stores None for dimensions selected by a code.
                    add(('geometry', field), (code_index,), 'derived')
                    if code_index == 3:
                        add(('geometry', field), (index,), 'retained', False)
                active = (0, code_index)
            else:
                add(('geometry', 'full_depth'), (1,))
                add(('geometry', 'width'), (2,))
                add(('geometry', 'size_code'), (3,), 'marker')
                active = (0, 1, 2, 3)
        elif kind == 'CUSTOM':
            add(('geometry', 'full_depth'), (1,))
            add(('geometry', 'curve'), (2,))
            add(('geometry', 'curve', 'key'), (2,))
            add(('geometry', 'curve', 'collection'), (2,), 'derived')
            active = (0, 1, 2)
        else:
            active = [0]
            for index, field in enumerate(self._numeric[kind].fields, 1):
                ignored = regulator and kind == 'RECT_OPEN' and field == 'ignored_sides'
                add(('geometry', field), (index,), 'retained' if ignored else 'value', not ignored)
                if not ignored:
                    active.append(index)
        add(('geometry',), active)
        for index, field in ((5, 'barrels'), (6, 'culvert')):
            if len(values) > index:
                add((field,), (index,), 'retained' if regulator else 'value', not regulator)
        return tuple(coverage), tuple(assignments)

    def parse(self, values: tuple[str, ...]) -> g.CrossSection:
        """Parse Shape Geom1 ... (without the owning link ID)."""
        if not values:
            raise ValueError("Missing cross-section shape")
        kind = values[0].upper()
        if kind not in self.kinds:
            raise UnsupportedGeometry(f"Unsupported cross-section shape: {values[0]}")
        if kind in _REFERENCE:
            if len(values) != 2:
                raise ValueError("Reference cross-sections require exactly one resource ID")
            cls, field, namespace = _REFERENCE[kind]
            result = g.CrossSection(geometry=cls(**{field: Ref(collection=namespace, key=values[1])}))
        else:
            if not 5 <= len(values) <= 7:
                raise ValueError("Cross-section requires four geometry parameters and optional barrels/culvert")
            parameters = values[1:5]
            if kind == "CUSTOM":
                shape = g.Custom(full_depth=finite_number(parameters[0]),
                                 curve=Ref(collection="swmm:curves", key=parameters[1]))
            elif kind in _STANDARD:
                depth, width, code, _ = (finite_number(item) for item in parameters)
                code = depth if width == 0 else code
                if code > 0:
                    shape = _STANDARD[kind](size_code=integer(number_text(code)))
                else:
                    shape = _STANDARD[kind](full_depth=depth, width=width)
            else:
                syntax = self._numeric[kind]
                # Validate even unused numeric parameters before claiming a row.
                tuple(finite_number(item) for item in parameters)
                shape = syntax.record_type(**{
                    field: (integer(token) if field in syntax.integer_fields else finite_number(token))
                    for field, token in zip(syntax.fields, parameters)
                })
            result = g.CrossSection(geometry=shape,
                                    barrels=integer(values[5]) if len(values) >= 6 else None,
                                    culvert=integer(values[6]) if len(values) >= 7 else None)
        ValidationReport(diagnostics=tuple(validate_fields(result))).raise_for_errors()
        return result

    def format(self, section: g.CrossSection) -> tuple[str, ...]:
        ValidationReport(diagnostics=tuple(validate_fields(section))).raise_for_errors()
        shape = section.geometry
        kind = shape.kind
        if kind in _REFERENCE:
            cls, field, _ = _REFERENCE[kind]
            if type(shape) is not cls:
                raise UnsupportedGeometry(f"No syntax registered for {type(shape).__name__}")
            if section.barrels is not None or section.culvert is not None:
                raise ValueError("Barrels/culvert are not read for reference cross-sections")
            return kind, getattr(shape, field).key
        if kind == "CUSTOM" and type(shape) is g.Custom:
            parameters = [number_text(shape.full_depth), shape.curve.key]
        elif kind in _STANDARD and type(shape) is _STANDARD[kind]:
            # This native shorthand puts the standard size code in Geom1.
            parameters = ([str(shape.size_code), "0", "0"] if shape.size_code is not None
                          else [number_text(shape.full_depth), number_text(shape.width), "0"])
        elif kind in self._numeric and type(shape) is self._numeric[kind].record_type:
            parameters = [number_text(getattr(shape, field) if getattr(shape, field) is not None else 0)
                          for field in self._numeric[kind].fields]
        else:
            raise UnsupportedGeometry(f"No syntax registered for {type(shape).__name__}")
        parameters += ["0"] * (4 - len(parameters))
        if section.barrels is not None or section.culvert is not None:
            parameters.append(str(section.barrels if section.barrels is not None else 1))
        if section.culvert is not None:
            parameters.append(str(section.culvert))
        return (kind, *parameters)
