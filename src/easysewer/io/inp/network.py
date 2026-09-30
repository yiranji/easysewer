"""Hydraulic network feature, assembled independently of section order.

All four node kinds and five link families share graph ownership and source
bindings; positional syntax is delegated to the relevant domain codecs.
"""

from dataclasses import fields, replace

from ...model import network as n
from ...model.identity import Ref, canonical_key
from ...model.fields import validate_fields
from ...model.store import RecordStore
from ...model.values import Offset, Point
from ...model.node_rules import NODE_UNIT_TRANSFORMS, validate_nodes
from ...model.link_rules import LINK_UNIT_TRANSFORMS, validate_links, link_resource_uses
from ...model.usage import ResourceUse
from ...model.geometry import Custom, Irregular, Street
from ...schema import DecodedFeature, FeatureDescriptor
from ...schema.structured import EncodedRow, FeatureData, RecordEntry, SourceBinding
from ...schema.network_fields import NETWORK_FIELD_RULES
from ...schema.geometry_fields import GEOMETRY_FIELD_RULES
from ...schema.regulator_fields import REGULATOR_FIELD_RULES
from ...schema.node_fields import NODE_FIELD_RULES
from ...schema.geometry_profile import standard_dimensions
from ...model.geometry import HorizontalEllipse, VerticalEllipse, Arch
from ...model.units import UnitContext
from ...validation import Diagnostic, DiagnosticSubject, Severity, SourceSpan, ValidationReport
from .geometry import CrossSectionCodec, UnsupportedGeometry, finite_number, number_text
from .formatting import optional_tail as _tail
from .nodes import parse_storage, format_storage, parse_divider, format_divider, node_field_layout
from .regulators import parse_regulator, format_regulator, parse_regulator_section, regulator_field_layout
from .field_sources import FieldSources
from ...validation._cooperative import checkpointed


def _boolean(token):
    if token.upper() not in ("YES", "NO"):
        raise ValueError(f"Expected YES or NO: {token}")
    return token.upper() == "YES"


def _numeric_tail(record, fields):
    return _tail(tuple(number_text(getattr(record, field)) if getattr(record, field) is not None else None
                       for field in fields))


def _row_key(section, identity, index=None):
    key = section, canonical_key(identity)
    return key if index is None else (*key, str(index))


