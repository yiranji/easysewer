"""Node-specific validation and formula conversions for the fixed SWMM profile."""

from dataclasses import replace
import math

from ..validation import Diagnostic, DiagnosticSubject, Severity, ValidationReport
from . import network as n
from .fields import validate_fields
from .identity import Ref
from .options import get_options
from .units import UnitContext, UnitTransform


def _functional_units(value, context):
    length, area = context.number(1, "length"), context.number(1, "area")
    try:
        coefficient = value.coefficient * area / length ** value.exponent
    except (OverflowError, ZeroDivisionError):
        coefficient = math.inf
    if not math.isfinite(coefficient):
        ValidationReport(diagnostics=(Diagnostic(code="units.coefficient_range",
            message="Converted storage coefficient exceeds finite numeric precision", field="coefficient"),)).raise_for_errors()
    return replace(value, coefficient=coefficient,
                   constant=context.number(value.constant, "area"))


def _divider_weir_units(value, context):
    # Unlike a link weir, this coefficient multiplies height**1.5 without a
    # separate crest width. Native dhMax remains in model units.
    return replace(value, minimum_flow=context.number(value.minimum_flow, "flow"),
                   height=context.number(value.height, "depth"),
                   coefficient=value.coefficient * context.number(1, "flow") / context.number(1, "length") ** 1.5)


NODE_UNIT_TRANSFORMS = (
    UnitTransform(value_type=n.FunctionalStorage, convert=_functional_units),
    UnitTransform(value_type=n.WeirDivider, convert=_divider_weir_units),
)


