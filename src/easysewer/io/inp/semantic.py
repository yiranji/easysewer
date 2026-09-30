"""Checked semantic ownership projection onto an immutable source document."""

from dataclasses import dataclass, replace
import hashlib
from types import MappingProxyType

from ...model.store import OpaqueConstraint
from ...model.identity import Ref
from ...model.provenance import RecordOrigin, SourceRecord
from ...model.inspection import FieldDeclaration, field_at
from ...schema.structured import FeatureData, FeatureEncoding, FieldLineBinding, ModelSchema
from ...schema.registry import RegistryError
from ...validation import Diagnostic, DiagnosticSubject, Severity, SourceSpan, ValidationReport
from .document import InpDocument, TextEdit, section_key
from .lexer import format_token
from ...validation._cooperative import checkpointed
from ...validation._cooperative import _active, checkpoint
from ...schema.structured import EncodedRow


def _rows_equal(left, right):
    if _active.get() is None:
        return left == right
    def same(a, b):
        if type(a) is type(b) and type(a) in (dict, tuple, EncodedRow):
            if a is b:return True
            if type(a) is EncodedRow:
                return same((a.key,a.section,a.values,a.owners,a.raw_text), (b.key,b.section,b.values,b.owners,b.raw_text))
            if len(a)!=len(b):return False
            if type(a) is dict:
                for key,value in checkpointed(a.items()):
                    if key not in b:return False
                    other=b[key]
                    if value is not other and not same(value,other):return False
            else:
                for value,other in checkpointed(zip(a,b)):
                    if value is not other and not same(value,other):return False
            return True
        return a == b
    checkpoint();result=same(left,right);checkpoint();return result


def _rows(schema, store, profile):
    result = {}
    covered = set()
    for descriptor, codec in checkpointed(schema.bindings):
        if profile.key not in descriptor.profiles:
            continue
        encoding = codec.encode(store, profile)
        if isinstance(encoding, FeatureEncoding):
            for omitted in checkpointed(encoding.omitted):
                if not omitted.reason or not store.contains(omitted.owner):
                    raise RegistryError("Omitted records require an existing owner and an explicit reason")
                covered.add(omitted.owner.canonical)
            encoding = encoding.rows
        for row in checkpointed(encoding):
            if section_key(row.section) not in descriptor.sections:
                raise RegistryError(f"Feature {descriptor.key} emitted an undeclared section")
            if not isinstance(row.key, tuple) or not row.key or not all(isinstance(item, str) for item in checkpointed(row.key)):
                raise RegistryError("Encoded row keys must be nonempty tuples of strings")
            if not isinstance(row.values, tuple) or not row.values and row.raw_text is None:
                raise RegistryError("Encoded rows require tokens or explicitly declared raw text")
            if row.raw_text is not None:
                if row.section not in descriptor.raw_sections or row.values:
                    raise RegistryError("Raw text requires a declared raw section and no token values")
                if type(row.raw_text) is not str or any(c in row.raw_text for c in checkpointed("\r\n\x00")) or row.raw_text.lstrip().startswith("["):
                    raise RegistryError("Raw row must be one physical line and cannot introduce a section")
            for value in checkpointed(row.values):
                format_token(value)
            for owner in checkpointed(row.owners):
                if not store.contains(owner):
                    raise RegistryError(f"Encoded row claims a nonexistent model record: {owner}")
                covered.add(owner.canonical)
            key = descriptor.key, row.key
            if key in result:
                raise RegistryError(f"Duplicate encoded row key: {key}")
            result[key] = row
    from ...model.identity import Ref
    for spec in checkpointed(store.specifications):
        for key in checkpointed(store.collection(spec.key)):
            if Ref(collection=spec.key, key=key).canonical not in covered:
                raise RegistryError(f"Model record has no active INP writer: {spec.key} {key!r}")
    return result


