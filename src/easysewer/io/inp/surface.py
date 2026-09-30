"""Street cross-sections, inlet design variants and conduit-keyed placements."""

from collections import OrderedDict

from ...model import surface as s
from ...model.fields import validate_fields
from ...model.geometry import RectOpen, Street, Trapezoidal
from ...model.identity import Ref, canonical_key
from ...model.network import Conduit
from ...model.resources import CURVE_DIMENSIONS
from ...model.usage import ResourceUse
from ...schema.registry import DecodedFeature, FeatureDescriptor
from ...schema.structured import EncodedRow, FeatureData, RecordEntry, SourceBinding
from ...validation import Diagnostic, Severity, SourceSpan, ValidationReport
from .geometry import finite_number, integer, number_text
from .formatting import optional_tail as _tail
from .field_sources import FieldSources
from .surface_sources import surface_sources
from ...schema.surface_fields import SURFACE_FIELD_RULES
from ...validation._cooperative import checkpointed

_COLLECTIONS = {"STREETS": "swmm:streets", "INLETS": "swmm:inlets", "INLET_USAGE": "swmm:inlet_usage"}
_STREET_FIELDS = ("crown_width", "curb_height", "cross_slope", "road_roughness", "gutter_depression", "gutter_width",
                  "sides", "backing_width", "backing_slope", "backing_roughness")
_USAGE_FIELDS = ("count", "percent_clogged", "maximum_flow", "local_depression", "local_width", "placement")


def _key(section, identity, part="header"):
    return section, canonical_key(identity), part


