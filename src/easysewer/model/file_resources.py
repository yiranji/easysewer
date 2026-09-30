"""Declared file consumers shared by exporters and runtime preflight."""

from dataclasses import dataclass, fields, is_dataclass, replace

from .identity import Ref, namespace_key
from .values import FileReference
from ..validation import DiagnosticSubject
from ..validation._cooperative import checkpointed


def file_subject(use):
    """Address the declared filename, never the contents of the external file."""
    return DiagnosticSubject(collection=use.owner.collection, key=use.owner.key,
                             path=(*use.path, 'path'))


def file_diagnostic(issue, use, resolver, *, source=None):
    """Keep inspector content positions separate from the INP declaration."""
    subject = file_subject(use)
    unbound = issue.subject is None or issue.subject.collection is None
    span = issue.span
    if unbound and span is not None and span.source is None and source is not None:
        span = replace(span, source=source)
    issue = replace(issue, span=span,
        subject=subject if unbound else issue.subject,
        related=issue.related if unbound or issue.subject == subject else
            tuple(dict.fromkeys((*issue.related, subject))))
    resolved = resolver.resolve(issue)
    # Inspectors may already carry evidence from a rejected candidate. Preserve
    # that evidence while adding the known declaration, rather than relabelling
    # it from the current graph.
    existing = {location.subject: location for location in checkpointed(issue.locations)}
    return replace(resolved, locations=tuple(existing.get(location.subject, location)
        for location in checkpointed(resolved.locations)))


@dataclass(frozen=True, kw_only=True)
class FileUse:
    owner: Ref
    path: tuple[str | int, ...]
    file: FileReference
    role: str
    format: str
    base: str = "document"
    kind: str = "file"
    access: str = "read"
    required: bool = True
    active: bool = True

    def __post_init__(self):
        namespace_key(self.role); namespace_key(self.format)
        if not isinstance(self.owner, Ref) or not isinstance(self.file, FileReference):
            raise TypeError("File consumers require a model owner and FileReference")
        if not isinstance(self.path, tuple) or not self.path or any(type(p) not in (str, int) for p in checkpointed(self.path)):
            raise ValueError("File consumer requires an exact nonempty field path")
        if self.base not in ("document", "working_directory") or self.kind not in ("file", "directory"):
            raise ValueError("Invalid file base or resource kind")
        if self.access not in ("read", "read_write", "write") or type(self.required) is not bool or type(self.active) is not bool:
            raise ValueError("Invalid file consumer access/requirement")


def file_references(value, path=()):
    if isinstance(value, FileReference):
        yield path, value
    elif is_dataclass(value):
        for item in checkpointed(fields(value)):
            yield from file_references(getattr(value, item.name), (*path, item.name))
    elif isinstance(value, tuple):
        for index, item in checkpointed(enumerate(value)):
            yield from file_references(item, (*path, index))


def replace_path(value, path, replacement):
    if not path:
        return replacement
    head, *tail = path
    if isinstance(head, int):
        result = list(value); result[head] = replace_path(value[head], tuple(tail), replacement)
        return tuple(result)
    return replace(value, **{head: replace_path(getattr(value, head), tuple(tail), replacement)})


def resolve_files(model, *, input_directory=None, working_directory=None):
    """Return an independent model with explicit absolute paths; perform no IO."""
    result = model.copy()
    with result._store._permit_context_change("file_rebase"):
        for use in checkpointed(model.file_uses()):
            base = working_directory if use.base == "working_directory" else input_directory
            try:
                resolved = use.file.resolve(relative_to=base)
            except ValueError as error:
                from ..validation import Diagnostic, ValidationError, ValidationReport
                report = model._resolve_diagnostics(ValidationReport(diagnostics=(Diagnostic(
                    code='files.resolve_path', message=str(error), subject=file_subject(use)),)))
                raise ValidationError(report) from error
            reference = replace(use.file, path=str(resolved), base_directory=None)
            rows = result.collection(use.owner.collection)
            rows.replace(use.owner.key, replace_path(rows[use.owner.key], use.path, reference))
    return result
