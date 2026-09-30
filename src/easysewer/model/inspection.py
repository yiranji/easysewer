"""Candidate field facts and original declarations; unknown is never a default."""

from dataclasses import MISSING, dataclass, field, fields, is_dataclass

from .identity import Ref, require_immutable
from .provenance import _same_value
from ..validation import Diagnostic, SourceSpan


def field_path(path):
    path = (path,) if isinstance(path, str) else path
    if (not isinstance(path, tuple) or not path or
            any(not (type(p) is str and p and not p.startswith('_') or
                     type(p) is int and p >= 0) for p in path)):
        raise ValueError('Field paths require public attribute names and nonnegative tuple indexes')
    return path


def field_at(record, path):
    """Only dataclass fields and tuple items are addressable; no arbitrary getattr."""
    value = record
    for part in field_path(path):
        parent = value
        if type(part) is int:
            if not isinstance(value, tuple) or part >= len(value):
                raise KeyError(path)
            value = value[part]
        else:
            if not is_dataclass(value) or part not in {f.name for f in fields(value)}:
                raise KeyError(path)
            value = getattr(value, part)
    return parent, value


@dataclass(frozen=True, kw_only=True)
class FieldFact:
    status: str = 'unknown'
    value: object = None
    reason: str = ''

    def __post_init__(self):
        if self.status not in ('known', 'unknown', 'required', 'not_applicable', 'invalid', 'ambiguous'):
            raise ValueError('Unknown field fact status')
        if self.status != 'known' and self.value is not None:
            raise ValueError('Only known facts have a value; explain alternatives in reason')
        require_immutable(self.value)


@dataclass(frozen=True, kw_only=True)
class FieldSemantics:
    unit: FieldFact = field(default_factory=FieldFact)
    default: FieldFact = field(default_factory=FieldFact)
    effective: FieldFact = field(default_factory=FieldFact)
    diagnostics: tuple = ()

    def __post_init__(self):
        if any(not isinstance(v, FieldFact) for v in (self.unit, self.default, self.effective)):
            raise TypeError('Field semantics require labelled FieldFact values')
        if not isinstance(self.diagnostics, tuple) or any(not isinstance(v, Diagnostic) for v in self.diagnostics):
            raise TypeError('Field diagnostics require an immutable tuple of Diagnostic values')
        require_immutable(self)


@dataclass(frozen=True, kw_only=True)
class FieldDeclaration:
    feature: str
    path: tuple
    line: int
    tokens: tuple
    role: str
    contributes: bool
    source_owners: tuple[Ref, ...] = ()
    raw_text: str | None = None
    span: SourceSpan | None = None


@dataclass(frozen=True, kw_only=True)
class FieldProvenance:
    """The path is explicitly in the original record's coordinate system."""
    owner: Ref
    original: Ref | None
    path: tuple
    status: str
    value: FieldFact = field(default_factory=FieldFact)
    declarations: tuple[FieldDeclaration, ...] = ()
    source: str | None = None
    source_sha256: str | None = None


@dataclass(frozen=True, kw_only=True)
class FieldInfo:
    owner: Ref
    path: tuple
    value: object
    constructor_default: FieldFact
    json_default: FieldFact
    semantics: FieldSemantics
    provenance: FieldProvenance
    changed: bool | None


def original_field(model, ref, path):
    path = field_path(path)
    if not isinstance(ref, Ref):
        raise TypeError('Field queries require a Ref')
    collection = model.collection(ref.collection)
    record = collection[ref.key]
    owner = Ref(collection=ref.collection, key=collection.spec.key_of(record))
    origin = model._store._origin(owner)
    source = model._source
    args = dict(owner=owner, original=origin.original, path=path)
    if origin.kind != 'inp' or source is None:
        return FieldProvenance(**args, status=origin.kind if origin.kind != 'inp' else 'untracked')
    original = source.original_records.get(origin.original)
    args.update(source=source.decoded.document.source, source_sha256=source.source_sha256)
    if original is None:
        return FieldProvenance(**args, status='unavailable')
    try:
        _, value = field_at(original, path)
    except KeyError:
        return FieldProvenance(**args, status='absent_path')
    declarations = source.field_declarations.get((origin.original, path), ())
    covered = (origin.original, path) in source.field_coverage
    # Unclaimed/malformed known syntax prevents an omission or completeness claim.
    status = ('explicit' if any(v.role in ('value', 'marker') for v in declarations)
              else 'derived') if declarations else 'omitted' if covered else 'unknown'
    if not covered:
        status = 'unknown'
    return FieldProvenance(**args, status=status, value=FieldFact(status='known', value=value),
                           declarations=declarations)


