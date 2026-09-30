"""Consumer-declared resource purposes, kept separate from resource ownership."""

from dataclasses import dataclass

from ..validation import Diagnostic, DiagnosticSubject
from .identity import Ref, references, require_immutable


@dataclass(frozen=True, kw_only=True)
class ResourceUse:
    owner: Ref
    target: Ref
    path: tuple[str | int, ...]
    role: str
    dimensions: tuple[str, ...] = ()
    accepted_kinds: tuple[str, ...] = ()

    def __post_init__(self):
        require_immutable(self)
        if not isinstance(self.owner, Ref) or not isinstance(self.target, Ref):
            raise TypeError("Resource-use owner and target must be Ref values")
        if (not isinstance(self.role, str) or not self.role or not isinstance(self.path, tuple) or not self.path
                or any(type(part) not in (str, int) for part in self.path)):
            raise ValueError("Resource uses require a role and exact reference path")
        if any(not isinstance(values, tuple) or any(not isinstance(item, str) or not item for item in values)
               for values in (self.dimensions, self.accepted_kinds)):
            raise TypeError("Resource dimensions and accepted kinds must be tuples of names")


def validate_uses(store, uses):
    dimensions = {}
    for use in uses:
        if not store.contains(use.owner):
            raise ValueError(f"Resource-use owner does not exist: {use.owner}")
        owner = store.collection(use.owner.collection)[use.owner.key]
        if not any(path == use.path and ref.canonical == use.target.canonical for path, ref in references(owner)):
            raise ValueError(f"Resource-use path does not address its target: {use.role}")
        if not store.contains(use.target):
            continue  # The graph reports unresolved references once.
        target = store.collection(use.target.collection)[use.target.key]
        subject = DiagnosticSubject(collection=use.owner.collection, key=use.owner.key, path=use.path)
        resource = DiagnosticSubject(collection=use.target.collection, key=use.target.key)
        if use.accepted_kinds and getattr(target, "kind", None) not in use.accepted_kinds:
            yield Diagnostic(code="resource.wrong_purpose", message=f"{use.role} requires {use.accepted_kinds}",
                             object_id=str(use.owner.key), field=".".join(str(part) for part in use.path),
                             subject=subject, related=(resource,))
        if hasattr(target, "points") and not target.points:
            yield Diagnostic(code="resource.empty_used_resource", message=f"{use.role} requires nonempty data",
                             object_id=str(use.target.key), field="points",
                             subject=DiagnosticSubject(collection=use.target.collection, key=use.target.key, path=('points',)),
                             related=(subject,))
        if use.dimensions:
            before, first = dimensions.setdefault(use.target.canonical, (use.dimensions, subject))
            if before != use.dimensions:
                yield Diagnostic(code="resource.conflicting_dimensions",
                    message=f"Shared resource has incompatible consumer dimensions {before} and {use.dimensions}",
                    object_id=str(use.target.key), subject=resource, related=(first,subject))