class SurfaceCodec:
    descriptor = FeatureDescriptor(key="swmm:surface", sections=frozenset(_COLLECTIONS), atomic_write=True)
    collections = s.SURFACE_COLLECTIONS
    field_rules = SURFACE_FIELD_RULES

    def decode(self, document, profile):
        records, bindings, issues = [], [], []
        source_fields = FieldSources()
        lexical_errors = {issue.span.line for issue in checkpointed(document.report.errors) if issue.span}

        def issue(line, code, message, severity=Severity.ERROR):
            issues.append(Diagnostic(code=code, message=message, severity=severity, section=line.section,
                                     object_id=line.values[0] if line.values else None,
                                     span=SourceSpan(source=document.source, line=line.number, column=1,
                                                     end_column=len(line.content) + 1)))

        for section, namespace in checkpointed(_COLLECTIONS.items()):
            groups = OrderedDict()
            for line in checkpointed(document.records(section)):
                if line.values:
                    groups.setdefault(canonical_key(line.values[0]), []).append(line)
            for lines in checkpointed(groups.values()):
                line = lines[0]
                if any(item.number in lexical_errors for item in checkpointed(lines)):
                    continue
                try:
                    name = line.values[0]
                    if section == "STREETS":
                        if len(lines) != 1:
                            raise ValueError("Street definitions must have unique IDs")
                        if not 5 <= len(line.values) <= 11:
                            raise ValueError("Street requires four mandatory and up to six optional fields")
                        fields = {}
                        for index, token in checkpointed(enumerate(line.values[1:])):
                            if index >= 8 and not fields.get("backing_width"):
                                issue(line, "street.ignored_backing", "Native ignores backing slope/roughness when backing width is zero", Severity.WARNING)
                                try:
                                    fields[_STREET_FIELDS[index]] = finite_number(token)
                                except ValueError:
                                    pass  # Retained source token has no native numeric interpretation.
                                continue
                            fields[_STREET_FIELDS[index]] = integer(token) if index == 6 else finite_number(token)
                        record = s.StreetSection(id=name, **fields)
                    elif section == "INLET_USAGE":
                        for line in checkpointed(lines):
                            tokens = line.values
                            if not 3 <= len(tokens) <= 9:
                                raise ValueError("Inlet usage requires conduit, design, receiver and up to six optional parameters")
                            fields = {}
                            for index, token in checkpointed(enumerate(tokens[3:])):
                                fields[_USAGE_FIELDS[index]] = (integer(token) if index == 0 else token.upper()
                                                                if index == 5 else finite_number(token))
                            if fields.get("placement") == "AUTO":
                                fields["placement"] = "AUTOMATIC"
                            record = s.InletUsage(link=Ref(collection="swmm:links", key=tokens[0]),
                                inlet=Ref(collection="swmm:inlets", key=tokens[1]), node=Ref(collection="swmm:nodes", key=tokens[2]), **fields)
                            ValidationReport(diagnostics=tuple(validate_fields(record))).raise_for_errors()
                        if len(lines) > 1:
                            issue(line, "inlet.repeated_usage", "Later inlet usage assignment takes effect for this conduit", Severity.INFO)
                    else:
                        components = {}
                        for line in checkpointed(lines):
                            tokens = line.values
                            if len(tokens) < 3:
                                raise ValueError("Incomplete inlet design")
                            kind = tokens[1].upper()
                            if kind in ("GRATE", "DROP_GRATE"):
                                if not 5 <= len(tokens) <= 7:
                                    raise ValueError("Grate requires length, width, type and its generic parameters")
                                grate_type = tokens[4].upper()
                                if grate_type == "GENERIC":
                                    if len(tokens) < 6:
                                        raise ValueError("Generic grate requires an open-area fraction")
                                    grate = s.GenericGrate(open_fraction=finite_number(tokens[5]),
                                                          splash_velocity=finite_number(tokens[6]) if len(tokens) == 7 else None)
                                else:
                                    grate = s.StandardGrate(kind=grate_type)
                                    if len(tokens) > 5:
                                        issue(line, "inlet.ignored_grate_parameters", "Native uses predetermined standard-grate area and splash parameters", Severity.WARNING)
                                component = s.GrateInlet(kind=kind, length=finite_number(tokens[2]), width=finite_number(tokens[3]), grate=grate)
                            elif kind in ("CURB", "DROP_CURB"):
                                if not 4 <= len(tokens) <= 5:
                                    raise ValueError("Curb requires length and height, with optional street-curb throat")
                                throat = tokens[4].upper() if kind == "CURB" and len(tokens) == 5 else None
                                if kind == "DROP_CURB" and len(tokens) == 5:
                                    issue(line, "inlet.ignored_throat", "Native ignores DROP_CURB throat tokens", Severity.WARNING)
                                component = s.CurbInlet(kind=kind, length=finite_number(tokens[2]), height=finite_number(tokens[3]), throat=throat)
                            elif kind == "SLOTTED" and len(tokens) == 4:
                                component = s.SlottedInlet(length=finite_number(tokens[2]), width=finite_number(tokens[3]))
                            elif kind == "CUSTOM" and len(tokens) == 3:
                                component = s.CustomInlet(curve=Ref(collection="swmm:curves", key=tokens[2]))
                            else:
                                raise ValueError(f"Unsupported or malformed inlet type {kind}")
                            ValidationReport(diagnostics=tuple(validate_fields(component))).raise_for_errors()
                            if kind in components:
                                issue(line, "inlet.repeated_component", "Later assignment replaces this inlet component", Severity.INFO)
                            components[kind] = component
                        if set(components) == {"GRATE", "CURB"}:
                            design = s.CombinationInlet(grate=components["GRATE"], curb=components["CURB"])
                        elif len(components) == 1:
                            design = next(iter(components.values()))
                        else:
                            raise ValueError("Only a GRATE plus CURB forms a supported combination inlet")
                        record = s.InletDesign(id=name, design=design)
                    report = ValidationReport(diagnostics=tuple(validate_fields(record)))
                    report.raise_for_errors()
                    issues.extend(report.diagnostics)
                    records.append(RecordEntry(collection=namespace, value=record))
                    surface_sources(source_fields, record, lines, _STREET_FIELDS, _USAGE_FIELDS)
                    bindings.extend(SourceBinding(line=item.number, key=_key(section, name)) for item in checkpointed(lines))
                except (ValueError, TypeError, OverflowError) as error:
                    issue(line, "surface.invalid_input", str(error))
        originals = {Ref(collection=e.collection, key=e.value.link.key if type(e.value) is s.InletUsage else e.value.id).canonical: e.value for e in checkpointed(records)}
        return DecodedFeature(value=FeatureData(records=tuple(records), bindings=tuple(bindings), **source_fields.finish(originals)),
                              claimed_lines=frozenset(item.line for item in checkpointed(bindings)), report=ValidationReport(diagnostics=tuple(issues)))

    def encode(self, store, profile):
        def row(section, name, values, part="header"):
            return EncodedRow(key=_key(section, name, part), section=section, values=(name, *values),
                              owners=(Ref(collection=_COLLECTIONS[section], key=name),))
        for record in checkpointed(store.collection("swmm:streets").values()):
            values = tuple(None if getattr(record, name) is None else number_text(getattr(record, name)) for name in checkpointed(_STREET_FIELDS))
            yield row("STREETS", record.id, _tail(values, ("0", "0", "0", "0", "0", "0", "2", "0", "0", "0")))

        def design_tokens(design):
            if isinstance(design, s.GrateInlet):
                values = (design.kind, number_text(design.length), number_text(design.width), design.grate.kind)
                if isinstance(design.grate, s.GenericGrate):
                    values += (number_text(design.grate.open_fraction),)
                    if design.grate.splash_velocity is not None:
                        values += (number_text(design.grate.splash_velocity),)
                elif not isinstance(design.grate, s.StandardGrate):
                    raise ValueError(f"No grate writer for {type(design.grate).__name__}")
                return values
            if isinstance(design, s.CurbInlet):
                return (design.kind, number_text(design.length), number_text(design.height), *((design.throat,) if design.throat is not None else ()))
            if isinstance(design, s.SlottedInlet):
                return (design.kind, number_text(design.length), number_text(design.width))
            if isinstance(design, s.CustomInlet):
                return (design.kind, design.curve.key)
            raise ValueError(f"No inlet writer for {type(design).__name__}")

        for record in checkpointed(store.collection("swmm:inlets").values()):
            if isinstance(record.design, s.CombinationInlet):
                yield row("INLETS", record.id, design_tokens(record.design.grate))
                yield row("INLETS", record.id, design_tokens(record.design.curb), "curb")
            else:
                yield row("INLETS", record.id, design_tokens(record.design))
        for record in checkpointed(store.collection("swmm:inlet_usage").values()):
            values = tuple(None if getattr(record, name) is None else str(getattr(record, name)) if name == "placement"
                           else number_text(getattr(record, name)) for name in checkpointed(_USAGE_FIELDS))
            yield row("INLET_USAGE", record.link.key, (record.inlet.key, record.node.key,
                      *_tail(values, ("1", "0", "0", "0", "0", "AUTOMATIC"))))

    def validate(self, store, profile):
        for usage in store.collection("swmm:inlet_usage").values():
            if not ValidationReport(diagnostics=tuple(validate_fields(usage))).is_valid:
                continue
            if not store.contains(usage.link) or not store.contains(usage.inlet):
                continue
            link = store.collection("swmm:links")[usage.link.key]
            inlet = store.collection("swmm:inlets")[usage.inlet.key].design
            if not isinstance(link, Conduit) or link.section is None:
                yield Diagnostic(code="inlet.requires_conduit", message="Inlet usage requires a conduit with a cross-section", object_id=str(usage.link.key))
                continue
            geometry = link.section.geometry
            if isinstance(inlet, s.CustomInlet):
                allowed = True
            elif isinstance(inlet, (s.GrateInlet, s.CurbInlet)) and inlet.kind.startswith("DROP_"):
                allowed = isinstance(geometry, (RectOpen, Trapezoidal))
            else:
                allowed = isinstance(geometry, Street)
            if not allowed:
                yield Diagnostic(code="inlet.incompatible_conduit", message="Native would remove this inlet because its conduit shape is incompatible", object_id=str(usage.link.key))

    def resource_uses(self, store, profile):
        for record in store.collection("swmm:inlets").values():
            if isinstance(record.design, s.CustomInlet):
                target = record.design.curve
                if not isinstance(target, Ref):
                    continue
                dimensions = ()
                if store.contains(target):
                    curve = store.collection(target.collection)[target.key]
                    dimensions = CURVE_DIMENSIONS.get(curve.kind, ())
                yield ResourceUse(owner=Ref(collection="swmm:inlets", key=record.id), target=target,
                                  path=("design", "curve"), role="custom inlet capture", dimensions=dimensions,
                                  accepted_kinds=("DIVERSION", "RATING"))
