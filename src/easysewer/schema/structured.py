"""Graph feature codecs and stable ownership keys for semantic INP editing."""

from dataclasses import dataclass
from typing import Iterable, Protocol

from ..model.identity import Ref, canonical_key, namespace_key, require_immutable
from ..model.store import CollectionSpec, RecordStore
from ..model.units import UnitTransform
from ..model.pollutant_units import PollutantUnitTransform
from ..model.usage import ResourceUse
from ..model.inspection import field_path
from .field_contracts import FieldRule
from ..validation import Diagnostic
from .profiles import SwmmProfile
from .registry import DecodedFeature, FeatureDescriptor, RegistryError, SchemaRegistry
from ..validation._cooperative import checkpointed


@dataclass(frozen=True, kw_only=True)
class RecordEntry:
    collection: str
    value: object

    def __post_init__(self):
        namespace_key(self.collection)
        require_immutable(self.value)


@dataclass(frozen=True, kw_only=True)
class SourceBinding:
    line: int
    key: tuple[str, ...]

    def __post_init__(self):
        if type(self.line) is not int or self.line < 1:
            raise ValueError("Source line must be a positive integer")
        canonical_key(self.key)


@dataclass(frozen=True, kw_only=True)
class FieldCoverage:
    """The codec completely understood this original field's input syntax."""
    owner: Ref
    path: tuple

    def __post_init__(self):
        if not isinstance(self.owner, Ref):
            raise TypeError('Field coverage requires a record Ref')
        object.__setattr__(self, 'path', field_path(self.path))


@dataclass(frozen=True, kw_only=True)
class FieldBinding(FieldCoverage):
    line: int
    tokens: tuple[int, ...]
    role: str = 'value'
    contributes: bool = True

    def __post_init__(self):
        super().__post_init__()
        if type(self.line) is not int or self.line < 1:
            raise ValueError('Field bindings require a physical source line')
        if (not isinstance(self.tokens, tuple) or not self.tokens or
                any(type(i) is not int or i < 0 for i in self.tokens) or len(set(self.tokens)) != len(self.tokens)):
            raise ValueError('Field bindings require distinct nonnegative token indexes')
        if self.role not in ('value', 'marker', 'derived', 'retained') or type(self.contributes) is not bool:
            raise ValueError('Invalid field source role or contribution flag')


@dataclass(frozen=True, kw_only=True)
class FieldLineBinding(FieldCoverage):
    """A complete physical line in an explicitly declared raw-text section."""
    line: int
    role: str = 'value'
    contributes: bool = True

    def __post_init__(self):
        super().__post_init__()
        if type(self.line) is not int or self.line < 1:
            raise ValueError('Field bindings require a physical source line')
        if self.role not in ('value', 'marker', 'derived', 'retained') or type(self.contributes) is not bool:
            raise ValueError('Invalid field source role or contribution flag')


@dataclass(frozen=True, kw_only=True)
class FeatureData:
    records: tuple[RecordEntry, ...] = ()
    bindings: tuple[SourceBinding, ...] = ()
    field_bindings: tuple[FieldBinding | FieldLineBinding, ...] = ()
    field_coverage: tuple[FieldCoverage, ...] = ()

    def __post_init__(self):
        require_immutable(self)
        if (any(not isinstance(v, (FieldBinding, FieldLineBinding)) for v in self.field_bindings) or
                any(type(v) is not FieldCoverage for v in self.field_coverage)):
            raise TypeError('Field declarations require FieldBinding/FieldLineBinding and FieldCoverage values')
        if len({item.line for item in self.bindings}) != len(self.bindings):
            raise ValueError("A source line may have only one binding")
        # Repeated source assignments may bind to one effective semantic row.
        # Changed occurrences are collapsed by their owning writer.


@dataclass(frozen=True, kw_only=True)
class EncodedRow:
    key: tuple[str, ...]
    section: str
    values: tuple[str, ...]
    owners: tuple[Ref, ...]
    raw_text: str | None = None


@dataclass(frozen=True, kw_only=True)
class OmittedRecord:
    """An explicitly empty/default configuration requiring no INP data row."""
    owner: Ref
    reason: str


@dataclass(frozen=True, kw_only=True)
class FeatureEncoding:
    rows: tuple[EncodedRow, ...] = ()
    omitted: tuple[OmittedRecord, ...] = ()


class StructuredCodec(Protocol):
    collections: tuple[CollectionSpec, ...]

    def decode(self, document, profile: SwmmProfile) -> DecodedFeature[FeatureData]: ...
    def encode(self, store: RecordStore, profile: SwmmProfile) -> Iterable[EncodedRow] | FeatureEncoding: ...
    def validate(self, store: RecordStore, profile: SwmmProfile) -> Iterable[Diagnostic]: ...


