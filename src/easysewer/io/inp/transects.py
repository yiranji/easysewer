"""Stateful NC/X1/GR decoding and independent, explicitly terminated output."""

from dataclasses import replace
import math

from ...model.fields import validate_fields
from ...model.identity import Ref, canonical_key
from ...model.options import get_options
from ...model.units import UnitContext
from ...model.surface import Transect, TransectPoint, TransectRoughness, TRANSECTS_COLLECTION, TRANSECT_UNIT_TRANSFORMS
from ...schema.registry import DecodedFeature, FeatureDescriptor
from ...schema.structured import EncodedRow, FeatureData, RecordEntry, SourceBinding
from ...validation import Diagnostic, Severity, SourceSpan, ValidationReport
from .geometry import finite_number, number_text
from .field_sources import FieldSources
from .transect_sources import TransectSources
from ...schema.surface_fields import TRANSECT_FIELD_RULES
from ...validation._cooperative import checkpointed


def _key(name, part="header"):
    return canonical_key(name), str(part)


class TransectsCodec:
    descriptor = FeatureDescriptor(key="swmm:transects", sections=frozenset({"TRANSECTS"}), atomic_write=True)
    collections = (TRANSECTS_COLLECTION,)
    unit_transforms = TRANSECT_UNIT_TRANSFORMS
    field_rules = TRANSECT_FIELD_RULES

    def decode(self, document, profile):
        rows = document.records("TRANSECTS")
        if not rows:
            return DecodedFeature(value=FeatureData())
        issues, bindings, completed = [], [], []
        source_fields = FieldSources()
        sources = TransectSources(source_fields)
        values = [0.0, 0.0, 0.0]
        pending_nc = []
        current = None
        previous_section = None
        seen = set()
        lexical_errors = {item.span.line for item in checkpointed(document.report.errors) if item.span}
        final_name = next((line.values[1] for line in checkpointed(reversed(rows))
                           if len(line.values) > 1 and line.values[0].upper() == "X1"), None)

        def issue(line, code, message, severity=Severity.WARNING):
            issues.append(Diagnostic(code=code, message=message, severity=severity, section="TRANSECTS",
                                     span=SourceSpan(source=document.source, line=line.number, column=1,
                                                     end_column=len(line.content) + 1)))

        def finalize(line, *, section_exit=False):
            if current is None:
                return
            wrong_target = section_exit and canonical_key(current["id"]) != canonical_key(final_name)
            if wrong_target:
                issue(line, "transect.early_section_exit", "Native section exit targets the last declared transect and advances inherited roughness", Severity.INFO)
            if len(current["stations"]) + current["finalizations"] >= 1500:
                issue(line, "transect.native_station_capacity", "Repeated native finalization exceeds the station capacity; normalize before running")
            # Native validation mutates the channel n by sqrt(Lfactor). A zero
            # in a subsequent NC inherits that value, not the original token.
            if not wrong_target:
                current["roughness"] = TransectRoughness(left=values[0], right=values[1], channel=values[2])
                current["native_complete"] = True
            sources.finalize(wrong_target=wrong_target)
            values[2] *= math.sqrt(current["meander_factor"] or 1)
            current["finalized"] = True
            current["finalizations"] += 1

        def save(line):
            if current is None:
                return
            if not current["native_complete"]:
                issue(line, "transect.missing_native_finalizer", "Native reader requires NC or a following section to finish this transect; normalize before running")
            fields = {key: value for key, value in checkpointed(current.items()) if key not in ("count", "finalized", "finalizations", "native_complete")}
            fields["stations"] = tuple(fields["stations"])
            record = Transect(**fields)
            report = ValidationReport(diagnostics=tuple(validate_fields(record)))
            report.raise_for_errors()
            issues.extend(report.diagnostics)
            if current["count"] != len(record.stations):
                issue(line, "transect.ignored_count", "Declared station count differs from GR data; SWMM uses actual stations")
            completed.append(RecordEntry(collection="swmm:transects", value=record))
            sources.save(record)

        try:
            for line in checkpointed(document.lines):
                if line.kind == "header":
                    if current is not None and previous_section == "TRANSECTS":
                        finalize(line, section_exit=True)
                    previous_section = line.section
                    continue
                if line.section != "TRANSECTS" or line.kind not in ("data", "raw"):
                    continue
                if line.number in lexical_errors:
                    raise ValueError("Malformed transect line prevents resolving its inherited state")
                tokens = line.values
                keyword = tokens[0].upper() if tokens else ""
                if keyword == "NC":
                    if len(tokens) != 4:
                        raise ValueError("NC requires left, right and channel roughness")
                    finalize(line)
                    roughness = [finite_number(token) for token in checkpointed(tokens[1:])]
                    if any(value < 0 for value in checkpointed(roughness)):
                        raise ValueError("NC roughness cannot be negative")
                    sources.manning(line, values, roughness)
                    values = [new if new > 0 else old for old, new in checkpointed(zip(values, roughness))]
                    values[0] = values[0] or values[2]
                    values[1] = values[1] or values[2]
                    pending_nc.append(line.number)
                elif keyword == "X1":
                    if not 10 <= len(tokens) <= 11:
                        raise ValueError("SWMM 5.2.4 X1 requires ID, count, banks, two placeholders, meander, width and elevation offset")
                    save(line)
                    if canonical_key(tokens[1]) in seen:
                        raise ValueError(f"Duplicate transect {tokens[1]}")
                    seen.add(canonical_key(tokens[1]))
                    data = [finite_number(token) for token in checkpointed(tokens[2:10])]
                    if data[5] < 0 or data[6] < 0:
                        raise ValueError("Transect meander and width factors cannot be negative")
                    current = dict(id=tokens[1], roughness=TransectRoughness(left=values[0], right=values[1], channel=values[2]),
                                   count=data[0], left_bank=data[1], right_bank=data[2], meander_factor=data[5],
                                   width_factor=data[6], elevation_offset=data[7], stations=[], finalized=False, finalizations=0, native_complete=False)
                    sources.start(line)
                    if any(data[3:5]) or len(tokens) == 11:
                        issue(line, "transect.ignored_fields", "Native X1 ignores its two placeholder values and any eleventh token; canonical output uses ten tokens")
                    bindings.extend(SourceBinding(line=number, key=_key(tokens[1], "nc")) for number in checkpointed(pending_nc))
                    pending_nc.clear()
                    bindings.append(SourceBinding(line=line.number, key=_key(tokens[1])))
                elif keyword == "GR":
                    if current is None:
                        raise ValueError("GR requires a preceding X1 transect")
                    if current["finalized"]:
                        raise ValueError("GR after a native finalization boundary cannot be safely merged into the preceding transect")
                    if len(tokens) < 3 or len(tokens) % 2 != 1:
                        raise ValueError("GR requires complete elevation-station pairs")
                    current["stations"].extend(TransectPoint(elevation=finite_number(tokens[index]), station=finite_number(tokens[index + 1]))
                                               for index in checkpointed(range(1, len(tokens), 2)))
                    sources.stations(line)
                    bindings.append(SourceBinding(line=line.number, key=_key(current["id"])))
                else:
                    raise ValueError(f"Unsupported transect state record {keyword!r}")
            save(rows[-1])
            if current is None:
                raise ValueError("Transect section contains no X1 definition")
            bindings.extend(SourceBinding(line=number, key=_key(current["id"], "nc")) for number in checkpointed(pending_nc))
        except (ValueError, TypeError, OverflowError) as error:
            issue(line, "transect.invalid_input", str(error), Severity.ERROR)
            return DecodedFeature(value=FeatureData(), report=ValidationReport(diagnostics=tuple(issues)))
        originals = {Ref(collection=e.collection, key=e.value.id).canonical: e.value for e in checkpointed(completed)}
        sources.reconcile_owners(bindings)
        return DecodedFeature(value=FeatureData(records=tuple(completed), bindings=tuple(bindings), **source_fields.finish(originals)),
                              claimed_lines=frozenset(item.line for item in checkpointed(bindings)), report=ValidationReport(diagnostics=tuple(issues)))

    def encode(self, store, profile):
        def row(record, values, part="header"):
            return EncodedRow(key=_key(record.id, part), section="TRANSECTS", values=values,
                              owners=(Ref(collection="swmm:transects", key=record.id),))
        for record in checkpointed(store.collection("swmm:transects").values()):
            yield row(record, ("NC", *(number_text(getattr(record.roughness, name)) for name in checkpointed(("left", "right", "channel")))), "nc")
            yield row(record, ("X1", record.id, str(len(record.stations)), number_text(record.left_bank), number_text(record.right_bank),
                               "0", "0", number_text(record.meander_factor), number_text(record.width_factor), number_text(record.elevation_offset)))
            for index, point in checkpointed(enumerate(record.stations)):
                yield row(record, ("GR", number_text(point.elevation), number_text(point.station)), index)

    def validate(self, store, profile):
        options = get_options(store)
        if not profile.transect_mixed_width_scaling or not ValidationReport(diagnostics=tuple(validate_fields(options))).is_valid:
            return
        units = UnitContext(flow_units=options.flow_units or profile.option_default("flow_units"))
        if units.system == "SI":
            for record in store.collection("swmm:transects").values():
                if record.width_factor not in (0, 1):
                    yield Diagnostic(code="transect.width_bank_rounding", severity=Severity.WARNING,
                                     object_id=record.id, field="width_factor", section="TRANSECTS",
                                     message="Native SI bank/station arithmetic differs with a non-unit width factor; "
                                             "rounding can change overbank roughness boundaries")

    def validate_document(self, document, profile):
        for issue in self.decode(document, profile).report.diagnostics:
            if issue.code in ("transect.missing_native_finalizer", "transect.native_station_capacity"):
                yield replace(issue, severity=Severity.ERROR)
