"""Model identities and immutable values, independent of all file formats."""

from dataclasses import dataclass, fields, is_dataclass, replace
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from enum import Enum
from pathlib import PurePath
import re
from typing import Iterator

from ..validation._cooperative import checkpointed

RecordKey = str | tuple[str, ...]
_ASCII_UPPER = str.maketrans("abcdefghijklmnopqrstuvwxyz", "ABCDEFGHIJKLMNOPQRSTUVWXYZ")
_NAMESPACE = re.compile(r"[a-z][a-z0-9_.-]*:[a-z][a-z0-9_.-]*\Z")
_CAPABILITY = re.compile(r"[a-z][a-z0-9_.-]*:[a-z][a-z0-9_.-]*(?::[a-z0-9][a-z0-9_.-]*)*\Z")


def namespace_key(value: str) -> str:
    if not isinstance(value, str) or not _NAMESPACE.fullmatch(value):
        raise ValueError("Expected a namespaced collection key, e.g. 'swmm:nodes'")
    return value


def capability_key(value: str) -> str:
    """Validate an exact capability ID, including optional version components."""
    if not isinstance(value, str) or not _CAPABILITY.fullmatch(value):
        raise ValueError("Expected a capability key, e.g. 'easysewer:routing-io:1'")
    return value


def canonical_key(value: RecordKey) -> RecordKey:
    """Match SWMM's ASCII-only case-insensitive IDs, preserving non-ASCII text."""
    if isinstance(value, tuple):
        if not value or any(not isinstance(part, str) for part in value):
            raise ValueError("Composite keys require one or more string components")
        return tuple(canonical_key(part) for part in value)
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError("Record keys must be nonempty strings without NUL")
    return value.translate(_ASCII_UPPER)


def validate_identifier(value: str) -> None:
    canonical_key(value)
    if not isinstance(value, str) or value.startswith("[") or any(
        char in value for char in ' \t\r\n;"'
    ):
        raise ValueError(f"ID cannot be represented as an INP identifier: {value!r}")


@dataclass(frozen=True, kw_only=True)
class Ref:
    collection: str
    key: RecordKey

    def __post_init__(self):
        namespace_key(self.collection)
        canonical_key(self.key)

    @property
    def canonical(self) -> "Ref":
        return Ref(collection=self.collection, key=canonical_key(self.key))


@dataclass(frozen=True, kw_only=True)
class ReferenceUse:
    owner: Ref
    target: Ref
    path: tuple[str | int, ...]


def references(value: object, path: tuple[str | int, ...] = ()) -> Iterator[tuple[tuple, Ref]]:
    if isinstance(value, Ref):
        yield path, value
    elif is_dataclass(value) and not isinstance(value, type):
        for item in checkpointed(fields(value)):
            yield from references(getattr(value, item.name), path + (item.name,))
    elif isinstance(value, tuple):
        for index, item in checkpointed(enumerate(value)):
            yield from references(item, path + (index,))


def rewrite_references(value: object, old: Ref, new: Ref) -> object:
    if isinstance(value, Ref):
        return new if value.canonical == old.canonical else value
    if is_dataclass(value) and not isinstance(value, type):
        updates = {}
        for item in fields(value):
            before = getattr(value, item.name)
            after = rewrite_references(before, old, new)
            if after is not before:
                if not item.init:
                    raise TypeError(f"Reference-bearing field must be replaceable: {item.name}")
                updates[item.name] = after
        return replace(value, **updates) if updates else value
    if isinstance(value, tuple):
        after = tuple(rewrite_references(item, old, new) for item in value)
        return value if all(a is b for a, b in zip(value, after)) else after
    return value


def require_immutable(value: object, path: str = "record", _active=None) -> None:
    """Reject mutable extension payloads before they can corrupt graph indexes."""
    if value is None or isinstance(value, (str, bytes, int, float, bool, Decimal, Enum,
                                           date, datetime, time, timedelta, PurePath)):
        return
    active = set() if _active is None else _active
    if id(value) in active:
        raise TypeError(f"{path} contains a cyclic value; use Ref for graph cycles")
    active.add(id(value))
    try:
        if isinstance(value, tuple):
            for index, item in enumerate(value):
                require_immutable(item, f"{path}[{index}]", active)
        elif is_dataclass(value) and not isinstance(value, type):
            if not value.__dataclass_params__.frozen:
                raise TypeError(f"{path} must be a frozen dataclass")
            for item in fields(value):
                require_immutable(getattr(value, item.name), f"{path}.{item.name}", active)
        else:
            raise TypeError(f"{path} contains unsupported or mutable data: {type(value).__name__}")
    finally:
        active.remove(id(value))
