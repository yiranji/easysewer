"""Explicit feature registration and checked ownership of source records.

Decoders can aggregate arbitrarily many lines and sections into typed values.
No universal row schema, implicit plugin discovery, or native imports are used.
Semantic editing/serialization will build on these ownership records; this
module deliberately does not pretend that recognizing a section implements it.
"""

import re
from dataclasses import dataclass, field
from enum import IntEnum
from types import MappingProxyType
from typing import Generic, Mapping, Protocol, TypeVar

from ..io.inp import InpDocument, InpLine
from ..io.inp.document import section_key
from ..validation import Diagnostic, Severity, SourceSpan, ValidationReport
from .profiles import EPA_SWMM_5_2_4, SwmmProfile
from ..validation._cooperative import checkpointed

T = TypeVar("T")
_FEATURE_KEY = re.compile(r"[a-z][a-z0-9_.-]*:[a-z][a-z0-9_.-]*\Z")


class SupportLevel(IntEnum):
    PRESERVED = 1
    STRUCTURED = 2
    VERIFIED = 3


@dataclass(frozen=True, kw_only=True)
class FeatureDescriptor:
    key: str
    sections: frozenset[str]
    support: SupportLevel = SupportLevel.STRUCTURED
    requires: tuple[str, ...] = ()
    profiles: frozenset[str] = frozenset({EPA_SWMM_5_2_4.key})
    atomic_write: bool = False
    ordered_sections: frozenset[str] = frozenset()
    rewrite_after: tuple[str, ...] = ()
    raw_sections: frozenset[str] = frozenset()

    def __post_init__(self):
        if not _FEATURE_KEY.fullmatch(self.key):
            raise ValueError("Feature keys must be namespaced, for example 'swmm:inflows.flow'")
        object.__setattr__(self, "sections", frozenset(section_key(s) for s in self.sections))
        object.__setattr__(self, "ordered_sections", frozenset(section_key(s) for s in self.ordered_sections))
        object.__setattr__(self, "raw_sections", frozenset(section_key(s) for s in self.raw_sections))
        object.__setattr__(self, "requires", tuple(self.requires))
        object.__setattr__(self, "rewrite_after", tuple(self.rewrite_after))
        object.__setattr__(self, "profiles", frozenset(self.profiles))
        object.__setattr__(self, "support", SupportLevel(self.support))
        if not self.sections or not self.profiles:
            raise ValueError("Declare at least one section and profile")
        if len(set(self.requires)) != len(self.requires):
            raise ValueError("Duplicate feature dependencies")
        if not self.ordered_sections <= self.sections:
            raise ValueError("Ordered identity sections must belong to the feature")
        if not self.raw_sections <= self.sections:
            raise ValueError("Raw text sections must belong to the feature")
        if not set(self.rewrite_after) <= set(self.requires):
            raise ValueError("Rewrite-order dependencies must also be declared feature dependencies")


@dataclass(frozen=True, kw_only=True)
class DecodedFeature(Generic[T]):
    """Only claimed records have a structured owner; all others stay opaque.

    Values should be immutable domain objects. Rows that failed to decode must
    stay unclaimed and carry diagnostics instead of being dropped.
    """

    value: T
    claimed_lines: frozenset[int] = frozenset()
    report: ValidationReport = field(default_factory=ValidationReport)

    def __post_init__(self):
        object.__setattr__(self, "claimed_lines", frozenset(self.claimed_lines))
        if any(type(number) is not int or number < 1 for number in self.claimed_lines):
            raise ValueError("Claims must contain positive, one-based line numbers")


class FeatureDecoder(Protocol[T]):
    def decode(self, document: InpDocument, profile: SwmmProfile) -> DecodedFeature[T]:
        """Decode all relevant records, including repeated section occurrences."""
        ...


@dataclass(frozen=True, kw_only=True)
class DecodedDocument:
    document: InpDocument
    profile: SwmmProfile
    features: Mapping[str, DecodedFeature]
    owners: Mapping[int, str]
    descriptors: Mapping[str, FeatureDescriptor]
    report: ValidationReport

    def __post_init__(self):
        # Detach from the mutable builder and protect the provenance snapshot.
        for name in ("features", "owners", "descriptors"):
            object.__setattr__(self, name, MappingProxyType(dict(getattr(self, name))))

    @property
    def opaque_records(self) -> tuple[InpLine, ...]:
        return tuple(line for line in self.document.lines
                     if line.kind in ("data", "raw", "invalid_header")
                     and line.number not in self.owners)

    def support_for(self, line: int) -> SupportLevel:
        """Support belongs to a record/variant, not an entire recognized section."""
        if line < 1 or line > len(self.document.lines):
            raise IndexError("Line numbers are one-based")
        owner = self.owners.get(line)
        return self.descriptors[owner].support if owner else SupportLevel.PRESERVED