class NetworkCodec:
    descriptor = FeatureDescriptor(key="swmm:network", sections=frozenset({
        "JUNCTIONS", "OUTFALLS", "STORAGE", "DIVIDERS", "CONDUITS", "PUMPS", "ORIFICES", "WEIRS", "OUTLETS",
        "XSECTIONS", "LOSSES", "COORDINATES", "VERTICES", "POLYGONS",
    }), ordered_sections=frozenset({"JUNCTIONS", "OUTFALLS", "STORAGE", "DIVIDERS", "CONDUITS", "PUMPS", "ORIFICES", "WEIRS", "OUTLETS"}))
    collections = n.network_collections()
    unit_transforms = NODE_UNIT_TRANSFORMS + LINK_UNIT_TRANSFORMS
    field_rules = NETWORK_FIELD_RULES + GEOMETRY_FIELD_RULES + REGULATOR_FIELD_RULES + NODE_FIELD_RULES

    def __init__(self, *, cross_sections=None):
        self.cross_sections = cross_sections or CrossSectionCodec()

    def decode(self, document, profile):
        store = RecordStore(self.collections)
        nodes, links = store.collection("swmm:nodes"), store.collection("swmm:links")
        bindings, issues = [], []
        source_fields = FieldSources()
        lexical_errors = {item.span.line for item in checkpointed(document.report.errors) if item.span}
        catchment_names = {canonical_key(line.values[0]) for line in checkpointed(document.records('SUBCATCHMENTS'))}
        storage_names = {canonical_key(line.values[0]) for line in checkpointed(document.records('STORAGE'))}

        def address(line):
            if not line.values:
                return
            namespace = 'swmm:nodes' if line.section in ('JUNCTIONS', 'OUTFALLS', 'STORAGE', 'DIVIDERS', 'COORDINATES', 'POLYGONS') else 'swmm:links'
            try:
                owner = Ref(collection=namespace, key=line.values[0])
            except (ValueError, TypeError):
                return
            path = {'COORDINATES': ('position',), 'POLYGONS': ('polygon',),
                    'VERTICES': ('vertices',), 'XSECTIONS': ('section',), 'LOSSES': ('losses',)}.get(line.section, ())
            return DiagnosticSubject(collection=owner.collection, key=owner.key, path=path)

        def uncertain(line):
            subject = address(line)
            if subject is not None:
                source_fields.block(Ref(collection=subject.collection, key=subject.key), subject.path)

        def bind(owner, path, line, tokens, *, role='value', overwrite=True):
            source_fields.cover(owner, path)
            source_fields.add(owner, path, line, tokens, role=role, overwrite=overwrite)

        def issue(line, error, unsupported=False):
            uncertain(line)
            issues.append(Diagnostic(code="inp.unsupported_variant" if unsupported else "inp.invalid_record",
                                     severity=Severity.WARNING if unsupported else Severity.ERROR,
                                     message=str(error), section=line.section,
                                     object_id=line.values[0] if line.values else None,
                                     subject=address(line),
                                     span=SourceSpan(source=document.source, line=line.number, column=1,
                                                     end_column=len(line.content) + 1)))

        def accept(line, operation):
            if line.number in lexical_errors:
                uncertain(line)
                return
            try:
                operation(line)
            except (ValueError, KeyError, TypeError) as error:
                issue(line, error, isinstance(error, UnsupportedGeometry))

        def entity(line):
            values = line.values
            section = line.section
            if section == "JUNCTIONS":
                if not 2 <= len(values) <= 6:
                    raise ValueError("Junction requires ID, elevation and up to four optional parameters")
                record = n.Junction(id=values[0], elevation=finite_number(values[1]), **dict(zip(
                    ("max_depth", "initial_depth", "surcharge_depth", "ponded_area"),
                    (finite_number(value) for value in checkpointed(values[2:])))))
                collection = nodes
            elif section == "OUTFALLS":
                if len(values) < 3:
                    raise ValueError("Outfall requires ID, elevation and boundary type")
                kind = values[2].upper()
                index = 3
                if kind == "FREE":
                    boundary = n.FreeBoundary()
                elif kind == "NORMAL":
                    boundary = n.NormalBoundary()
                elif kind in ("FIXED", "TIDAL", "TIMESERIES"):
                    if len(values) < 4:
                        raise ValueError(f"Missing {kind} boundary parameter")
                    index = 4
                    boundary = (n.FixedBoundary(stage=finite_number(values[3])) if kind == "FIXED" else
                                n.TidalBoundary(curve=Ref(collection="swmm:curves", key=values[3])) if kind == "TIDAL" else
                                n.SeriesBoundary(series=Ref(collection="swmm:timeseries", key=values[3])))
                else:
                    raise UnsupportedGeometry(f"Unsupported outfall boundary: {kind}")
                if len(values) > index + 2:
                    raise ValueError("Extra outfall fields")
                record = n.Outfall(id=values[0], elevation=finite_number(values[1]), boundary=boundary,
                                   gated=_boolean(values[index]) if len(values) > index else None,
                                   route_to=Ref(collection="swmm:subcatchments", key=values[index + 1])
                                   if len(values) > index + 1 else None)
                collection = nodes
            elif section == "STORAGE":
                record, ignored = parse_storage(values)
                for code, message in checkpointed(ignored):
                    issues.append(Diagnostic(code=code, message=message,
                                             severity=Severity.WARNING, section=section, object_id=record.id,
                                             span=SourceSpan(source=document.source, line=line.number, column=1,
                                                             end_column=len(line.content) + 1)))
                collection = nodes
            elif section == "DIVIDERS":
                record = parse_divider(values)
                collection = nodes
            elif section in ("PUMPS", "ORIFICES", "WEIRS", "OUTLETS"):
                record, ignored = parse_regulator(section, values)
                for code, message in checkpointed(ignored):
                    issues.append(Diagnostic(code=code, message=message, severity=Severity.WARNING,
                        section=section, object_id=record.id, span=SourceSpan(source=document.source,
                            line=line.number, column=1, end_column=len(line.content) + 1)))
                collection = links
            else:
                if not 7 <= len(values) <= 9:
                    raise ValueError("Conduit requires seven fields and up to two optional flows")
                record = n.Conduit(id=values[0], inlet=Ref(collection="swmm:nodes", key=values[1]),
                                   outlet=Ref(collection="swmm:nodes", key=values[2]),
                                   length=finite_number(values[3]), roughness=finite_number(values[4]),
                                   inlet_offset=Offset.NODE_INVERT if values[5] == "*" else finite_number(values[5]),
                                   outlet_offset=Offset.NODE_INVERT if values[6] == "*" else finite_number(values[6]),
                                   initial_flow=finite_number(values[7]) if len(values) > 7 else None,
                                   maximum_flow=finite_number(values[8]) if len(values) > 8 else None)
                collection = links
            ValidationReport(diagnostics=tuple(validate_fields(record))).raise_for_errors()
            collection.add(record)
            bindings.append(SourceBinding(line=line.number, key=_row_key(section, record.id)))
            if type(record) in (n.Junction, n.Conduit):
                owner = Ref(collection='swmm:nodes' if type(record) is n.Junction else 'swmm:links', key=record.id)
                source_fields.cover(owner, *((f.name,) for f in checkpointed(fields(record))))
                names = (('id', 'elevation', 'max_depth', 'initial_depth', 'surcharge_depth', 'ponded_area')
                         if type(record) is n.Junction else
                         ('id', 'inlet', 'outlet', 'length', 'roughness', 'inlet_offset', 'outlet_offset', 'initial_flow', 'maximum_flow'))
                for index, name in checkpointed(enumerate(names[:len(values)])):
                    bind(owner, (name,), line, (index,), role='marker' if values[index] == '*' else 'value')
                    if name in ('inlet', 'outlet'):
                        bind(owner, (name, 'key'), line, (index,))
                        bind(owner, (name, 'collection'), line, (index,), role='derived')
            elif type(record) in (n.Pump, n.Orifice, n.Weir, n.Outlet):
                owner = Ref(collection='swmm:links', key=record.id)
                source_fields.cover(owner, *((f.name,) for f in checkpointed(fields(record))))
                coverage, assignments = regulator_field_layout(record, values)
                source_fields.cover(owner, *coverage)
                for path, indexes, role, contributes in checkpointed(assignments):
                    source_fields.add(owner, path, line, indexes, role=role, contributes=contributes)
            elif type(record) in (n.Storage, n.Divider, n.Outfall):
                owner = Ref(collection='swmm:nodes', key=record.id)
                source_fields.cover(owner, *((f.name,) for f in checkpointed(fields(record))))
                coverage, assignments = node_field_layout(record, values)
                source_fields.cover(owner, *coverage)
                for path, indexes, role, contributes in checkpointed(assignments):
                    source_fields.add(owner, path, line, indexes, role=role, contributes=contributes)

        def related(line):
            values = line.values
            section = line.section
            if not values:
                raise ValueError("Empty relation record")
            collection = nodes if section in ("COORDINATES", "POLYGONS") else links
            try:
                record = collection[values[0]]
            except KeyError:
                raise UnsupportedGeometry(f"Owner {values[0]!r} is absent or not yet structured") from None
            key = _row_key(section, record.id)
            if section == "XSECTIONS":
                if not isinstance(record, (n.Conduit, n.Orifice, n.Weir)):
                    raise UnsupportedGeometry("Cross-sections are supported for conduits, orifices and weirs")
                if record.section is not None:
                    raise ValueError("Duplicate cross-section")
                if isinstance(record, (n.Orifice, n.Weir)):
                    section_value, ignored = parse_regulator_section(values[1:], self.cross_sections)
                    if ignored:
                        issues.append(Diagnostic(code="regulator.ignored_section_fields", severity=Severity.WARNING,
                            message="Native ignores unused geometry slots, barrels and culvert fields for this regulator; original tokens remain in source",
                            section=section, object_id=record.id, span=SourceSpan(source=document.source,
                                line=line.number, column=1, end_column=len(line.content) + 1)))
                else:
                    section_value = self.cross_sections.parse(values[1:])
                record = replace(record, section=section_value)
            elif section == "LOSSES":
                if not isinstance(record, n.Conduit):
                    raise UnsupportedGeometry("Non-conduit LOSSES records remain source-owned")
                if not 4 <= len(values) <= 6 or record.losses is not None:
                    raise ValueError("Invalid or duplicate conduit losses")
                record = replace(record, losses=n.ConduitLosses(
                    entry=finite_number(values[1]), exit=finite_number(values[2]), average=finite_number(values[3]),
                    flap_gate=_boolean(values[4]) if len(values) > 4 else None,
                    seepage=finite_number(values[5]) if len(values) > 5 else None))
            else:
                if len(values) < 3:
                    raise ValueError("Coordinate record requires ID, X and Y")
                point = Point(x=finite_number(values[1]), y=finite_number(values[2]))
                if len(values) > 3:
                    issues.append(Diagnostic(code='geometry.ignored_columns', severity=Severity.WARNING,
                        message='GUI ignores trailing coordinate fields; normalization removes them',
                        section=section, object_id=record.id,
                        span=SourceSpan(source=document.source, line=line.number, column=1, end_column=len(line.content)+1)))
                if section == "COORDINATES":
                    if record.position is not None:
                        issues.append(Diagnostic(code='geometry.repeated_assignment', severity=Severity.INFO,
                            message='Later node coordinates take effect', section=section, object_id=record.id,
                            span=SourceSpan(source=document.source, line=line.number, column=1, end_column=len(line.content)+1)))
                    record = replace(record, position=point)
                elif section == 'POLYGONS':
                    if not isinstance(record, n.Storage):
                        raise UnsupportedGeometry('Only storage nodes can own node polygons')
                    key = _row_key(section, record.id, len(record.polygon))
                    record = replace(record, polygon=(*record.polygon, point))
                else:
                    key = _row_key(section, record.id, len(record.vertices))
                    record = replace(record, vertices=(*record.vertices, point))
            ValidationReport(diagnostics=tuple(validate_fields(record))).raise_for_errors()
            collection.replace(record.id, record)
            bindings.append(SourceBinding(line=line.number, key=key))
            owner = Ref(collection='swmm:nodes' if section in ('COORDINATES', 'POLYGONS') else 'swmm:links', key=record.id)
            if section == 'COORDINATES':
                bind(owner, ('position',), line, (1, 2))
                for index, axis in checkpointed(enumerate(('x', 'y'), 1)):
                    bind(owner, ('position', axis), line, (index,))
            elif section in ('VERTICES', 'POLYGONS'):
                name = 'vertices' if section == 'VERTICES' else 'polygon'
                index = len(getattr(record, name)) - 1
                bind(owner, (name,), line, (1, 2), overwrite=False)
                bind(owner, (name, index), line, (1, 2))
                for col, axis in checkpointed(enumerate(('x', 'y'), 1)):
                    bind(owner, (name, index, axis), line, (col,))
            elif section == 'LOSSES':
                bind(owner, ('losses',), line, range(1, len(values)))
                source_fields.cover(owner, *((('losses', f.name)) for f in checkpointed(fields(n.ConduitLosses))))
                for index, name in checkpointed(enumerate(('entry', 'exit', 'average', 'flap_gate', 'seepage')[:len(values)-1], 1)):
                    bind(owner, ('losses', name), line, (index,))
            elif section == 'XSECTIONS':
                bind(owner, ('section',), line, range(1, len(values)))
                coverage, assignments = self.cross_sections.field_layout(values[1:],
                    regulator=isinstance(record, (n.Orifice, n.Weir)))
                source_fields.cover(owner, *(('section', *path) for path in checkpointed(coverage)))
                for path, indexes, role, contributes in checkpointed(assignments):
                    source_fields.add(owner, ('section', *path), line,
                        (index + 1 for index in checkpointed(indexes)), role=role, contributes=contributes)

        # No relation assumes its owner appeared earlier in the input file.
        for line in checkpointed(document.lines):
            if line.kind == "data" and line.section in self.descriptor.ordered_sections:
                accept(line, entity)
        for line in checkpointed(document.lines):
            if line.kind == "data":
                if line.section in {"XSECTIONS", "LOSSES", "COORDINATES", "VERTICES"}:
                    accept(line, related)
                elif (line.section == 'POLYGONS' and canonical_key(line.values[0]) in storage_names
                      and canonical_key(line.values[0]) not in catchment_names):
                    accept(line, related)
        records = tuple(RecordEntry(collection=spec.key, value=value)
                        for spec in checkpointed(self.collections) for value in checkpointed(store.collection(spec.key).values()))
        original_records = {Ref(collection=r.collection, key=r.value.id).canonical: r.value for r in checkpointed(records)}
        return DecodedFeature(value=FeatureData(records=records, bindings=tuple(bindings),
                                               **source_fields.finish(original_records)),
                              claimed_lines=frozenset(item.line for item in checkpointed(bindings)),
                              report=ValidationReport(diagnostics=tuple(issues)))

    def encode(self, store, profile):
        def emit(section, identity, values, index=None):
            namespace = "swmm:nodes" if section in ("JUNCTIONS", "OUTFALLS", "STORAGE", "DIVIDERS", "COORDINATES", "POLYGONS") else "swmm:links"
            return EncodedRow(key=_row_key(section, identity, index), section=section, values=(identity, *values),
                              owners=(Ref(collection=namespace, key=identity),))

        for record in checkpointed(store.collection("swmm:nodes").values()):
            if type(record) is n.Junction:
                values = (number_text(record.elevation), *_numeric_tail(record, (
                    "max_depth", "initial_depth", "surcharge_depth", "ponded_area")))
            elif type(record) is n.Outfall:
                boundary = record.boundary
                if type(boundary) in (n.FreeBoundary, n.NormalBoundary):
                    parameters = ()
                elif type(boundary) is n.FixedBoundary:
                    parameters = (number_text(boundary.stage),)
                elif type(boundary) is n.TidalBoundary:
                    parameters = (boundary.curve.key,)
                elif type(boundary) is n.SeriesBoundary:
                    parameters = (boundary.series.key,)
                else:
                    raise ValueError(f"No INP writer for {type(boundary).__name__}")
                values = (number_text(record.elevation), boundary.kind, *parameters,
                          *_tail((None if record.gated is None else "YES" if record.gated else "NO",
                                  record.route_to.key if record.route_to else None), ("NO", "*")))
            elif type(record) is n.Storage:
                values = format_storage(record)
            elif type(record) is n.Divider:
                values = format_divider(record)
            else:
                raise ValueError(f"No INP writer for {type(record).__name__}")
            yield emit(record.kind, record.id, values)
        for record in checkpointed(store.collection("swmm:nodes").values()):
            if record.position is not None:
                yield emit("COORDINATES", record.id, (number_text(record.position.x), number_text(record.position.y)))
            if isinstance(record, n.Storage):
                for index, point in checkpointed(enumerate(record.polygon)):
                    yield emit('POLYGONS', record.id, (number_text(point.x), number_text(point.y)), index)
        for record in checkpointed(store.collection("swmm:links").values()):
            if type(record) is not n.Conduit:
                yield emit(record.kind, record.id, format_regulator(record))
                continue
            offsets = tuple("*" if value is Offset.NODE_INVERT else number_text(value if value is not None else 0)
                            for value in checkpointed((record.inlet_offset, record.outlet_offset)))
            yield emit("CONDUITS", record.id, (record.inlet.key, record.outlet.key,
                       number_text(record.length), number_text(record.roughness), *offsets,
                       *_numeric_tail(record, ("initial_flow", "maximum_flow"))))
        for record in checkpointed(store.collection("swmm:links").values()):
            if isinstance(record, (n.Conduit, n.Orifice, n.Weir)) and record.section is not None:
                yield emit("XSECTIONS", record.id, self.cross_sections.format(record.section))
        for record in checkpointed(store.collection("swmm:links").values()):
            if isinstance(record, n.Conduit) and record.losses is not None:
                losses = record.losses
                yield emit("LOSSES", record.id, (number_text(losses.entry), number_text(losses.exit),
                           number_text(losses.average), *_tail((None if losses.flap_gate is None else
                           "YES" if losses.flap_gate else "NO", number_text(losses.seepage)
                           if losses.seepage is not None else None), ("NO", "0"))))
        for record in checkpointed(store.collection("swmm:links").values()):
            for index, point in checkpointed(enumerate(record.vertices)):
                yield emit("VERTICES", record.id, (number_text(point.x), number_text(point.y)), index)

    def validate(self, store, profile):
        yield from validate_nodes(store, profile)
        yield from validate_links(store, profile)
        for link in store.collection('swmm:links').values():
            section = getattr(link, 'section', None)
            if type(getattr(section, 'geometry', None)) in (HorizontalEllipse, VerticalEllipse, Arch):
                # Code bounds are profile-specific, not generic dataclass limits.
                fact = standard_dimensions(section.geometry, UnitContext(), profile)
                if fact.status == 'invalid':
                    yield Diagnostic(code='geometry.standard_size', section='XSECTIONS', object_id=link.id,
                                     field='section.geometry.size_code', message=fact.reason)
        catchments = next((store.collection(s.key) for s in store.specifications if s.key == 'swmm:subcatchments'), ())
        for record in store.collection('swmm:nodes').values():
            if isinstance(record, n.Storage) and record.polygon and record.id in catchments:
                yield Diagnostic(code='geometry.shadowed_storage_polygon', section='POLYGONS',
                    object_id=record.id, field='polygon',
                    message='A same-ID subcatchment takes precedence over a storage polygon in INP; rename one owner')

    def validate_run(self, store, profile):
        yield from validate_nodes(store, profile, for_run=True)
        yield from validate_links(store, profile, for_run=True)

    def validate_document(self, document, profile):
        for line in document.records("STORAGE"):
            if len(line.values) > 4 and line.values[4].upper() == "PARABOLOID":
                yield Diagnostic(code="storage.native_shape_spelling", section="STORAGE", object_id=line.values[0],
                                 message="Native SWMM requires PARABOLIC; normalize the documented PARABOLOID alias before running",
                                 span=SourceSpan(source=document.source, line=line.number, column=1, end_column=len(line.content) + 1))

    def resource_uses(self, store, profile):
        yield from link_resource_uses(store)
        for node in store.collection("swmm:nodes").values():
            if not ValidationReport(diagnostics=tuple(validate_fields(node))).is_valid:
                continue
            owner = Ref(collection="swmm:nodes", key=node.id)
            if isinstance(node, n.Outfall) and isinstance(node.boundary, n.TidalBoundary):
                yield ResourceUse(owner=owner, target=node.boundary.curve, path=("boundary", "curve"),
                                  role="tidal outfall", dimensions=("hours", "elevation"), accepted_kinds=("TIDAL",))
            elif isinstance(node, n.Outfall) and isinstance(node.boundary, n.SeriesBoundary):
                yield ResourceUse(owner=owner, target=node.boundary.series, path=("boundary", "series"),
                                  role="outfall stage", dimensions=("elevation",))
            elif isinstance(node, n.Storage) and isinstance(node.shape, n.TabularStorage):
                yield ResourceUse(owner=owner, target=node.shape.curve, path=("shape", "curve"),
                                  role="storage area", dimensions=("depth", "area"), accepted_kinds=("STORAGE",))
            elif isinstance(node, n.Divider) and isinstance(node.law, n.TabularDivider):
                yield ResourceUse(owner=owner, target=node.law.curve, path=("law", "curve"),
                                  role="divider flow", dimensions=("flow", "flow"), accepted_kinds=("DIVERSION",))
        for link in store.collection("swmm:links").values():
            if not ValidationReport(diagnostics=tuple(validate_fields(link))).is_valid:
                continue
            if isinstance(link, n.Conduit) and link.section is not None and isinstance(link.section.geometry, Custom):
                yield ResourceUse(owner=Ref(collection="swmm:links", key=link.id), target=link.section.geometry.curve,
                                  path=("section", "geometry", "curve"), role="custom cross-section",
                                  dimensions=("ratio", "ratio"), accepted_kinds=("SHAPE",))
            elif isinstance(link, n.Conduit) and link.section is not None and isinstance(link.section.geometry, (Irregular, Street)):
                name = "transect" if isinstance(link.section.geometry, Irregular) else "street"
                yield ResourceUse(owner=Ref(collection="swmm:links", key=link.id), target=getattr(link.section.geometry, name),
                                  path=("section", "geometry", name), role=f"{name} cross-section",
                                  accepted_kinds=("TRANSECT" if name == "transect" else "STREET",))


