"""Ordered typed collections and transactional reference-aware graph changes."""

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, is_dataclass, replace
from typing import Callable, Generic, Iterable, TypeVar

from ..validation import Diagnostic, DiagnosticSubject, SourceSpan, ValidationError, ValidationReport
from .identity import (
    RecordKey, Ref, ReferenceUse, canonical_key, namespace_key, references,
    require_immutable, rewrite_references,
)
from .provenance import RecordOrigin
from ..validation._cooperative import _active, checkpointed

T = TypeVar("T")


@dataclass(frozen=True, kw_only=True)
class CollectionSpec(Generic[T]):
    key: str
    record_type: type[T] | tuple[type, ...]
    key_of: Callable[[T], RecordKey]
    identity_field: str | None = None
    validate: Callable[[T], Iterable[Diagnostic]] | None = None
    validate_change: Callable[[T | None, T | None, "RecordStore"], Iterable[Diagnostic]] | None = None

    def __post_init__(self):
        namespace_key(self.key)
        types = self.record_type if isinstance(self.record_type, tuple) else (self.record_type,)
        if not types or any(not isinstance(item, type) for item in types):
            raise TypeError("record_type must contain types")
        if not callable(self.key_of):
            raise TypeError("key_of must be callable")


@dataclass(frozen=True, kw_only=True)
class OpaqueConstraint:
    """Unparsed input that might reference any object in the given namespaces."""

    description: str
    collections: frozenset[str] | None = None
    span: SourceSpan | None = None

    def __post_init__(self):
        if self.span is not None and type(self.span) is not SourceSpan:
            raise TypeError('Opaque constraint source must be a SourceSpan')
        if self.collections is not None:
            object.__setattr__(self, "collections", frozenset(namespace_key(c) for c in self.collections))


class RecordConflictError(ValueError):
    pass


class RecordCollection(Mapping[RecordKey, T], Generic[T]):
    """A live mapping view. Iteration yields original keys, never engine indexes."""

    def __init__(self, store: "RecordStore", spec: CollectionSpec[T], *, _diagnostics=None):
        self._store = store
        self.spec = spec
        self._diagnostics = _diagnostics

    @contextmanager
    def _mutation(self, candidate=None):
        try:
            yield
        except ValidationError as error:
            if self._diagnostics is None:
                raise
            candidates = () if candidate is None else ((
                Ref(collection=self.spec.key, key=self.spec.key_of(candidate)), candidate),)
            raise ValidationError(self._diagnostics(error.report, candidates=candidates)) from error

    def __getitem__(self, key: RecordKey) -> T:
        return self._store._records[self.spec.key][canonical_key(key)]

    def __iter__(self) -> Iterator[RecordKey]:
        # Snapshot the order so mutation during iteration does not corrupt it.
        records = self._store._records[self.spec.key].values()
        if _active.get() is None:
            return iter(tuple(self.spec.key_of(value) for value in records))
        keys = tuple(self.spec.key_of(value) for value in checkpointed(records))
        return checkpointed(keys)

    def __len__(self):
        return len(self._store._records[self.spec.key])

    def add(self, record: T) -> None:
        with self._mutation(record):
            self._store._put(self.spec, record, adding=True)

    def replace(self, key: RecordKey, record: T) -> None:
        self._store._check(self.spec, record)
        if self.spec.key_of(record) != self.spec.key_of(self[key]):
            raise RecordConflictError("Replacement cannot change identity; use rename for named entities")
        with self._mutation(record):
            self._store._put(self.spec, record, adding=False)

    def update(self, key: RecordKey, **changes) -> T:
        if self.spec.identity_field in changes:
            raise RecordConflictError("Use rename to change an object's ID")
        record = replace(self[key], **changes)
        self.replace(key, record)
        return record

    def rename(self, key: RecordKey, new_key: str) -> None:
        with self._mutation():
            self._store.rename(Ref(collection=self.spec.key, key=key), new_key)

    def remove(self, key: RecordKey, *, cascade: bool = False) -> tuple[Ref, ...]:
        with self._mutation():
            return self._store.remove(Ref(collection=self.spec.key, key=key), cascade=cascade)

    def move(self, key: RecordKey, *, before: RecordKey | None = None) -> None:
        """Move an existing record before another, or to the end."""
        with self._mutation():
            self._store.move(Ref(collection=self.spec.key, key=key), before=before)