class RegistryError(ValueError):
    """Invalid registration, dependencies, or conflicting decoder claims."""


class SchemaRegistry:
    """A local registration builder. Existing decoded snapshots never change."""

    def __init__(self):
        self._features: dict[str, tuple[FeatureDescriptor, FeatureDecoder]] = {}

    def register(self, descriptor: FeatureDescriptor, decoder: FeatureDecoder) -> None:
        if descriptor.key in self._features:
            raise RegistryError(f"Feature already registered: {descriptor.key}")
        if descriptor.support == SupportLevel.PRESERVED:
            raise RegistryError("Opaque records need no decoder; registration requires structured support")
        if not callable(getattr(decoder, "decode", None)):
            raise TypeError("Decoder must implement decode(document, profile)")
        self._features[descriptor.key] = (descriptor, decoder)

    @property
    def descriptors(self) -> tuple[FeatureDescriptor, ...]:
        return tuple(descriptor for descriptor, _ in self._features.values())

    def _ordered(self):
        bindings = dict(self._features)
        result = []
        visited = set()
        visiting = set()

        def visit(key):
            if key in visiting:
                raise RegistryError(f"Cyclic feature dependency: {key}")
            if key in visited:
                return
            if key not in bindings:
                raise RegistryError(f"Missing feature dependency: {key}")
            visiting.add(key)
            descriptor, decoder = bindings[key]
            for dependency in checkpointed(descriptor.requires):
                visit(dependency)
            visiting.remove(key)
            visited.add(key)
            result.append((descriptor, decoder))

        for key in checkpointed(bindings):
            visit(key)
        return result

    def decode(
        self, document: InpDocument, *, profile: SwmmProfile = EPA_SWMM_5_2_4
    ) -> DecodedDocument:
        features = {}
        descriptors = {}
        owners = {}
        issues = list(document.report.diagnostics)
        for descriptor, decoder in checkpointed(self._ordered()):
            if profile.key not in descriptor.profiles or any(
                dependency not in features for dependency in checkpointed(descriptor.requires)
            ):
                issues.append(Diagnostic(
                    code="schema.profile_unsupported", severity=Severity.WARNING,
                    feature=descriptor.key,
                    message=f"Feature or dependency is unavailable for profile {profile.key}; source retained.",
                ))
                continue
            decoded = decoder.decode(document, profile)
            if not isinstance(decoded, DecodedFeature):
                raise TypeError(f"{descriptor.key} did not return DecodedFeature")
            for number in checkpointed(decoded.claimed_lines):
                if number > len(document.lines):
                    raise RegistryError(f"{descriptor.key} claimed nonexistent line {number}")
                line = document.lines[number - 1]
                allowed = ("data", "raw", "blank", "comment") if line.section in descriptor.raw_sections else ("data", "raw")
                if line.kind not in allowed or line.section not in descriptor.sections:
                    raise RegistryError(f"{descriptor.key} claimed a line outside its records: {number}")
                if number in owners:
                    raise RegistryError(f"Line {number} claimed by both {owners[number]} and {descriptor.key}")
                owners[number] = descriptor.key
            issues.extend(decoded.report.diagnostics)
            features[descriptor.key] = decoded
            descriptors[descriptor.key] = descriptor

        # The catalog reports recognition only. Unimplemented known sections are
        # just as opaque as unknown future sections and are never discarded.
        seen = set()
        for section in checkpointed(document.sections):
            if section.name not in profile.sections and section.name not in seen:
                seen.add(section.name)
                issues.append(Diagnostic(
                    code="schema.unknown_section", severity=Severity.INFO,
                    section=section.name,
                    message=f"Section {section.name} is outside profile {profile.key}; source retained.",
                    span=SourceSpan(source=document.source, line=section.header.number,
                                    column=1, end_column=len(section.header.content) + 1),
                ))
        return DecodedDocument(document=document, profile=profile, features=features,
                               owners=owners, descriptors=descriptors,
                               report=ValidationReport(diagnostics=tuple(issues)))
