"""Hydraulic link rules and formula conversions for the pinned profile."""

from dataclasses import replace
import math

from ..validation import Diagnostic, DiagnosticSubject, Severity, ValidationReport
from . import network as n
from .fields import validate_fields
from .identity import Ref
from .options import get_options
from .resources import CURVE_DIMENSIONS
from .units import UnitTransform
from .usage import ResourceUse
from .values import Offset


def _outlet_units(value, context):
    try:
        coefficient = value.coefficient * context.number(1, "flow") / context.number(1, "length") ** value.exponent
    except (OverflowError, ZeroDivisionError):
        coefficient = math.inf
    if not math.isfinite(coefficient):
        ValidationReport(diagnostics=(Diagnostic(code="units.coefficient_range", message="Converted outlet coefficient exceeds finite numeric precision",
                                                field="coefficient"),)).raise_for_errors()
    return replace(value, coefficient=coefficient)


def _weir_units(value, context):
    coefficient = "roadway_coefficient" if value.weir_type == "ROADWAY" else "weir_coefficient"
    offset = value.crest_height
    return replace(value, coefficient=context.number(value.coefficient, coefficient),
        end_coefficient=None if value.end_coefficient is None else context.number(value.end_coefficient, "weir_coefficient"),
        crest_height=offset if offset is None or offset is Offset.NODE_INVERT else context.number(offset, "length"),
        road_width=None if value.road_width is None else context.number(value.road_width, "length"),
        section=context.convert_value(value.section, path=('section',)))


LINK_UNIT_TRANSFORMS = (
    UnitTransform(value_type=n.FunctionalRating, convert=_outlet_units),
    UnitTransform(value_type=n.Weir, convert=_weir_units),
)


def offset_fields(link):
    if isinstance(link, n.Conduit):
        return ("inlet_offset", "outlet_offset")
    if isinstance(link, (n.Orifice, n.Outlet)):
        return ("offset",)
    if isinstance(link, n.Weir):
        return ("crest_height",)
    return ()


def link_resource_uses(store):
    for link in store.collection("swmm:links").values():
        if not ValidationReport(diagnostics=tuple(validate_fields(link))).is_valid:
            continue
        owner = Ref(collection="swmm:links", key=link.id)
        if isinstance(link, n.Pump) and link.curve is not None:
            dimensions = ()
            if store.contains(link.curve):
                dimensions = CURVE_DIMENSIONS.get(store.collection("swmm:curves")[link.curve.key].kind, ())
            yield ResourceUse(owner=owner, target=link.curve, path=("curve",), role="pump characteristic",
                              dimensions=dimensions, accepted_kinds=("PUMP1", "PUMP2", "PUMP3", "PUMP4", "PUMP5"))
        elif isinstance(link, n.Outlet) and isinstance(link.rating, n.TabularRating):
            yield ResourceUse(owner=owner, target=link.rating.curve, path=("rating", "curve"), role="outlet rating",
                              dimensions=("depth", "flow"), accepted_kinds=("RATING",))
        elif isinstance(link, n.Weir) and link.coefficient_curve is not None:
            yield ResourceUse(owner=owner, target=link.coefficient_curve, path=("coefficient_curve",), role="weir coefficient",
                              dimensions=("depth", "weir_coefficient"), accepted_kinds=("WEIR",))