class ModelSchema(SchemaRegistry):
    """Explicit graph/codec bundle. Parser-only registries remain usable separately."""

    def __init__(self):
        super().__init__()
        self._codecs = {}
        self._unit_types = {}
        self._pollutant_unit_types = {}
        self._json_declarations = ()
        self._field_rules = {}

    def register_json(self, *declarations):
        from ..io.json.types import JsonTypes
        checked = JsonTypes((*self._json_declarations, *declarations))
        self._json_declarations = checked.declarations

    @property
    def json_types(self):
        from ..io.json.types import JsonTypes
        return JsonTypes(self._json_declarations)

    def register(self, descriptor: FeatureDescriptor, decoder: StructuredCodec) -> None:
        if not callable(getattr(decoder, "encode", None)) or not callable(getattr(decoder, "validate", None)):
            raise TypeError("Model feature codecs must provide encode and validate")
        if not isinstance(getattr(decoder, "collections", None), tuple):
            raise TypeError("Model feature codecs must declare collection specifications")
        guards = getattr(decoder, "mutation_guards", ())
        if not isinstance(guards, tuple) or any(not callable(guard) for guard in guards):
            raise TypeError("Mutation guards must be an explicit tuple of callables")
        transforms = getattr(decoder, "unit_transforms", ())
        if not isinstance(transforms, tuple) or any(not isinstance(item, UnitTransform) for item in transforms):
            raise TypeError("Unit transforms must be an explicit tuple of UnitTransform values")
        seen = set(self._unit_types)
        for item in transforms:
            if item.value_type in seen:
                raise RegistryError(f"Conflicting unit transforms for {item.value_type.__name__}")
            seen.add(item.value_type)
        pollutant_transforms = getattr(decoder, 'pollutant_unit_transforms', ())
        if not isinstance(pollutant_transforms, tuple) or any(not isinstance(item, PollutantUnitTransform) for item in pollutant_transforms):
            raise TypeError('Pollutant-unit transforms must be an explicit tuple of PollutantUnitTransform values')
        seen = set(self._pollutant_unit_types)
        for item in pollutant_transforms:
            if item.value_type in seen:
                raise RegistryError(f'Conflicting pollutant-unit transforms for {item.value_type.__name__}')
            seen.add(item.value_type)
        rules = getattr(decoder, 'field_rules', ())
        if not isinstance(rules, tuple) or any(not isinstance(rule, FieldRule) for rule in rules):
            raise TypeError('Field contracts must be an explicit tuple of FieldRule values')
        seen = set(self._field_rules)
        for rule in rules:
            key = (rule.value_type, rule.field, rule.root_type)
            if key in seen:
                raise RegistryError(f'Conflicting field contract for {rule.value_type.__name__}.{rule.field}')
            seen.add(key)
        super().register(descriptor, decoder)
        self._codecs[descriptor.key] = decoder
        self._unit_types.update((item.value_type, descriptor.key) for item in transforms)
        self._pollutant_unit_types.update((item.value_type, descriptor.key) for item in pollutant_transforms)
        self._field_rules.update(((rule.value_type, rule.field, rule.root_type), (descriptor, rule)) for rule in rules)

    def field_rule(self, value_type, field, profile, *, root_type=None):
        # A scoped declaration intentionally overrides a general type rule only
        # for its exact record root. Inactive profile rules do not shadow it.
        for root in (root_type, None) if root_type is not None else (None,):
            entry = self._field_rules.get((value_type, field, root))
            if entry is not None and profile.key in entry[0].profiles:
                return entry[1]
        return None

    def field_rules(self, profile):
        """All explicit rules, retaining root scopes for coverage inspection."""
        return tuple(rule for descriptor, rule in self._field_rules.values() if profile.key in descriptor.profiles)

    @property
    def bindings(self):
        return tuple((descriptor, self._codecs[descriptor.key]) for descriptor, _ in self._ordered())

    def unit_transforms(self, profile):
        return tuple(rule for descriptor, codec in self.bindings if profile.key in descriptor.profiles
                     for rule in getattr(codec, "unit_transforms", ()))

    def resource_uses(self, store, profile):
        result = []
        for descriptor, codec in self.bindings:
            if profile.key in descriptor.profiles and callable(getattr(codec, "resource_uses", None)):
                for use in codec.resource_uses(store, profile):
                    if not isinstance(use, ResourceUse):
                        raise RegistryError("Resource-use hooks must yield ResourceUse values")
                    result.append(use)
        return tuple(result)

    def pollutant_unit_transforms(self, profile):
        return tuple(rule for descriptor, codec in self.bindings if profile.key in descriptor.profiles
                     for rule in getattr(codec, 'pollutant_unit_transforms', ()))

    def new_store(self) -> RecordStore:
        specs = {}
        for _, codec in self.bindings:
            for spec in codec.collections:
                if spec.key in specs and specs[spec.key] != spec:
                    raise RegistryError(f"Conflicting collection specifications: {spec.key}")
                specs[spec.key] = spec
        guards = tuple(guard for _, codec in self.bindings for guard in getattr(codec, "mutation_guards", ()))
        return RecordStore(specs.values(), guards=guards)

    def file_uses(self, store, profile):
        from ..model.file_resources import FileUse, file_references
        found = {}
        for descriptor, codec in checkpointed(self.bindings):
            if profile.key in descriptor.profiles and callable(getattr(codec, "file_uses", None)):
                for use in checkpointed(codec.file_uses(store, profile)):
                    if not isinstance(use, FileUse) or not store.contains(use.owner):
                        raise RegistryError("File-use hooks require FileUse values with existing owners")
                    record = store.collection(use.owner.collection)[use.owner.key]
                    if (use.path, use.file) not in tuple(file_references(record)):
                        raise RegistryError("File-use path does not address its declared FileReference")
                    key = use.owner.canonical, use.path
                    if key in found:
                        raise RegistryError("A file field has multiple consumer declarations")
                    found[key] = use
        # Undeclared extension file fields remain visible and cannot pretend to
        # have had their format verified by a known consumer.
        for spec in checkpointed(store.specifications):
            for key, record in checkpointed(store.collection(spec.key).items()):
                owner = Ref(collection=spec.key, key=key)
                for path, file in checkpointed(file_references(record)):
                    found.setdefault((owner.canonical, path), FileUse(owner=owner, path=path, file=file,
                        role="core:undeclared", format="core:unknown", access="read" if file.direction == "input" else "write"))
        return tuple(found.values())

    def snapshot(self) -> "ModelSchema":
        result = ModelSchema()
        for descriptor, codec in self.bindings:
            result.register(descriptor, codec)
        result.register_json(*self._json_declarations)
        return result
