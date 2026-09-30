"""Pure domain primitives for the candidate 2.0 model."""

from .identity import Ref, ReferenceUse
from .store import CollectionSpec, OpaqueConstraint, RecordCollection, RecordConflictError, RecordStore
from .units import UnitContext, UnitRules
from .values import FileReference, Offset, Point
from .provenance import RecordProvenance, SourceRecord
from .inspection import FieldFact, FieldSemantics, FieldDeclaration, FieldProvenance, FieldInfo


def __getattr__(name):
    if name == "Model":
        from .facade import Model
        return Model
    raise AttributeError(name)

__all__ = [
    "Ref", "ReferenceUse", "CollectionSpec", "OpaqueConstraint", "RecordCollection",
    "RecordConflictError", "RecordStore", "UnitContext", "UnitRules", "FileReference", "Offset", "Point", "Model",
    "RecordProvenance", "SourceRecord",
    "FieldFact", "FieldSemantics", "FieldDeclaration", "FieldProvenance", "FieldInfo",
]
