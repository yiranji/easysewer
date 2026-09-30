"""Explicit sequential migrations with computed changes and unmapped paths."""

from dataclasses import dataclass
import json

from ...validation import Diagnostic, Severity, ValidationReport
from .document import JsonDocument, fail
from .model import version


@dataclass(frozen=True, kw_only=True)
class MigrationOutput:
    document: JsonDocument
    unmapped: tuple[str, ...] = ()


@dataclass(frozen=True, kw_only=True)
class MigrationChange:
    path: str
    operation: str
    before_json: str | None
    after_json: str | None


@dataclass(frozen=True, kw_only=True)
class MigrationResult:
    document: JsonDocument
    changes: tuple[MigrationChange, ...]
    unmapped: tuple[str, ...]
    steps: tuple[tuple[str,str], ...]

    @property
    def report(self):
        issues = [Diagnostic(code="json.migration_applied", message=f"Applied schema migration {before} to {after}",
                             feature="easysewer:json", severity=Severity.INFO) for before,after in self.steps]
        issues.extend(Diagnostic(code="json.migration_unmapped", message="Migration retained an unmapped value",
                                 field=path, feature="easysewer:json", severity=Severity.WARNING) for path in self.unmapped)
        return ValidationReport(diagnostics=tuple(issues))


def _changes(before, after, path="$"):
    def text(value):
        return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True)
    if type(before) is dict and type(after) is dict:
        for key in before.keys() | after.keys():
            child = path+"/"+key.replace("~","~0").replace("/","~1")
            if key not in before:
                yield MigrationChange(path=child, operation="add", before_json=None, after_json=text(after[key]))
            elif key not in after:
                yield MigrationChange(path=child, operation="remove", before_json=text(before[key]), after_json=None)
            else:
                yield from _changes(before[key],after[key],child)
    elif type(before) is not type(after) or before != after:
        yield MigrationChange(path=path, operation="replace", before_json=text(before), after_json=text(after))


class MigrationRegistry:
    def __init__(self):
        self._steps = {}

    def register(self, source, target, transform):
        if source in self._steps or version(target) <= version(source) or not callable(transform):
            raise ValueError("Migration requires a unique source, increasing target and explicit callable")
        self._steps[source] = target, transform

    def upgrade(self, document, *, target):
        before = document.data
        initial = before.get("schema_version")
        if version(initial) > version(target):
            fail("json.migration_direction", "A schema migration cannot silently downgrade a document")
        current, steps, unmapped = document, [], []
        while current.data.get("schema_version") != target:
            source = current.data.get("schema_version")
            step = self._steps.get(source)
            if step is None or version(step[0]) > version(target):
                fail("json.migration_missing", f"No migration path from {source} to {target}")
            following, transform = step
            output = transform(current)
            if not isinstance(output, MigrationOutput) or not isinstance(output.document, JsonDocument):
                raise TypeError("Migration callbacks must return MigrationOutput")
            if not isinstance(output.unmapped, tuple) or any(type(path) is not str for path in output.unmapped):
                raise TypeError("Unmapped paths must be an immutable tuple of strings")
            after = output.document.data
            if after.get("schema_version") != following or after.get("kind") != before.get("kind"):
                fail("json.migration_contract", "Migration output has an unexpected version or document kind")
            steps.append((source,following))
            unmapped.extend(output.unmapped)
            current = output.document
        changes = tuple(sorted(_changes(before,current.data),key=lambda value:value.path))
        return MigrationResult(document=current,changes=changes,unmapped=tuple(dict.fromkeys(unmapped)),steps=tuple(steps))