def inspect_field(model, ref, path):
    from ..schema.field_contracts import FieldContext
    path = field_path(path)
    if not isinstance(ref, Ref):
        raise TypeError('Field queries require a Ref')
    collection = model.collection(ref.collection)
    record = collection[ref.key]
    owner = Ref(collection=ref.collection, key=collection.spec.key_of(record))
    parent, value = field_at(record, path)
    constructor = FieldFact(status='not_applicable', reason='A tuple item has no constructor field default')
    json_default = FieldFact()
    semantics = FieldSemantics()
    resolver = None
    if type(path[-1]) is str:
        attr = next(f for f in fields(parent) if f.name == path[-1])
        if attr.default is not MISSING:
            constructor = FieldFact(status='known', value=attr.default)
        elif attr.default_factory is not MISSING:
            constructor = FieldFact(reason='Constructor default factory is not executed by inspection')
        else:
            constructor = FieldFact(status='required')
        declaration = next((d for d in model._schema.json_types.declarations if d.value_type is type(parent)), None)
        wire = next((f for f in declaration.fields if f.attribute == attr.name), None) if declaration else None
        if wire is not None:
            defaults = dict(declaration.defaults)
            json_default = (FieldFact(status='known', value=defaults[wire.name]) if wire.name in defaults
                            else FieldFact(status='required'))
        rule = model._schema.field_rule(type(parent), attr.name, model.profile, root_type=type(record))
        if rule is not None:
            resolver, name = rule.resolve, attr.name
    elif len(path) > 1 and type(path[-2]) is str:
        container, _ = field_at(record, path[:-1])
        rule = model._schema.field_rule(type(container), path[-2], model.profile, root_type=type(record))
        if rule is not None and rule.resolve_item is not None:
            resolver, name, parent = rule.resolve_item, path[-2], container
            json_default = FieldFact(status='not_applicable', reason='A tuple item has no independent JSON field default')
    if resolver is not None:
        semantics = resolver(FieldContext(owner=owner, path=path, record=record,
            container=parent, field=name, value=value, store=model._store, profile=model.profile,
            resource_uses=model.resource_uses))
        if not isinstance(semantics, FieldSemantics):
            raise TypeError('Field resolvers must return FieldSemantics')
        if semantics.diagnostics:
            from dataclasses import replace
            from .diagnostics import DiagnosticResolver
            resolver = DiagnosticResolver(model)
            semantics = replace(semantics, diagnostics=tuple(resolver.resolve(issue) for issue in semantics.diagnostics))
    provenance = original_field(model, ref, path)
    changed = None
    if provenance.value.status == 'known':
        original = model._source.original_records[provenance.original]
        # Record identity does not establish identities for unnamed sequence items.
        # A variant switch likewise cannot establish correspondence of equal names.
        same_types = type(original) is type(record) and all(
            type(field_at(original, path[:i])[0]) is type(field_at(record, path[:i])[0])
            for i in range(1, len(path) + 1))
        if any(type(p) is int for p in path) or not same_types:
            provenance = FieldProvenance(owner=owner, original=provenance.original, path=path,
                                         status='untracked_path')
        else:
            changed = not _same_value(value, provenance.value.value)
    return FieldInfo(owner=owner, path=path, value=value, constructor_default=constructor,
        json_default=json_default, semantics=semantics, provenance=provenance, changed=changed)