def validate_links(store, profile, *, for_run=False):
    options = get_options(store)
    if not ValidationReport(diagnostics=tuple(validate_fields(options))).is_valid:
        return
    mode = options.link_offsets or profile.option_default("link_offsets")
    routing = options.flow_routing or profile.option_default("flow_routing")
    nodes = store.collection("swmm:nodes")
    links = tuple(link for link in store.collection("swmm:links").values()
                  if ValidationReport(diagnostics=tuple(validate_fields(link))).is_valid)

    def issue(link, code, message, field=None, severity=Severity.ERROR, *, path=None, related=()):
        return Diagnostic(code=code, message=message, object_id=link.id, section=link.kind, field=field, severity=severity,
            subject=DiagnosticSubject(collection='swmm:links', key=link.id,
                path=path if path is not None else tuple(field.split('.')) if field else ()), related=related)

    for link in links:
        if not for_run:
            if isinstance(link, (n.Conduit, n.Orifice, n.Weir)) and link.section is None:
                yield issue(link, "network.missing_cross_section", f"{type(link).__name__} requires a cross-section", "section")
            for field in offset_fields(link):
                value = getattr(link, field)
                if mode == "DEPTH" and value is Offset.NODE_INVERT:
                    yield issue(link, "network.invalid_offset_marker", "Node-invert '*' offsets require LINK_OFFSETS ELEVATION", field)
                elif mode == "ELEVATION" and value is None:
                    yield issue(link, "network.missing_absolute_offset", "Supply an absolute elevation or Offset.NODE_INVERT", field)
                elif mode == "DEPTH" and value is not None and value is not Offset.NODE_INVERT and value < 0:
                    yield issue(link, "network.negative_offset_clamped", "The engine clamps negative depth offsets to zero", field, Severity.WARNING)
            if isinstance(link, (n.Orifice, n.Weir)) and link.section is not None:
                allowed = ("CIRCULAR", "RECT_CLOSED") if isinstance(link, n.Orifice) else {
                    "TRANSVERSE": ("RECT_OPEN",), "SIDEFLOW": ("RECT_OPEN",), "ROADWAY": ("RECT_OPEN",),
                    "V-NOTCH": ("TRIANGULAR",), "TRAPEZOIDAL": ("TRAPEZOIDAL",)}[link.weir_type]
                if link.section.geometry.kind not in allowed:
                    yield issue(link, "regulator.invalid_shape", f"This regulator requires {allowed}", "section.geometry")
                if link.section.barrels is not None or link.section.culvert is not None or getattr(link.section.geometry, "ignored_sides", None):
                    yield issue(link, "regulator.ignored_section_fields", "Native regulators ignore barrels, culvert code and RECT_OPEN side exclusions",
                                "section", Severity.WARNING)
            if isinstance(link, n.Weir):
                if link.weir_type != "ROADWAY" and (link.road_width is not None or link.road_surface is not None):
                    yield issue(link, "weir.inapplicable_road_fields", "Road width and surface require a ROADWAY weir")
                if link.weir_type == "ROADWAY" and any((link.gated, link.end_contractions, link.end_coefficient,
                                                         link.can_surcharge, link.coefficient_curve)):
                    yield issue(link, "weir.ignored_roadway_parameters", "ROADWAY ignores gate, end contractions/coefficient, surcharge and coefficient curve",
                                severity=Severity.WARNING)
            continue

        if isinstance(link, n.Pump):
            if (link.startup_depth or 0) > 0 and link.startup_depth <= (link.shutoff_depth or 0):
                yield issue(link, "pump.invalid_depth_limits", "Positive startup depth must exceed shutoff depth", "startup_depth")
            if routing == "DYNWAVE" and link.curve is None and sum(item.inlet.canonical == link.inlet.canonical for item in links) > 1:
                yield issue(link, "pump.ideal_multiple_outlets", "An ideal pump must be the sole link leaving its inlet node")
        if isinstance(link, n.Outlet) and isinstance(link.rating, n.FunctionalRating):
            if link.rating.coefficient < 0 or link.rating.exponent < 0:
                yield issue(link, "outlet.negative_rating", "Negative coefficient/exponent is not a supported physical outlet rating", "rating")
        if isinstance(link, (n.Pump, n.Outlet, n.Weir)):
            ref = (link.curve if isinstance(link, n.Pump) else link.coefficient_curve if isinstance(link, n.Weir)
                   else link.rating.curve if isinstance(link.rating, n.TabularRating) else None)
            if ref is not None and store.contains(ref):
                curve = store.collection("swmm:curves")[ref.key]
                if ValidationReport(diagnostics=tuple(validate_fields(curve))).is_valid and any(p.x < 0 or p.y < 0 for p in curve.points):
                    yield issue(link, "regulator.negative_curve", "Hydraulic rating axes and coefficients must be nonnegative")
        if isinstance(link, (n.Orifice, n.Weir, n.Outlet)) and store.contains(link.inlet):
            inlet = nodes[link.inlet.key]
            if routing != "DYNWAVE" and not isinstance(inlet, n.Storage):
                yield issue(link, "regulator.requires_storage", "Steady/kinematic routing requires the regulator inlet to be a Storage",
                    'inlet', path=('inlet','key'), related=(
                        DiagnosticSubject(collection=link.inlet.collection, key=link.inlet.key),
                        DiagnosticSubject(collection='swmm:options', key='settings', path=('flow_routing',))))
            if not ValidationReport(diagnostics=tuple(validate_fields(inlet))).is_valid or not store.contains(link.outlet):
                continue
            outlet = nodes[link.outlet.key]
            if not ValidationReport(diagnostics=tuple(validate_fields(outlet))).is_valid:
                continue
            value = getattr(link, offset_fields(link)[0])
            if value is None and mode == "ELEVATION":
                continue
            depth = 0 if value is Offset.NODE_INVERT else max(0, (value or 0) - (inlet.elevation if mode == "ELEVATION" else 0))
            if inlet.elevation + depth < outlet.elevation:
                yield issue(link, "regulator.crest_below_downstream", "Crest lies below downstream invert; dynamic wave raises it automatically",
                            severity=Severity.WARNING)