@dataclass(frozen=True, kw_only=True)
class ModelSource:
    decoded: object
    bindings: tuple[tuple[int, str, tuple[str, ...]], ...]
    original_rows: tuple[tuple[tuple, object], ...]
    original_records: object
    provenance_rows: object
    source_sha256: str
    field_declarations: object
    field_coverage: frozenset

    @classmethod
    def read(cls, document: InpDocument, schema: ModelSchema, profile):
        decoded = schema.decode(document, profile=profile)
        store = schema.new_store()
        bindings = []
        for feature, output in checkpointed(decoded.features.items()):
            if not isinstance(output.value, FeatureData):
                raise RegistryError(f"Model codec {feature} must decode FeatureData")
            if {item.line for item in checkpointed(output.value.bindings)} != output.claimed_lines:
                raise RegistryError(f"Model codec {feature} must bind every claimed line exactly once")
            with store._permit_context_change("import"):
                for item in checkpointed(output.value.records):
                    store.collection(item.collection).add(item.value)
            bindings.extend((item.line, feature, item.key) for item in checkpointed(output.value.bindings))
        rows = _rows(schema, store, profile)
        original_records = {}
        provenance_rows = {}
        for spec in checkpointed(store.specifications):
            for key, record in checkpointed(store.collection(spec.key).items()):
                ref = Ref(collection=spec.key, key=key).canonical
                original_records[ref] = record
                store._set_origin(ref, RecordOrigin(kind='inp', original=ref))
        for line, feature, key in checkpointed(bindings):
            if (feature, key) not in rows or rows[feature, key].section != document.lines[line - 1].section:
                raise RegistryError(f"Source binding has no matching encoded row: {feature}, {key}")
            original_line = document.lines[line - 1]
            declaration = SourceRecord(feature=feature, section=original_line.section, key=key,
                span=SourceSpan(source=document.source, line=line, column=1, end_column=len(original_line.content)+1),
                text=original_line.content, values=original_line.values)
            for owner in checkpointed(dict.fromkeys(rows[feature, key].owners)):
                provenance_rows.setdefault(owner.canonical, []).append(declaration)
        field_declarations, field_coverage = {}, set()
        owner_lines = {(feature, line): {r.canonical for r in checkpointed(rows[feature, key].owners)}
                       for line, feature, key in checkpointed(bindings)}
        diagnostic_owners = {(feature, line): tuple(dict.fromkeys(rows[feature, key].owners))
                             for line, feature, key in checkpointed(bindings)}
        def bind_diagnostic(issue, feature):
            if issue.subject is not None and issue.subject.collection is not None:
                return issue
            owners = diagnostic_owners.get((feature, issue.span.line), ()) if issue.span is not None and issue.span.source == document.source else ()
            if not owners:
                return issue
            owner, *related = owners
            return replace(issue, subject=DiagnosticSubject(collection=owner.collection, key=owner.key,
                path=issue.subject.path if issue.subject is not None else ()),
                related=tuple(dict.fromkeys((*issue.related, *(DiagnosticSubject(collection=ref.collection, key=ref.key) for ref in checkpointed(related))))))
        # Only a checked source binding can associate a successful parse with
        # model owners. Unclaimed lines and future syntax remain unbound.
        reports = {key: ValidationReport(diagnostics=tuple(bind_diagnostic(d, key) for d in checkpointed(value.report.diagnostics)))
                   for key, value in checkpointed(decoded.features.items())}
        changed = {id(old): new for key, value in checkpointed(decoded.features.items())
                   for old, new in checkpointed(zip(value.report.diagnostics, reports[key].diagnostics)) if old is not new}
        decoded = replace(decoded, report=ValidationReport(diagnostics=tuple(changed.get(id(d), d) for d in checkpointed(decoded.report.diagnostics))),
            features={key: replace(value, report=reports[key]) for key, value in checkpointed(decoded.features.items())})
        for feature, output in checkpointed(decoded.features.items()):
            data = output.value
            declared_owners = {Ref(collection=item.collection,
                key=store.collection(item.collection).spec.key_of(item.value)).canonical for item in checkpointed(data.records)}
            seen = set()
            for binding in checkpointed((*data.field_coverage, *data.field_bindings)):
                owner = binding.owner.canonical
                if owner not in declared_owners:
                    raise RegistryError('Field declaration must belong to a record decoded by its feature')
                try:
                    field_at(original_records[owner], binding.path)
                except KeyError:
                    raise RegistryError('Field declaration addresses an absent original field') from None
            for coverage in checkpointed(data.field_coverage):
                key = coverage.owner.canonical, coverage.path
                if key in field_coverage:
                    raise RegistryError('Duplicate field coverage declaration')
                field_coverage.add(key)
            for binding in checkpointed(data.field_bindings):
                source_owners = owner_lines.get((feature, binding.line), set())
                if not source_owners or (binding.owner.canonical not in source_owners and binding.role != 'derived'):
                    raise RegistryError('Field token claims must match their feature and encoded record owner')
                line = document.lines[binding.line - 1]
                raw = isinstance(binding, FieldLineBinding)
                if raw and line.section not in decoded.descriptors[feature].raw_sections:
                    raise RegistryError('Field line declarations require an explicitly declared raw section')
                if not raw and any(i >= len(line.tokens) for i in checkpointed(binding.tokens)):
                    raise RegistryError('Field token index exceeds the original physical line')
                key = binding.owner.canonical, binding.path
                duplicate = key, binding.line, None if raw else binding.tokens, binding.role
                if duplicate in seen:
                    raise RegistryError('Duplicate field token declaration')
                seen.add(duplicate)
                field_declarations.setdefault(key, []).append(FieldDeclaration(feature=feature,
                    path=binding.path, line=binding.line, tokens=() if raw else tuple(line.tokens[i] for i in checkpointed(binding.tokens)),
                    role=binding.role, contributes=binding.contributes,
                    raw_text=line.content if raw else None,
                    span=SourceSpan(source=document.source, line=line.number, column=1,
                                    end_column=len(line.content)+1) if raw else None,
                    source_owners=tuple(sorted(source_owners, key=lambda ref: (ref.collection, repr(ref.key))))))
        # A later feature can narrow its own unresolved reference footprint. The
        # conservative fallback never claims to understand opaque expressions.
        constraints = [OpaqueConstraint(description=f"Unparsed [{line.section}] at line {line.number}",
                        span=SourceSpan(source=document.source, line=line.number, column=1,
                                        end_column=len(line.content)+1))
                       for line in checkpointed(decoded.opaque_records) if line.section != "TITLE"]
        store.set_opaque_constraints(constraints)
        return store, cls(decoded=decoded, bindings=tuple(bindings), original_rows=tuple(rows.items()),
            original_records=MappingProxyType(original_records),
            provenance_rows=MappingProxyType({ref: tuple(sorted(values, key=lambda v: v.span.line))
                for ref, values in checkpointed(provenance_rows.items())}),
            source_sha256=hashlib.sha256(document.to_bytes()).hexdigest(),
            field_declarations=MappingProxyType({key: tuple(sorted(values, key=lambda v: v.line))
                for key, values in checkpointed(field_declarations.items())}), field_coverage=frozenset(field_coverage))

    @property
    def report(self):
        issues = list(self.decoded.report.diagnostics)
        opaque = [line for line in checkpointed(self.decoded.opaque_records) if line.section != "TITLE"]
        if opaque:
            issues.append(Diagnostic(code="model.partial_support", severity=Severity.WARNING,
                                     message=f"{len(opaque)} input records remain unstructured and are preserved"))
        return ValidationReport(diagnostics=tuple(issues))

    def render(self, schema, store, profile, *, normalize=False) -> InpDocument:
        current = _rows(schema, store, profile)
        original = dict(self.original_rows)
        document = self.decoded.document
        if _rows_equal(current, original) and _rows_equal(tuple(current), tuple(original)) and not normalize:
            return document
        dirty = {key[0] for key in checkpointed(original.keys() | current.keys()) if not _rows_equal(original.get(key), current.get(key))}
        atomic = {descriptor.key for descriptor, _ in checkpointed(schema.bindings) if descriptor.atomic_write and descriptor.key in dirty}
        for descriptor, codec in checkpointed(schema.bindings):
            rewrite = getattr(codec, 'requires_atomic_write', None)
            if descriptor.key in dirty and callable(rewrite):
                required = rewrite(document, profile)
                if type(required) is not bool:
                    raise RegistryError('Source-dependent atomic-write decisions must be boolean')
                if required:
                    atomic.add(descriptor.key)
            if descriptor.ordered_sections:
                def identities(rows):
                    return tuple((key, row.values[0] if row.raw_text is None else None) for key, row in checkpointed(rows.items())
                                 if key[0] == descriptor.key and row.section in descriptor.ordered_sections)
                if not _rows_equal(identities(current), identities(original)):
                    atomic.add(descriptor.key)
                    dirty.add(descriptor.key)
            if (descriptor.atomic_write and descriptor.sections.intersection(dict(profile.section_terminators))
                    and any(key[0] == descriptor.key for key in checkpointed(current))):
                # Appending even another feature's rows can re-finalize native
                # state left at EOF. Rebuild stateful blocks whenever projecting
                # a changed model, using their resolved independent values.
                atomic.add(descriptor.key)
                dirty.add(descriptor.key)
        if normalize:
            dirty = {descriptor.key for descriptor, _ in checkpointed(schema.bindings)}
            atomic = set(dirty)
        # A dependent native block must follow declarations moved to the end.
        # Feature bindings are dependency ordered, so this also handles chains.
        for descriptor, _ in checkpointed(schema.bindings):
            if set(descriptor.rewrite_after) & atomic:
                atomic.add(descriptor.key)
                dirty.add(descriptor.key)
        bound_keys = {(feature, key) for _, feature, key in checkpointed(self.bindings)}
        last_binding = {(feature, key): line for line, feature, key in checkpointed(sorted(self.bindings))}
        newline = next((line.newline for line in checkpointed(document.lines) if line.newline), "\n")
        edits = []
        line_features = {line: feature for line, feature, _ in checkpointed(self.bindings)}
        atomic_sections = {section for descriptor, _ in checkpointed(schema.bindings) if descriptor.key in atomic for section in checkpointed(descriptor.sections)}
        for section in checkpointed(document.sections):
            # Empty old headers are significant for stateful readers such as
            # TRANSECTS. Remove fully regenerated section occurrences as well.
            if section.name in atomic_sections and all(line_features.get(line.number) in atomic for line in checkpointed(section.records)):
                header = section.header
                replacement = header.comment + header.newline if header.comment else ""
                edits.append(TextEdit(start=header.start, end=header.end, replacement=replacement))
        consumed = {key for key in checkpointed(current) if key not in bound_keys and key[0] not in dirty
                    and _rows_equal(current[key], original.get(key))}
        for line_number, feature, row_key in checkpointed(self.bindings):
            key = feature, row_key
            before = original[key]
            after = current.get(key)
            if _rows_equal(after, before) and feature not in atomic:
                consumed.add(key)
                continue
            line = document.lines[line_number - 1]
            if (feature not in atomic and after is not None and after.section == line.section
                    and last_binding[key] == line_number):
                replacement = _format_line(after)
                if line.comment is not None and before.raw_text is None:
                    replacement += " " + line.comment
                replacement += line.newline
                consumed.add(key)
            else:
                replacement = (line.comment + line.newline) if line.comment is not None and before.raw_text is None else ""
            edits.append(TextEdit(start=line.start, end=line.end, replacement=replacement))
        additions = tuple(row for key, row in checkpointed(current.items()) if key not in consumed)
        # New records get explicit section headers; no dependence on the last
        # source section, and no reordering of source-owned multiline programs.
        if additions:
            edits.sort(key=lambda edit: edit.start)
            suffix = newline if document.text and not document.text.endswith(("\n", "\r")) else ""
            suffix += _format_rows(additions, newline)
            # Combine a terminal replacement with the append into one edit.
            if edits and edits[-1].end == len(document.text):
                last = edits.pop()
                edits.append(TextEdit(start=last.start, end=last.end, replacement=last.replacement + suffix))
            else:
                edits.append(TextEdit(start=len(document.text), end=len(document.text), replacement=suffix))
        return _terminate_sections(document.apply(document.patch(edits)) if edits else document, profile)