def validate_nodes(store, profile, *, for_run=False):
    options = get_options(store)
    if not ValidationReport(diagnostics=tuple(validate_fields(options))).is_valid:
        return
    routing = options.flow_routing or profile.option_default("flow_routing")
    units = UnitContext(flow_units=options.flow_units or profile.option_default("flow_units"))
    links = store.collection("swmm:links")
    valid_links = {key: link for key, link in links.items()
                   if ValidationReport(diagnostics=tuple(validate_fields(link))).is_valid}

    def issue(record, code, message, field=None, severity=Severity.ERROR):
        return Diagnostic(code=code, message=message, object_id=record.id, field=field,
                          section=record.kind, severity=severity,
                          subject=DiagnosticSubject(collection='swmm:nodes', key=record.id,
                              path=tuple(field.split('.')) if field else ()))

    # Build adjacency once, including malformed links so a bad neighbor never
    # becomes a falsely known crown. No per-node scan of the complete network.
    adjacent = {}
    if for_run:
        from ..schema.field_contracts import FieldContext
        from ..schema.network_fields import initial_depth_within_bound
        from ..validation._cooperative import checkpointed
        needs_depth = {Ref(collection='swmm:nodes', key=node.id).canonical
            for node in checkpointed(store.collection('swmm:nodes').values())
            if type(node) in (n.Junction, n.Divider, n.Storage)
            and type(node.initial_depth) in (int, float) and node.initial_depth > 0}
        if needs_depth:
            for link in checkpointed(links.values()):
                owners = {ref.canonical for ref in (link.inlet, link.outlet) if isinstance(ref, Ref)}
                for owner in owners & needs_depth:
                    adjacent.setdefault(owner, []).append(link)
    storage_index = -1
    for node_index, node in enumerate(store.collection("swmm:nodes").values()):
        if isinstance(node, n.Storage):
            storage_index += 1
        if not ValidationReport(diagnostics=tuple(validate_fields(node))).is_valid:
            continue
        if for_run and type(node) in (n.Junction, n.Divider, n.Storage) and node.initial_depth:
            owner = Ref(collection='swmm:nodes', key=node.id)
            context = FieldContext(owner=owner, path=('initial_depth',), record=node,
                container=node, field='initial_depth', value=node.initial_depth,
                store=store, profile=profile)
            mode = options.link_offsets or profile.option_default('link_offsets')
            bound = initial_depth_within_bound(context, mode, links=adjacent.get(owner.canonical, ()))
            if bound.status == 'known' and bound.value is False:
                yield issue(node, 'storage.initial_depth' if type(node) is n.Storage else 'node.initial_depth',
                    'Initial depth exceeds adjusted maximum plus surcharge in native internal feet', 'initial_depth')
        if isinstance(node, n.Divider):
            if routing == "DYNWAVE" and not for_run:
                yield issue(node, "divider.inactive_law", "Dynamic wave treats a divider as a junction; its diversion law is inactive",
                            "law", Severity.INFO)
            if not for_run:
                continue
            if node.diverted_link is None:
                yield issue(node, "divider.missing_link", "Native validation requires an attached diverted link", "diverted_link")
            elif store.contains(node.diverted_link):
                link = links[node.diverted_link.key]
                if not ValidationReport(diagnostics=tuple(validate_fields(link))).is_valid:
                    continue
                owner = Ref(collection="swmm:nodes", key=node.id).canonical
                if owner not in (link.inlet.canonical, link.outlet.canonical):
                    yield issue(node, "divider.unattached_link", "Diverted link must connect to its divider", "diverted_link")
                elif routing != "DYNWAVE" and link.inlet.canonical != owner:
                    yield issue(node, "divider.incoming_diversion", "Active division requires the diverted link to leave the node", "diverted_link")
            if routing != "DYNWAVE":
                outgoing = [link for link in valid_links.values() if link.inlet.canonical == Ref(collection="swmm:nodes", key=node.id).canonical]
                if len(outgoing) > 2:
                    yield issue(node, "divider.too_many_outlets", "Steady/kinematic routing allows at most two outgoing divider links")
                elif len(outgoing) != 2:
                    yield issue(node, "divider.outlet_count", "A complete diversion requires two outgoing links", severity=Severity.WARNING)
            if isinstance(node.law, n.WeirDivider):
                try:
                    maximum = node.law.coefficient * node.law.height ** 1.5
                except OverflowError:
                    maximum = math.inf
                if not math.isfinite(maximum) or node.law.minimum_flow > maximum:
                    yield issue(node, "divider.invalid_weir_range", "Minimum flow must not exceed the finite full-weir flow C * H**1.5", "law")
                elif node.law.minimum_flow == maximum:
                    yield issue(node, "divider.zero_weir_range", "Equal minimum/full-weir flows eliminate the intermediate weir range", "law", Severity.WARNING)
        elif isinstance(node, n.Storage):
            shape = node.shape
            if not for_run:
                newton = isinstance(shape, (n.ConicalStorage, n.PyramidalStorage)) or (
                    isinstance(shape, n.FunctionalStorage) and shape.exponent != 0 and shape.constant != 0
                    and not (shape.exponent == 1 and shape.coefficient > 0))
                if routing != "DYNWAVE" and newton and node_index != storage_index:
                    yield issue(node, "storage.native_index_sensitive", "The fixed native nonlinear storage inversion uses a storage index "
                                "as a node index; this layout's result can depend on declaration order. Export preserves that order",
                                "shape", Severity.WARNING)
                if units.system == "SI" and profile.unit_rules is not None:
                    length = UnitContext().convert(1, dimension="length", to=units, rules=profile.unit_rules)
                    volume = UnitContext().convert(1, dimension="volume", to=units, rules=profile.unit_rules)
                    if volume != length ** 3:
                        yield issue(node, "storage.native_volume_factor", "Native volume and cubic-length factors differ; "
                                    "dimensionally converted storage does not guarantee identical hydraulic results across US/SI",
                                    "shape", Severity.INFO)
                if units.system == "SI" and node.seepage is not None and node.seepage.conductivity > 0 and not isinstance(shape, n.TabularStorage):
                    yield issue(node, "storage.native_seepage_area", "Native analytical-storage seepage uses an unconverted bottom area in SI; "
                                "compare seepage results before changing unit systems", "seepage", Severity.WARNING)
                continue
            if node.max_depth == 0 and not node.surcharge_depth:
                yield issue(node, "storage.zero_depth", "Open storage requires a positive full depth", "max_depth")
            if isinstance(shape, n.FunctionalStorage):
                if shape.exponent < 0:
                    yield issue(node, "storage.singular_function", "A negative exponent is singular at zero depth in native area evaluation", "shape.exponent")
                else:
                    try:
                        area = shape.constant + shape.coefficient * node.max_depth ** shape.exponent
                        volume = shape.constant * node.max_depth + shape.coefficient / (shape.exponent + 1) * node.max_depth ** (shape.exponent + 1)
                        bottom = shape.constant + (shape.coefficient if shape.exponent == 0 else 0)
                    except OverflowError:
                        area = volume = bottom = math.inf
                    if not all(math.isfinite(value) and value >= 0 for value in (area, volume, bottom)) or (node.max_depth > 0 and volume == 0):
                        yield issue(node, "storage.invalid_function", "Storage must have finite nonnegative areas and positive full volume", "shape")
            elif isinstance(shape, n.TabularStorage) and store.contains(shape.curve):
                curve = store.collection("swmm:curves")[shape.curve.key]
                if not ValidationReport(diagnostics=tuple(validate_fields(curve))).is_valid:
                    continue
                if any(point.x < 0 or point.y < 0 for point in curve.points):
                    yield issue(node, "storage.invalid_curve", "Storage depths and areas must be nonnegative", "shape.curve")
                if curve.points and (all(point.y == 0 for point in curve.points) or
                                     (len(curve.points) == 1 and node.max_depth > curve.points[0].x)):
                    yield issue(node, "storage.zero_curve_volume", "Storage curve gives zero native full volume", "shape.curve")
            if isinstance(shape, n.ParaboloidStorage) and node.seepage is not None and node.seepage.conductivity > 0:
                yield issue(node, "storage.native_paraboloid_seepage", "The fixed native exfiltration initializer does not initialize "
                            "PARABOLOID seepage areas; this combination cannot be run reliably", "seepage")