def default_schema():
    from ...schema.structured import ModelSchema
    from .options import OptionsCodec
    from .resources import ResourcesCodec
    from .transects import TransectsCodec
    from .surface import SurfaceCodec
    from .climate import ClimateCodec
    from .hydrology import HydrologyCodec
    from .inflows import InflowsCodec
    from .controls import ControlsCodec
    from .report import ReportCodec
    from .project import ProjectCodec, MapCodec, TagsCodec
    from .files import FilesCodec
    from .rdii import RdiiCodec
    from .quality import QualityCodec
    from .groundwater import GroundwaterCodec
    from .treatment import TreatmentCodec
    from .lid import LidCodec
    from .events import EventsCodec
    from .display import LabelsCodec, ProfilesCodec
    result = ModelSchema()
    options = OptionsCodec()
    result.register(options.descriptor, options)
    resources = ResourcesCodec()
    result.register(resources.descriptor, resources)
    for feature in (TransectsCodec(), SurfaceCodec(), HydrologyCodec(), ClimateCodec(), InflowsCodec(), ControlsCodec()):
        result.register(feature.descriptor, feature)
    codec = NetworkCodec()
    result.register(codec.descriptor, codec)
    for feature in (RdiiCodec(), QualityCodec(), GroundwaterCodec(), TreatmentCodec(), LidCodec(), ReportCodec(), ProjectCodec(), MapCodec(), FilesCodec(), EventsCodec(), TagsCodec(), LabelsCodec(), ProfilesCodec()):
        result.register(feature.descriptor, feature)
    from ..json.catalog import builtin_types
    result.register_json(*builtin_types())
    return result