def _terminate_sections(document, profile):
    if document.sections:
        following = dict(profile.section_terminators).get(document.sections[-1].name)
        if following:
            newline = next((line.newline for line in checkpointed(document.lines) if line.newline), "\n")
            suffix = ("" if document.text.endswith(("\r", "\n")) else newline) + f"[{following}]" + newline
            return document.apply(document.patch((TextEdit(start=len(document.text), end=len(document.text), replacement=suffix),)))
    return document


def native_document_diagnostics(document, profile):
    # Runner serializes its execution INP as UTF-8, regardless of the source
    # document codec. Native input.c limits physical content to MAXLINE - 1
    # bytes, including comments and whitespace, before tokenizing any section.
    for line in checkpointed(document.lines):
        span = SourceSpan(source=document.source, line=line.number, column=1,
                          end_column=len(line.content) + 1)
        size = len(line.content.encode('utf-8'))
        if size >= 1024:
            yield Diagnostic(code='inp.native_line_length', section=line.section,
                message=f"Native INP lines allow at most 1023 UTF-8 content bytes; this line has {size}", span=span)
        for character in ('\x00', '\x1a'):
            position = line.content.find(character)
            if position >= 0:
                yield Diagnostic(code='inp.native_control_character', section=line.section,
                    message=f"Native INP input rejects U+{ord(character):04X}, including in comments",
                    span=SourceSpan(source=document.source, line=line.number, column=position + 1,
                                    end_column=position + 2))
        if line.newline == '\r' and line.end < len(document.text):
            yield Diagnostic(code='inp.native_line_ending', section=line.section,
                message="Native INP records require LF or CRLF separators; convert this lone CR before running",
                span=SourceSpan(source=document.source, line=line.number, column=len(line.content) + 1,
                                end_column=len(line.content) + 2))
    for line in checkpointed(document.lines):
        if line.kind == 'data' and line.values and line.values[0].startswith('['):
            yield Diagnostic(code='inp.native_header_token', section=line.section,
                message="Native SWMM treats a first token starting with '[' as a section header even when quoted; rename this display record before running",
                span=SourceSpan(source=document.source, line=line.number, column=1,
                    end_column=len(line.content) + 1))
    for section in checkpointed(document.sections):
        text = section.header.content.lstrip()
        if len(text) > 1 and text[0] == "[" and text[1].isspace():
            yield Diagnostic(code="inp.native_section_header", message="Native SWMM does not accept whitespace after '['; normalize the supported section",
                             section=section.name, span=SourceSpan(source=document.source, line=section.header.number,
                                                                  column=1, end_column=len(section.header.content) + 1))


def render_new(schema, store, profile) -> InpDocument:
    return _terminate_sections(InpDocument.from_text(_format_rows(_rows(schema, store, profile).values(), "\n")), profile)


def _format_rows(rows, newline):
    # The codec owns record order, including transitions between sections.
    # Grouping non-contiguous rows would renumber native nodes/links and can
    # change stateful programs or native nonlinear storage calculations.
    result, previous = [], None
    for row in checkpointed(rows):
        if row.section != previous:
            result.append(f"[{row.section}]" + newline)
            previous = row.section
        result.append(_format_line(row) + newline)
    return "".join(result)


def _format_line(row):
    return row.raw_text if row.raw_text is not None else " ".join(format_token(value) for value in checkpointed(row.values))
