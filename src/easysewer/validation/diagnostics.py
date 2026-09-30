"""Pure Python diagnostics; no dependency on the model facade or native engine."""

from dataclasses import dataclass
from enum import Enum
import re


class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True, kw_only=True)
class SourceSpan:
    """One-based character columns, with an exclusive end, on one source line."""

    line: int
    column: int
    end_column: int
    source: str | None = None

    def __post_init__(self):
        if self.line < 1 or self.column < 1 or self.end_column < self.column:
            raise ValueError("Invalid source span")


@dataclass(frozen=True, kw_only=True)
class DiagnosticSubject:
    """A typed model address, or an unbound local-validator field path."""

    collection: str | None = None
    key: str | tuple[str, ...] | None = None
    path: tuple[str | int, ...] = ()

    def __post_init__(self):
        if (self.collection is None) != (self.key is None):
            raise ValueError('Diagnostic collection and key must be supplied together')
        if self.collection is not None:
            if type(self.collection) is not str or not re.fullmatch(r'[a-z][a-z0-9_.-]*:[a-z][a-z0-9_.-]*', self.collection):
                raise ValueError('Diagnostic collection must be namespaced')
            parts = self.key if type(self.key) is tuple else (self.key,)
            if not parts or any(type(p) is not str or not p or '\x00' in p for p in parts):
                raise ValueError('Diagnostic keys require nonempty string components')
        if type(self.path) is not tuple or any(not (
                type(p) is str and p and not p.startswith('_') or type(p) is int and p >= 0) for p in self.path):
            raise ValueError('Diagnostic paths require public names and nonnegative indexes')


@dataclass(frozen=True, kw_only=True)
class DiagnosticLocation:
    """Labelled original source evidence; changed/context spans are not current values."""

    subject: DiagnosticSubject
    status: str
    original: DiagnosticSubject | None = None
    spans: tuple[SourceSpan, ...] = ()
    source_sha256: str | None = None

    def __post_init__(self):
        if type(self.subject) is not DiagnosticSubject or self.subject.collection is None:
            raise TypeError('Locations require a bound diagnostic subject')
        if self.status not in ('current', 'changed', 'context', 'omitted', 'unknown', 'untracked', 'programmatic', 'absent'):
            raise ValueError('Unknown diagnostic source status')
        if self.original is not None and (type(self.original) is not DiagnosticSubject or self.original.collection is None):
            raise TypeError('Original location requires a bound subject')
        if self.original is not None and (self.original.collection != self.subject.collection or self.original.path != self.subject.path):
            raise ValueError('Original diagnostic coordinates must retain collection and field path')
        if type(self.spans) is not tuple or any(type(v) is not SourceSpan for v in self.spans):
            raise TypeError('Diagnostic source spans must be an immutable tuple')
        if self.source_sha256 is not None and (type(self.source_sha256) is not str or not re.fullmatch('[0-9a-f]{64}', self.source_sha256)):
            raise ValueError('Invalid diagnostic source digest')
        if self.spans and (self.original is None or self.source_sha256 is None):
            raise ValueError('Source spans require original identity and source digest')
        if self.status in ('current', 'context') and not self.spans:
            raise ValueError('Located diagnostics require source spans')
        if self.status in ('omitted', 'untracked', 'programmatic', 'absent') and self.spans:
            raise ValueError('Unlocated diagnostic status cannot claim source spans')
        if self.status in ('programmatic', 'absent') and (self.original is not None or self.source_sha256 is not None):
            raise ValueError('A missing or programmatic record cannot claim original source identity')


@dataclass(frozen=True, kw_only=True)
class Diagnostic:
    code: str
    message: str
    severity: Severity = Severity.ERROR
    span: SourceSpan | None = None
    feature: str | None = None
    section: str | None = None
    object_id: str | None = None
    field: str | None = None
    subject: DiagnosticSubject | None = None
    related: tuple[DiagnosticSubject, ...] = ()
    locations: tuple[DiagnosticLocation, ...] = ()

    def __post_init__(self):
        if not self.code or not self.message:
            raise ValueError("Diagnostics require a code and message")
        object.__setattr__(self, "severity", Severity(self.severity))
        if self.subject is not None and type(self.subject) is not DiagnosticSubject:
            raise TypeError('Expected a DiagnosticSubject')
        if type(self.related) is not tuple or any(type(v) is not DiagnosticSubject for v in self.related):
            raise TypeError('Related subjects require an immutable tuple')
        if type(self.locations) is not tuple or any(type(v) is not DiagnosticLocation for v in self.locations):
            raise TypeError('Diagnostic locations require an immutable tuple')
        subjects = (() if self.subject is None else (self.subject,)) + self.related
        if any(v.subject not in subjects for v in self.locations):
            raise ValueError('Location does not belong to a diagnostic subject')


@dataclass(frozen=True, kw_only=True)
class ValidationReport:
    diagnostics: tuple[Diagnostic, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "diagnostics", tuple(self.diagnostics))
        if any(not isinstance(item, Diagnostic) for item in self.diagnostics):
            raise TypeError("Expected Diagnostic objects")

    @property
    def errors(self) -> tuple[Diagnostic, ...]:
        return tuple(item for item in self.diagnostics if item.severity == Severity.ERROR)

    @property
    def is_valid(self) -> bool:
        return not self.errors

    def raise_for_errors(self) -> None:
        if not self.is_valid:
            raise ValidationError(self)


class ValidationError(ValueError):
    def __init__(self, report: ValidationReport):
        self.report = report
        super().__init__("; ".join(f"{item.code}: {item.message}" for item in report.errors))


def diagnostic_from_data(data, *, locations):
    """Decode strict plain JSON after the containing format selects its version."""
    def exact(value, names):
        if type(value) is not dict or set(value) != set(names.split()):
            raise ValueError('Invalid diagnostic JSON fields')

    def span(value):
        if value is None:
            return None
        exact(value, 'line column end_column source')
        return SourceSpan(**value)

    def subject(value):
        if value is None:
            return None
        exact(value, 'collection key path')
        if type(value['path']) is not list:
            raise TypeError('Diagnostic path must be a JSON array')
        return DiagnosticSubject(collection=value['collection'],
            key=tuple(value['key']) if type(value['key']) is list else value['key'], path=tuple(value['path']))

    def location(value):
        exact(value, 'subject status original spans source_sha256')
        if type(value['spans']) is not list:
            raise TypeError('Diagnostic spans must be a JSON array')
        return DiagnosticLocation(subject=subject(value['subject']), status=value['status'],
            original=subject(value['original']), spans=tuple(span(s) for s in value['spans']),
            source_sha256=value['source_sha256'])

    exact(data, 'code message severity span feature section object_id field' +
          (' subject related locations' if locations else ''))
    values = dict(data, span=span(data['span']))
    if locations:
        if type(data['related']) is not list or type(data['locations']) is not list:
            raise TypeError('Diagnostic relations and locations must be JSON arrays')
        values.update(subject=subject(data['subject']), related=tuple(subject(v) for v in data['related']),
                      locations=tuple(location(v) for v in data['locations']))
    return Diagnostic(**values)
