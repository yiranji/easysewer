"""Native 5.2.4 REPORT syntax, retaining its cumulative selection semantics."""

from dataclasses import replace

from ...model.identity import Ref
from ...model.fields import validate_fields
from ...model.report import REPORT_COLLECTION, ReportOptions, ReportSelection, get_report
from ...schema.registry import DecodedFeature, FeatureDescriptor
from ...schema.context_fields import REPORT_FIELD_RULES
from ...schema.structured import EncodedRow, FeatureData, FeatureEncoding, OmittedRecord, RecordEntry, SourceBinding
from ...validation import Diagnostic, Severity, SourceSpan, ValidationReport
from .field_sources import FieldSources
from ...validation._cooperative import checkpointed

_OWNER = Ref(collection="swmm:report", key="settings")
_KEYWORDS = (("DISABLED", "disabled"), ("INPUT", "input"), ("SUBCATCH", "subcatchments"),
             ("NODE", "nodes"), ("LINK", "links"), ("CONTINUITY", "continuity"),
             ("FLOWSTATS", "flow_stats"), ("CONTROLS", "controls"), ("AVERAGES", "averages"))
_SELECTIONS = {"subcatchments": "SUBCATCHMENTS", "nodes": "NODES", "links": "LINKS"}


class ReportCodec:
    descriptor = FeatureDescriptor(key="swmm:report", sections=frozenset({"REPORT"}), atomic_write=True,
                                   requires=("swmm:network", "swmm:hydrology"))
    collections = (REPORT_COLLECTION,)
    field_rules = REPORT_FIELD_RULES

    def decode(self, document, profile):
        values, bindings, issues, failed = {}, [], [], set()
        seen = []
        lexical = {d.span.line for d in checkpointed(document.report.errors) if d.span}
        for line in checkpointed(document.records("REPORT")):
            span = SourceSpan(source=document.source, line=line.number, column=1, end_column=len(line.content)+1)
            keyword = line.values[0].upper() if line.values else ""
            field = next((field for prefix, field in checkpointed(_KEYWORDS) if keyword.startswith(prefix)), None)
            if field is None:
                issues.append(Diagnostic(code="report.unsupported_keyword", severity=Severity.WARNING,
                    message="REPORT LID is documented but not accepted by the fixed engine; use a supported LID_USAGE migration" if keyword == "LID" else "Unknown report keyword is preserved",
                    section="REPORT", span=span))
                continue
            seen.append((line, field))
            try:
                tokens = line.values[:40]
                if len(line.values) > 40:
                    issues.append(Diagnostic(code="report.native_token_limit", severity=Severity.WARNING,
                        message="SWMM 5.2.4 reads only the first 40 tokens; the ignored tail remains in the source document",
                        section="REPORT", span=span, field=field))
                if line.number in lexical or len(tokens) < 2:
                    raise ValueError("Report option needs a keyword and value")
                if field in _SELECTIONS:
                    before = values.get(field)
                    members = list(before.members if before else ())
                    mode = tokens[1].upper()
                    if mode not in ("ALL", "NONE"):
                        mode = "SELECTED"
                        known = {r.canonical for r in checkpointed(members)}
                        for name in checkpointed(tokens[1:]):
                            ref = Ref(collection="swmm:" + field, key=name)
                            if ref.canonical not in known:
                                members.append(ref); known.add(ref.canonical)
                    elif len(line.values) > 2:
                        issues.append(Diagnostic(code="report.ignored_tail", severity=Severity.WARNING,
                            message="Native ALL/NONE ignores following tokens on the same line", span=span, field=field))
                    value = ReportSelection(mode=mode, members=tuple(members))
                else:
                    token = line.values[1].upper()
                    if not (token.startswith("YES") or token.startswith("NO")):
                        raise ValueError("Report switch requires YES or NO")
                    value = token.startswith("YES")
                    if len(line.values) > 2 or token not in ("YES", "NO"):
                        issues.append(Diagnostic(code="report.native_coercion", severity=Severity.WARNING,
                            message="Native reporting uses the switch prefix and ignores trailing tokens", span=span, field=field))
                candidate = ReportOptions(**(values | {field: value}))
                ValidationReport(diagnostics=tuple(validate_fields(candidate))).raise_for_errors()
                values[field] = value
                normal = _SELECTIONS.get(field, next(k for k, f in checkpointed(_KEYWORDS) if f == field))
                if keyword != normal:
                    issues.append(Diagnostic(code="report.keyword_alias", severity=Severity.WARNING, span=span,
                        message=f"Native keyword prefix interprets {keyword} as {normal}", field=field))
            except (ValueError, TypeError) as error:
                failed.add(field)
                issues.append(Diagnostic(code="report.invalid_input", message=str(error), section="REPORT", field=field, span=span))
        for field in checkpointed(failed):
            values.pop(field, None)
        # Bind each effective selection's cumulative members to one canonical row;
        # the final ALL/NONE selector has a distinct key when members exist.
        for line, field in checkpointed(seen):
            if field not in failed:
                suffix = "members" if field in _SELECTIONS and line.values[1].upper() not in ("ALL", "NONE") else "mode"
                if field in _SELECTIONS and suffix == "mode" and values[field].mode == "SELECTED":
                    suffix = "members"
                bindings.append(SourceBinding(line=line.number, key=(field, suffix) if field in _SELECTIONS else (field,)))
        # Earlier ALL/NONE lines can now map to a missing effective row (e.g. ALL
        # followed by a list). Remap to that field's first canonical row.
        for i, binding in checkpointed(enumerate(bindings)):
            if len(binding.key) == 2:
                field, suffix = binding.key
                if suffix == "members" and not values[field].members:
                    bindings[i] = replace(binding, key=(field, "mode"))
        records = (RecordEntry(collection="swmm:report", value=ReportOptions(**values)),) if values else ()
        sources = FieldSources()
        sources.cover(_OWNER, *((field,) for _, field in checkpointed(_KEYWORDS)))
        for field in checkpointed(failed): sources.block(_OWNER, (field,))
        last = {field: line.number for line, field in checkpointed(seen) if field not in failed}
        members = {}
        for line, field in checkpointed(seen):
            if field in failed: continue
            if field not in _SELECTIONS:
                sources.add(_OWNER, (field,), line, (1,))
                continue
            tokens = line.values[:40]
            selected = tokens[1].upper() not in ('ALL', 'NONE')
            indexes = tuple(range(1, len(tokens))) if selected else (1,)
            known = members.setdefault(field, {})
            before = len(known)
            sources.cover(_OWNER, (field, 'mode'), (field, 'members'))
            sources.add(_OWNER, (field, 'mode'), line, indexes, role='derived' if selected else 'value')
            if selected:
                namespaces = set()
                for token in checkpointed(indexes):
                    ref = Ref(collection='swmm:' + field, key=tokens[token]).canonical
                    fresh = ref not in known
                    index = known.setdefault(ref, len(known))
                    path = (field, 'members', index)
                    for suffix, positions, role in checkpointed((((), (token,), 'value'), (('key',), (token,), 'value'),
                                                    (('collection',), (0,), 'derived'))):
                        if suffix == ('collection',):
                            if index in namespaces: continue
                            namespaces.add(index)
                        sources.cover(_OWNER, path + suffix)
                        sources.add(_OWNER, path + suffix, line, positions, role=role,
                                    overwrite=False, contributes=fresh)
                sources.add(_OWNER, (field, 'members'), line, indexes, role='derived',
                            overwrite=False, contributes=len(known) > before)
            sources.add(_OWNER, (field,), line, indexes, role='derived', overwrite=False,
                        contributes=len(known) > before or line.number == last[field])
        owners = {_OWNER.canonical: records[0].value} if records else {}
        return DecodedFeature(value=FeatureData(records=records, bindings=tuple(bindings), **sources.finish(owners)),
            claimed_lines=frozenset(b.line for b in checkpointed(bindings)), report=ValidationReport(diagnostics=tuple(issues)))

    def encode(self, store, profile):
        if not store.collection("swmm:report"):
            return FeatureEncoding()
        options = get_report(store)
        rows = []
        for keyword, field in checkpointed(_KEYWORDS):
            value = getattr(options, field)
            if value is None:
                continue
            if field in _SELECTIONS:
                keyword = _SELECTIONS[field]
                if value.members:
                    # Chunk by conservative native token/line limits, keeping every ID.
                    chunk, size, index = [], len(keyword), 0
                    for ref in checkpointed(value.members):
                        cost = len(str(ref.key).encode("utf-8")) + 1
                        if cost + len(keyword) > 900:
                            raise ValueError("Report object ID exceeds the native line budget")
                        if chunk and (len(chunk) >= 39 or size + cost > 900):
                            rows.append(EncodedRow(key=(field, "members" if index == 0 else f"members-{index}"), section="REPORT", values=(keyword, *chunk), owners=(_OWNER,)))
                            chunk, size, index = [], len(keyword), index+1
                        if not chunk and str(ref.key).upper() in ("ALL", "NONE"):
                            # Repeating an already selected ordinary ID prevents
                            # a chunk's first reserved ID becoming a selector.
                            chunk = [value.members[0].key]
                            size += len(str(chunk[0]).encode("utf-8")) + 1
                        if size + cost > 900:
                            raise ValueError("Reserved report ID cannot fit after an ordinary ID within the native line budget")
                        chunk.append(ref.key); size += cost
                    rows.append(EncodedRow(key=(field, "members" if index == 0 else f"members-{index}"), section="REPORT", values=(keyword, *chunk), owners=(_OWNER,)))
                if value.mode != "SELECTED":
                    rows.append(EncodedRow(key=(field, "mode"), section="REPORT", values=(keyword, value.mode), owners=(_OWNER,)))
            else:
                rows.append(EncodedRow(key=(field,), section="REPORT", values=(keyword, "YES" if value else "NO"), owners=(_OWNER,)))
        return FeatureEncoding(rows=tuple(rows), omitted=() if rows else (OmittedRecord(owner=_OWNER, reason="No report settings explicitly set"),))

    def validate(self, store, profile):
        return ()

    def validate_document(self, document, profile):
        for line in document.records("REPORT"):
            if line.values and line.values[0].upper() == "LID":
                yield Diagnostic(code="report.native_lid_keyword", message="SWMM 5.2.4 rejects REPORT LID; migrate its detailed-output request to LID_USAGE", section="REPORT",
                    span=SourceSpan(source=document.source, line=line.number, column=1, end_column=len(line.content)+1))
