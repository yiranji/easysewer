"""Bind explicit semantic subjects to labelled original source evidence."""

from dataclasses import replace
import re

from ..validation import DiagnosticLocation, DiagnosticSubject
from .identity import Ref
from .inspection import field_at, original_field
from .provenance import _same_value


def local_path(text):
    """Parse the local validator's field grammar, never a record identity."""
    if not text:
        return ()
    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*(?:\[[0-9]+\]|\.[A-Za-z][A-Za-z0-9_]*)*', text):
        return ()
    return tuple(int(index) if index else name for name, index in
                 re.findall(r'([A-Za-z][A-Za-z0-9_]*)|\[([0-9]+)\]', text))


def bind_subject(issue, owner):
    """A collection validator knows its owner; unrelated explicit subjects stay."""
    if issue.subject is not None and issue.subject.collection is not None:
        return issue
    return replace(issue, subject=DiagnosticSubject(collection=owner.collection, key=owner.key,
        path=issue.subject.path if issue.subject is not None else ()))


class DiagnosticResolver:
    def __init__(self, model, *, candidates=()):
        self.model = model
        # Rejected drafts never enter the store. Compare their values with the
        # retained source without temporarily mutating the live model.
        self.candidates = {owner.canonical: value for owner, value in candidates}
        self.cache = {}
        self.descendants = None

    def resolve(self, issue):
        subjects = (() if issue.subject is None else (issue.subject,)) + issue.related
        locations = tuple(self.location(subject) for subject in dict.fromkeys(subjects)
                          if subject.collection is not None)
        if not locations:
            return issue
        span = issue.span
        primary = next((v for v in locations if v.subject == issue.subject), None)
        if span is None and primary is not None and primary.status == 'current' and len(primary.spans) == 1:
            span = primary.spans[0]
        return replace(issue, locations=locations, span=span)

    def location(self, subject):
        if subject not in self.cache:
            self.cache[subject] = self._location(subject)
        return self.cache[subject]

    def _location(self, subject):
        model = self.model
        owner = Ref(collection=subject.collection, key=subject.key)
        present = model._store.contains(owner)
        candidate = owner.canonical in self.candidates
        if not present and not candidate:
            return DiagnosticLocation(subject=subject, status='absent')
        if not present:
            return DiagnosticLocation(subject=subject, status='programmatic')
        record = self.candidates[owner.canonical] if candidate else model.collection(owner.collection)[owner.key]
        origin = model._store._origin(owner)
        source = model._source
        if source is None or origin.kind != 'inp':
            return DiagnosticLocation(subject=subject, status='programmatic' if origin.kind == 'created' else 'untracked')
        original = source.original_records.get(origin.original)
        args = dict(subject=subject, original=DiagnosticSubject(collection=origin.original.collection,
                    key=origin.original.key, path=subject.path), source_sha256=source.source_sha256)
        if original is None:
            return DiagnosticLocation(**args, status='unknown')
        if any(type(v) is int for v in subject.path):
            return DiagnosticLocation(**args, status='untracked')
        try:
            before = field_at(original, subject.path)[1] if subject.path else original
            after = field_at(record, subject.path)[1] if subject.path else record
            same_types = type(original) is type(record) and all(
                type(field_at(original, subject.path[:i])[0]) is type(field_at(record, subject.path[:i])[0])
                for i in range(1, len(subject.path) + 1))
        except KeyError:
            return DiagnosticLocation(**args, status='untracked')
        if not same_types:
            return DiagnosticLocation(**args, status='untracked')
        spec = model.collection(owner.collection).spec
        changed = (spec.key_of(record) != spec.key_of(original) or
                   owner.canonical != origin.original.canonical or not _same_value(before, after))
        if not subject.path:
            spans = tuple(row.span for row in source.provenance_rows.get(origin.original, ()))
            return DiagnosticLocation(**args, status='changed' if changed else 'context' if spans else 'unknown', spans=spans)
        provenance = original_field(model, owner, subject.path)
        declarations = provenance.declarations
        aggregate = not declarations and provenance.status != 'omitted'
        exact = provenance.status in ('explicit', 'derived')
        if not declarations and provenance.status == 'omitted':
            return DiagnosticLocation(**args, status='changed' if changed else 'omitted')
        if not declarations:
            # Aggregate fields use only descendant declarations, explicitly as
            # context. Build this prefix index once, not once per diagnostic.
            if self.descendants is None:
                self.descendants = {}
                for (ref, path), values in source.field_declarations.items():
                    for i in range(1, len(path)):
                        self.descendants.setdefault((ref, path[:i]), []).extend(values)
            declarations = self.descendants.get((origin.original, subject.path), ())
        spans = []
        for declaration in declarations:
            if not declaration.contributes:
                continue
            spans.extend(token.span for token in declaration.tokens)
            if declaration.span is not None:
                spans.append(declaration.span)
        spans = tuple(sorted(set(spans), key=lambda s: (s.line, s.column, s.end_column)))
        status = 'changed' if changed else ('current' if exact else 'context' if aggregate else 'unknown') if spans else 'unknown'
        return DiagnosticLocation(**args, status=status, spans=spans)