class RecordStore:
    """Single-owner editable graph; records and configuration are immutable values.

    Transactions roll back graph data, specifications, constraints and revision
    on failure, including nested failures. They do not promise concurrent-thread
    isolation. Use a cloned snapshot for independent work or simulation.
    """

    def __init__(self, specs: Iterable[CollectionSpec] = (), *, opaque: Iterable[OpaqueConstraint] = (), guards=()):
        self._specs: dict[str, CollectionSpec] = {}
        self._records: dict[str, dict[RecordKey, object]] = {}
        self._origins: dict[Ref, RecordOrigin] = {}
        self._opaque = tuple(opaque)
        self._guards = tuple(guards)
        if any(not callable(guard) for guard in self._guards):
            raise TypeError("Mutation guards must be explicit callables")
        if any(not isinstance(value, OpaqueConstraint) for value in self._opaque):
            raise TypeError("Expected OpaqueConstraint values")
        self._revision = 0
        self._reference_index = None
        self._context_permissions = frozenset()
        for spec in specs:
            self.register(spec)

    @property
    def revision(self) -> int:
        return self._revision

    @property
    def specifications(self) -> tuple[CollectionSpec, ...]:
        return tuple(self._specs.values())

    @property
    def opaque_constraints(self) -> tuple[OpaqueConstraint, ...]:
        return self._opaque

    def register(self, spec: CollectionSpec) -> None:
        if spec.key in self._specs:
            raise RecordConflictError(f"Collection already registered: {spec.key}")
        self._specs[spec.key] = spec
        self._records[spec.key] = {}
        self._changed()

    def collection(self, key: str) -> RecordCollection:
        return RecordCollection(self, self._specs[key])

    def set_opaque_constraints(self, constraints: Iterable[OpaqueConstraint]) -> None:
        values = tuple(constraints)
        if any(not isinstance(value, OpaqueConstraint) for value in values):
            raise TypeError("Expected OpaqueConstraint values")
        self._opaque = values
        self._changed()

    def _changed(self):
        self._revision += 1
        self._reference_index = None

    @staticmethod
    def _check(spec, record):
        if not isinstance(record, spec.record_type) or not is_dataclass(record):
            raise TypeError(f"Wrong record type for {spec.key}: {type(record).__name__}")
        require_immutable(record)
        return canonical_key(spec.key_of(record))

    def _put(self, spec, record, *, adding):
        key = self._check(spec, record)
        records = self._records[spec.key]
        if adding and key in records:
            raise RecordConflictError(f"Duplicate key in {spec.key}: {spec.key_of(record)!r}")
        if adding:
            self._guard("add an object without proving its ID is available", (Ref(collection=spec.key, key=key),))
        if not adding and key not in records:
            raise KeyError(spec.key_of(record))
        self._validate_change(spec, records.get(key), record)
        self._run_guards("add" if adding else "replace", (Ref(collection=spec.key, key=key),), before=records.get(key), after=record)
        records[key] = record
        if adding:
            self._origins[Ref(collection=spec.key, key=key)] = RecordOrigin()
        self._changed()

    def _origin(self, ref: Ref) -> RecordOrigin:
        if not self.contains(ref):
            raise KeyError(ref.key)
        return self._origins[ref.canonical]

    def _set_origin(self, ref: Ref, origin: RecordOrigin) -> None:
        if not self.contains(ref):
            raise KeyError(ref.key)
        if not isinstance(origin, RecordOrigin):
            raise TypeError('Expected RecordOrigin')
        if origin.original is not None and origin.original.collection != ref.collection:
            raise ValueError('Source lineage cannot change collection')
        self._origins[ref.canonical] = origin

    def _validate_change(self, spec, before, after):
        if spec.validate_change is not None:
            from .diagnostics import bind_subject
            owner = Ref(collection=spec.key, key=spec.key_of(after if after is not None else before))
            ValidationReport(diagnostics=tuple(bind_subject(issue, owner)
                for issue in spec.validate_change(before, after, self))).raise_for_errors()

    def _context_change_allowed(self, kind):
        return kind in self._context_permissions or "import" in self._context_permissions

    def _run_guards(self, action, targets, *, before=None, after=None):
        if "import" in self._context_permissions:
            return
        targets = tuple(targets)
        issues = tuple(issue for guard in self._guards
                       for issue in guard(self, action, targets, before=before, after=after))
        ValidationReport(diagnostics=issues).raise_for_errors()

    @contextmanager
    def _permit_context_change(self, kind):
        previous = self._context_permissions
        self._context_permissions = previous | {kind}
        try:
            yield
        finally:
            self._context_permissions = previous

    def contains(self, ref: Ref) -> bool:
        return ref.collection in self._records and canonical_key(ref.key) in self._records[ref.collection]

    def _index(self):
        if self._reference_index is None:
            result = {}
            for namespace, rows in checkpointed(self._records.items()):
                spec = self._specs[namespace]
                for record in checkpointed(rows.values()):
                    owner = Ref(collection=namespace, key=spec.key_of(record))
                    for path, target in checkpointed(references(record)):
                        result.setdefault(target.canonical, []).append(
                            ReferenceUse(owner=owner, target=target, path=path))
            self._reference_index = {key: tuple(checkpointed(value)) for key, value in result.items()}
        return self._reference_index

    def referenced_by(self, ref: Ref) -> tuple[ReferenceUse, ...]:
        return self._index().get(ref.canonical, ())

    def validate(self) -> ValidationReport:
        from .diagnostics import bind_subject
        issues = []
        for namespace, rows in checkpointed(self._records.items()):
            validator = self._specs[namespace].validate
            if validator:
                for record in checkpointed(rows.values()):
                    owner = Ref(collection=namespace, key=self._specs[namespace].key_of(record))
                    issues.extend(bind_subject(issue, owner) for issue in checkpointed(validator(record)))
        for target, uses in checkpointed(self._index().items()):
            if self.contains(target):
                continue
            for use in checkpointed(uses):
                related = (DiagnosticSubject(collection=use.target.collection, key=use.target.key),)
                # Tuple positions are not persistent identities. Keep the item
                # subject, but expose its first stable aggregate as labelled
                # source context after reordering or replacement.
                index = next((i for i, part in enumerate(use.path) if type(part) is int), None)
                if index is not None:
                    related += (DiagnosticSubject(collection=use.owner.collection, key=use.owner.key,
                                                  path=use.path[:index]),)
                issues.append(Diagnostic(
                    code="model.unresolved_reference", message=f"Missing {target.collection} {use.target.key!r}",
                    feature=use.owner.collection, object_id=str(use.owner.key),
                    field=".".join(str(part) for part in use.path),
                    subject=DiagnosticSubject(collection=use.owner.collection, key=use.owner.key, path=use.path),
                    related=related,
                ))
        return ValidationReport(diagnostics=tuple(issues))

    def _guard(self, action: str, targets: Iterable[Ref]):
        targets = tuple(targets)
        affected = {target.collection for target in targets}
        issues = []
        for constraint in self._opaque:
            if constraint.collections is not None and not affected.intersection(constraint.collections):
                continue
            subjects = tuple(DiagnosticSubject(collection=target.collection, key=target.key)
                for target in targets if constraint.collections is None or target.collection in constraint.collections)
            issues.append(Diagnostic(code="model.opaque_reference_risk",
                message=f"Cannot {action}: {constraint.description}", span=constraint.span,
                subject=subjects[0] if subjects else None, related=subjects[1:]))
        ValidationReport(diagnostics=tuple(issues)).raise_for_errors()

    def rename(self, ref: Ref, new_key: str) -> None:
        spec = self._specs[ref.collection]
        if not spec.identity_field:
            raise TypeError("This collection has derived/record keys, not a renameable entity ID")
        if not isinstance(new_key, str):
            raise TypeError("Named entity IDs must be strings")
        original = self.collection(ref.collection)[ref.key]
        old = Ref(collection=ref.collection, key=spec.key_of(original))
        renamed = replace(original, **{spec.identity_field: new_key})
        new = Ref(collection=ref.collection, key=spec.key_of(renamed))
        if old.key == new.key:
            return
        self._guard("rename", (old,))
        if old.canonical != new.canonical and self.contains(new):
            raise RecordConflictError(f"Rename target already exists: {new_key}")
        self._run_guards("rename", (old,), before=original, after=renamed)
        candidate = {}
        origins = {}
        for namespace, rows in self._records.items():
            row_spec = self._specs[namespace]
            updated = {}
            for key, record in rows.items():
                if namespace == old.collection and key == old.canonical.key:
                    record = replace(record, **{spec.identity_field: new_key})
                record = rewrite_references(record, old, new)
                next_key = self._check(row_spec, record)
                if next_key in updated:
                    raise RecordConflictError(f"Rename creates duplicate relation key in {namespace}: {next_key}")
                updated[next_key] = record
                origins[Ref(collection=namespace, key=next_key)] = self._origins[Ref(collection=namespace, key=key)]
            candidate[namespace] = updated
        self._records = candidate
        self._origins = origins
        self._changed()

    def move(self, ref: Ref, *, before: RecordKey | None = None) -> None:
        rows = self._records[ref.collection]
        key = canonical_key(ref.key)
        if key not in rows:
            raise KeyError(ref.key)
        target = canonical_key(before) if before is not None else None
        if target is not None and target not in rows:
            raise KeyError(before)
        if key == target:
            return
        self._guard("reorder", (ref,))
        moved = {}
        for identity, record in rows.items():
            if identity == target:
                moved[key] = rows[key]
            if identity != key:
                moved[identity] = record
        if target is None:
            moved[key] = rows[key]
        if tuple(moved) != tuple(rows):
            self._run_guards("move", (ref,))
            self._records[ref.collection] = moved
            self._changed()

    def deletion_plan(self, ref: Ref) -> tuple[Ref, ...]:
        record = self.collection(ref.collection)[ref.key]
        first = Ref(collection=ref.collection, key=self._specs[ref.collection].key_of(record))
        result = [first]
        visited = {first.canonical}
        for target in result:
            for use in self.referenced_by(target):
                if use.owner.canonical not in visited:
                    visited.add(use.owner.canonical)
                    result.append(use.owner)
        return tuple(result)

    def remove(self, ref: Ref, *, cascade: bool = False) -> tuple[Ref, ...]:
        plan = self.deletion_plan(ref)
        if not cascade and len(plan) > 1:
            raise ValidationError(ValidationReport(diagnostics=(Diagnostic(
                code="model.object_in_use", message=f"{ref.key!r} is referenced by {len(plan) - 1} other records",
                feature=ref.collection, object_id=str(ref.key),
                subject=DiagnosticSubject(collection=plan[0].collection, key=plan[0].key),
                related=tuple(dict.fromkeys(DiagnosticSubject(collection=use.owner.collection,
                    key=use.owner.key, path=use.path) for target in plan for use in self.referenced_by(target))),
            ),)))
        self._guard("delete", plan)
        self._run_guards("remove", plan)
        candidate = {key: dict(rows) for key, rows in self._records.items()}
        for item in plan:
            spec = self._specs[item.collection]
            self._validate_change(spec, candidate[item.collection][item.canonical.key], None)
            del candidate[item.collection][item.canonical.key]
        self._records = candidate
        self._origins = {ref: origin for ref, origin in self._origins.items() if self.contains(ref)}
        self._changed()
        return plan

    def clone(self) -> "RecordStore":
        result = RecordStore(self.specifications, opaque=self._opaque, guards=self._guards)
        if _active.get() is None:
            result._records = {key: dict(rows) for key, rows in self._records.items()}
            result._origins = dict(self._origins)
        else:
            result._records = {key: dict(checkpointed(rows.items()))
                               for key, rows in checkpointed(self._records.items())}
            result._origins = dict(checkpointed(self._origins.items()))
        result._revision = self._revision
        return result

    @contextmanager
    def transaction(self, *, validate: bool = True):
        previous = (dict(self._specs), {key: dict(rows) for key, rows in self._records.items()},
                    self._opaque, self._revision, dict(self._origins))
        try:
            yield self
            if validate:
                self.validate().raise_for_errors()
        except BaseException:
            self._specs, self._records, self._opaque, self._revision, self._origins = previous
            self._reference_index = None
            raise
