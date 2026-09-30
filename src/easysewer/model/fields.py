"""Local field metadata used for validation and discoverable dimensions.

This metadata is not an automatic external JSON schema. Format codecs must
explicitly declare their serialized field names and version migrations.
"""

from dataclasses import MISSING, field, fields, is_dataclass, replace
from functools import lru_cache
from datetime import date
import math
import types
from typing import Any, Literal, Union, get_args, get_origin, get_type_hints

from ..validation import Diagnostic, DiagnosticSubject, Severity
from ..validation._cooperative import checkpointed
from .identity import Ref


def number(dimension: str, default=MISSING, *, minimum=None, maximum=None, positive=False, integer=False,
           markers=()):
    return field(default=default, metadata={
        "dimension": dimension, "numeric": True, "minimum": minimum,
        "maximum": maximum, "positive": positive, "integer": integer,
        "optional": default is None,
        "markers": markers,
    })


def reference(collection: str, default=MISSING):
    return field(default=default, metadata={"reference": collection, "optional": default is None})


def _finite_numeric(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        # Python integers can exceed the engine's floating-point range.
        return False


@lru_cache(maxsize=None)
def _annotations(cls):
    return get_type_hints(cls)


def _matches(value, annotation):
    if annotation is Any:
        return True
    origin, args = get_origin(annotation), get_args(annotation)
    if origin in (Union, types.UnionType):
        return any(_matches(value, item) for item in args)
    if origin is Literal:
        return any(type(value) is type(item) and value == item for item in args)
    if origin is tuple:
        if not isinstance(value, tuple):
            return False
        if len(args) == 2 and args[1] is Ellipsis:
            return all(_matches(item, args[0]) for item in checkpointed(value))
        return len(value) == len(args) and all(_matches(item, kind) for item, kind in zip(value, args))
    if annotation is float:
        return type(value) in (float, int)
    if annotation in (bool, int, date):
        return type(value) is annotation
    return isinstance(value, annotation)


def validate_fields(record):
    """Validate finite local values and reference namespaces recursively."""
    identity = getattr(record, "id", getattr(record, "record_id", None))

    def visit(value, path="", typed_path=()):
        if isinstance(value, tuple):
            for index, child in enumerate(checkpointed(value)):
                yield from visit(child, f"{path}[{index}]", typed_path + (index,))
        elif is_dataclass(value) and not isinstance(value, Ref):
            locally_valid = True
            annotations = _annotations(type(value))
            for item in fields(value):
                child = getattr(value, item.name)
                name = f"{path}.{item.name}" if path else item.name
                meta = item.metadata
                error = None
                if not _matches(child, annotations[item.name]):
                    error = f"Invalid value type for {item.name}"
                elif child is None:
                    if meta and not meta.get("optional", False):
                        error = "A required value is missing"
                elif meta.get("numeric"):
                    if any(child is marker for marker in meta.get("markers", ())):
                        pass
                    elif not _finite_numeric(child):
                        error = "Expected a finite numeric value"
                    elif meta.get("integer") and (not isinstance(child, int)):
                        error = "Expected an integer"
                    elif meta.get("positive") and child <= 0:
                        error = "Value must be positive"
                    elif meta.get("minimum") is not None and child < meta["minimum"]:
                        error = f"Value must be at least {meta['minimum']}"
                    elif meta.get("maximum") is not None and child > meta["maximum"]:
                        error = f"Value must be at most {meta['maximum']}"
                elif "reference" in meta and (
                    not isinstance(child, Ref) or child.collection != meta["reference"]
                ):
                    error = f"Expected a reference to {meta['reference']}"
                if error:
                    locally_valid = False
                    yield Diagnostic(code="model.invalid_field", message=error,
                                     object_id=identity, field=name,
                                     subject=DiagnosticSubject(path=typed_path + (item.name,)))
                nested = tuple(visit(child, name, typed_path + (item.name,)))
                if any(issue.severity == Severity.ERROR for issue in nested):
                    locally_valid = False
                yield from nested
            validator = getattr(value, "validate_local", None)
            if validator and locally_valid:
                for issue in validator():
                    from .diagnostics import local_path
                    suffix = issue.field or ""
                    name = f"{path}.{suffix}" if path and suffix else path or suffix
                    subject = issue.subject
                    if subject is None or subject.collection is None:
                        subject = DiagnosticSubject(path=typed_path + (subject.path if subject else local_path(suffix)))
                    yield replace(issue, object_id=identity, field=name or None, subject=subject)

    yield from visit(record)
