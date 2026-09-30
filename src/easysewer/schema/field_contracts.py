"""Explicit exact-type contracts for candidate public field inspection."""

from dataclasses import dataclass, fields, is_dataclass
from typing import Callable

from ..model.identity import Ref


@dataclass(frozen=True, kw_only=True)
class FieldRule:
    value_type: type
    field: str
    resolve: Callable
    root_type: type | None = None
    resolve_item: Callable | None = None

    def __post_init__(self):
        if (not isinstance(self.value_type, type) or not is_dataclass(self.value_type) or
                self.field not in {f.name for f in fields(self.value_type)} or not callable(self.resolve)):
            raise TypeError('Field rules require an exact dataclass type, declared field and resolver')
        if self.root_type is not None and (not isinstance(self.root_type, type) or not is_dataclass(self.root_type)):
            raise TypeError('Scoped field rules require an exact dataclass root type')
        if self.resolve_item is not None and not callable(self.resolve_item):
            raise TypeError('Tuple-item field resolvers must be callable')


@dataclass(frozen=True, kw_only=True)
class FieldContext:
    owner: Ref
    path: tuple
    record: object
    container: object
    field: str
    value: object
    store: object
    profile: object
    resource_uses: Callable | None = None
