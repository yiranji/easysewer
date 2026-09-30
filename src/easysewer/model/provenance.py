"""Immutable source declarations, distinct from current rendered INP rows."""

from dataclasses import dataclass, fields, is_dataclass
from datetime import datetime, time
from typing import Literal

from .identity import Ref
from ..validation import SourceSpan


@dataclass(frozen=True, kw_only=True)
class RecordOrigin:
    """Internal lifecycle association; not a timestamp or an authenticity claim."""
    kind: Literal['inp', 'created', 'untracked'] = 'created'
    original: Ref | None = None

    def __post_init__(self):
        if self.kind not in ('inp', 'created', 'untracked'):
            raise ValueError('Unknown record origin kind')
        if (self.kind == 'inp' and not isinstance(self.original, Ref) or
                self.kind != 'inp' and self.original is not None):
            raise ValueError('Only INP origins require an original record reference')
        if self.original is not None:
            object.__setattr__(self, 'original', self.original.canonical)


@dataclass(frozen=True, kw_only=True)
class SourceRecord:
    feature: str
    section: str
    key: tuple[str, ...]
    span: SourceSpan
    text: str
    values: tuple[str, ...]


@dataclass(frozen=True, kw_only=True)
class RecordProvenance:
    owner: Ref
    kind: Literal['inp', 'created', 'untracked', 'unavailable']
    original: Ref | None
    source: str | None
    source_sha256: str | None
    records: tuple[SourceRecord, ...]
    original_value: object = None
    changed: bool | None = None


def _same_value(left, right):
    if left is right:
        return True
    if type(left) is not type(right):
        return type(left) in (int, float) and type(right) in (int, float) and left == right
    if is_dataclass(left):
        return all(_same_value(getattr(left, f.name), getattr(right, f.name)) for f in fields(left))
    if isinstance(left, tuple):
        return len(left) == len(right) and all(_same_value(a, b) for a, b in zip(left, right))
    if isinstance(left, (datetime, time)):
        return left == right and left.fold == right.fold
    return left == right


def record_provenance(model, ref: Ref) -> RecordProvenance:
    if not isinstance(ref, Ref):
        raise TypeError('Provenance queries require a Ref')
    record = model.collection(ref.collection)[ref.key]
    spec = model.collection(ref.collection).spec
    owner = Ref(collection=ref.collection, key=spec.key_of(record))
    origin = model._store._origin(owner)
    source = model._source
    if origin.kind != 'inp' or source is None:
        return RecordProvenance(owner=owner, kind=origin.kind if source is not None else
            ('untracked' if origin.kind == 'inp' else origin.kind), original=None,
            source=None, source_sha256=None, records=())
    original = source.original_records.get(origin.original)
    rows = source.provenance_rows.get(origin.original, ())
    return RecordProvenance(owner=owner, kind='inp' if original is not None else 'unavailable',
        original=origin.original, source=source.decoded.document.source,
        source_sha256=source.source_sha256, records=rows, original_value=original,
        changed=None if original is None else not _same_value(record, original))
