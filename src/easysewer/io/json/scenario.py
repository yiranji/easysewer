"""Explicit scenario operation codecs; extensions are registered by trusted code."""

from dataclasses import dataclass
import json
import re

from ...model.identity import Ref, namespace_key
from ...scenario import (
    AddRecord, AssertRecord, ChangeContext, ChangePollutantUnits, FieldChange, MoveRecord, OpaqueOperation,
    RemoveRecord, RenameRecord, ReplaceRecord, ScenarioPatch, SetFields,
    apply_context, apply_record, apply_pollutant_units,
)
from .document import JsonDocument, fail
from .types import JsonField, JsonType, JsonTypes, UnknownValue


KIND = "easysewer:scenario"


def check_version(value):
    if type(value) is not str or not re.fullmatch(r"1\.(0|[1-9][0-9]*)", value):
        fail("scenario.schema_version", "Scenario requires supported schema major 1")


@dataclass(frozen=True, kw_only=True)
class OperationSpec:
    key: str
    operation_type: type
    encode: object
    decode: object
    apply: object
    # Independent schema document; references resolve against model $defs.
    json_schema: JsonDocument

    def __post_init__(self):
        namespace_key(self.key)
        if not isinstance(self.operation_type, type) or not all(callable(f) for f in (self.encode, self.decode, self.apply)):
            raise TypeError("Operation codecs require a type and encode/decode/apply callables")
        if type(self.json_schema.data) is not dict:
            raise TypeError("Operation schema must be an object")


class ScenarioRegistry:
    def __init__(self, specs=()):
        self._specs = {}
        for spec in specs:
            self.register(spec)

    def register(self, spec):
        if not isinstance(spec, OperationSpec):
            raise TypeError("Expected OperationSpec")
        if spec.key in self._specs or any(s.operation_type is spec.operation_type for s in self._specs.values()):
            raise ValueError("Conflicting scenario operation registration")
        self._specs[spec.key] = spec

    @property
    def specifications(self):
        return tuple(self._specs.values())

    @property
    def capabilities(self):
        return tuple(self._specs)

    def snapshot(self):
        return ScenarioRegistry(self.specifications)

    def for_value(self, value):
        for spec in self._specs.values():
            if type(value) is spec.operation_type:
                return spec
        fail("scenario.operation_type", f"Unregistered operation type {type(value).__name__}")

    def decode(self, data, types):
        if type(data) is not dict or type(data.get("type")) is not str:
            fail("scenario.operation_type", "Operation needs a string type discriminator")
        spec = self._specs.get(data["type"])
        if spec is None:
            return OpaqueOperation(document=JsonDocument.from_data(data))
        try:
            operation = spec.decode(data, types)
        except UnknownValue:
            return OpaqueOperation(document=JsonDocument.from_data(data))
        if type(operation) is not spec.operation_type:
            raise TypeError("Operation decoder returned the wrong declared type")
        return operation


def _value_schema():
    return {"anyOf": [{"type": t} for t in ("null", "boolean", "number", "string")]
        + [{"type": "array", "items": {"$ref": "#/$defs/scenario_value"}},
           {"type": "object", "required": ["type"], "properties": {"type": {"type": "string"}}}]}


def _declarations():
    target = JsonField(name="target", attribute="target", shape=("object", "core:ref"))
    key = ("union", ("string",), ("array", ("string",)), ("null",))
    return (
        JsonType(key="scenario:remove", value_type=RemoveRecord, fields=(target,
            JsonField(name="cascade", attribute="cascade", shape=("boolean",))), defaults=(("cascade", False),)),
        JsonType(key="scenario:rename", value_type=RenameRecord, fields=(target,
            JsonField(name="new_id", attribute="new_id", shape=("string",)))),
        JsonType(key="scenario:move", value_type=MoveRecord, fields=(target,
            JsonField(name="before", attribute="before", shape=key)), defaults=(("before", None),)),
        JsonType(key="scenario:context", value_type=ChangeContext, fields=(
            JsonField(name="context", attribute="context", shape=("literal", "flow_units", "link_offsets", "force_main_equation")),
            JsonField(name="value", attribute="value", shape=("string",)),
            JsonField(name="mode", attribute="mode", shape=("literal", "convert", "reinterpret")),
            JsonField(name="basis", attribute="basis", shape=("literal", "engine", "physical"))),
            defaults=(("mode", "convert"), ("basis", "engine"))),
        JsonType(key='scenario:pollutant_units', value_type=ChangePollutantUnits, fields=(target,
            JsonField(name='units', attribute='units', shape=('literal', 'MG/L', 'UG/L', '#/L')),
            JsonField(name='mode', attribute='mode', shape=('literal', 'convert', 'reinterpret'))),
            defaults=(('mode', 'convert'),)),
    )


def _simple_spec(declaration):
    from .catalog import builtin_types
    def codecs(types):
        return JsonTypes((*types.declarations, declaration))
    def encode(value, types):
        return codecs(types).encode(value)
    def decode(data, types):
        extras = []
        value = codecs(types).decode(data, extras=extras)
        if extras:
            raise UnknownValue("Unknown operation fields")
        return value
    return OperationSpec(key=declaration.key, operation_type=declaration.value_type,
        encode=encode, decode=decode, apply={ChangeContext: apply_context, ChangePollutantUnits: apply_pollutant_units}.get(declaration.value_type, apply_record),
        json_schema=JsonDocument.from_data(JsonTypes((*builtin_types(), declaration)).schema()[declaration.key]))


def _record_spec(key, cls):
    def encode(value, types):
        if not isinstance(value.target, Ref):
            fail("scenario.target", "Operation requires a Ref target")
        return {"type": key, "target": types.encode(value.target), "value": types.encode(value.value)}
    def decode(data, types):
        required = {"type", "target", "value"}
        if required - data.keys():
            fail("scenario.missing_field", "Record operation requires type, target and value")
        extras = []
        target = types.decode(data["target"], extras=extras)
        types._check_shape(target, ("object", "core:ref"), ("target",))
        value = types.decode(data["value"], extras=extras)
        if extras or data.keys() - required:
            raise UnknownValue("Unknown record operation fields")
        return cls(target=target, value=value)
    return OperationSpec(key=key, operation_type=cls, encode=encode, decode=decode, apply=apply_record,
        json_schema=JsonDocument.from_data({"type": "object", "required": ["type", "target", "value"],
            "properties": {"type": {"const": key}, "target": {"$ref": "#/$defs/core:ref"},
                           "value": {"$ref": "#/$defs/scenario_value"}}}))


def _fields_spec():
    def encode(operation, types):
        types._check_shape(operation.target, ("object", "core:ref"), ("target",))
        return {"type": "scenario:set_fields", "target": types.encode(operation.target),
                "changes": [{"name": c.name, "value": types.encode(c.value)} for c in operation.changes]}
    def decode(data, types):
        expected = {"type", "target", "changes"}
        if expected - data.keys() or type(data["changes"]) is not list:
            fail("scenario.fields", "SetFields requires target and a changes array")
        extras = []
        target = types.decode(data["target"], extras=extras)
        types._check_shape(target, ("object", "core:ref"), ("target",))
        changes = []
        unknown = bool(data.keys() - expected)
        for item in data["changes"]:
            if type(item) is not dict or {"name", "value"} - item.keys():
                fail("scenario.fields", "A field change requires name and value")
            unknown |= bool(item.keys() - {"name", "value"})
            try:
                changes.append(FieldChange(name=item["name"], value=types.decode(item["value"], extras=extras)))
            except (TypeError, ValueError) as error:
                if isinstance(error, UnknownValue):
                    raise
                fail("scenario.fields", str(error))
        try:
            result = SetFields(target=target, changes=tuple(changes))
        except (TypeError, ValueError) as error:
            fail("scenario.fields", str(error))
        if extras or unknown:
            raise UnknownValue("Unknown field changes")
        return result
    return OperationSpec(key="scenario:set_fields", operation_type=SetFields, encode=encode, decode=decode,
        apply=apply_record, json_schema=JsonDocument.from_data({"type": "object", "required": ["type", "target", "changes"],
            "properties": {"type": {"const": "scenario:set_fields"}, "target": {"$ref": "#/$defs/core:ref"},
                "changes": {"type": "array", "minItems": 1, "items": {"type": "object", "required": ["name", "value"],
                    "properties": {"name": {"type": "string", "minLength": 1}, "value": {"$ref": "#/$defs/scenario_value"}}}}}}))


def default_registry():
    return ScenarioRegistry((*(_simple_spec(d) for d in _declarations()),
        _record_spec("scenario:add", AddRecord), _record_spec("scenario:replace", ReplaceRecord),
        _record_spec("scenario:assert", AssertRecord), _fields_spec()))


def _types(schema):
    if schema is None:
        from ..inp.network import default_schema
        schema = default_schema()
    return schema.json_types


def read_patch(document, *, schema=None, registry=None):
    data = document.data
    if type(data) is not dict or data.get("kind") != KIND:
        fail("scenario.kind", "Expected an easysewer:scenario document")
    required = {"kind", "schema_version", "profile", "operations"}
    if required - data.keys():
        fail("scenario.envelope", "Missing scenario envelope fields")
    check_version(data["schema_version"])
    if type(data["operations"]) is not list or type(data.get("required_capabilities", [])) is not list:
        fail("scenario.envelope", "Operations and required_capabilities must be arrays")
    registry, types = registry or default_registry(), _types(schema)
    extensions = JsonDocument.from_data(data.get("extensions", {}))
    try:
        return ScenarioPatch(profile=data["profile"], schema_version=data["schema_version"],
            flow_units=data.get("flow_units"), required_capabilities=tuple(data.get("required_capabilities", [])),
            operations=tuple(registry.decode(value, types) for value in data["operations"]), extensions=extensions,
            _source=document, _unknown_root=tuple(sorted(data.keys() - required - {"flow_units", "required_capabilities", "extensions"})))
    except (TypeError, ValueError) as error:
        from ...validation import ValidationError
        if isinstance(error, ValidationError):
            raise
        fail("scenario.envelope", str(error))


def write_patch(patch, *, schema=None, registry=None):
    registry, types = registry or default_registry(), _types(schema)
    operations = []
    original_operations = patch._source.data["operations"] if patch._source else []
    for index, operation in enumerate(patch.operations):
        if isinstance(operation, OpaqueOperation):
            operations.append(operation.document.data)
        else:
            spec = registry.for_value(operation)
            encoded = spec.encode(operation, types)
            # Validate encoders and programmatic construction before applying any operations.
            decoded = registry.decode(encoded, types)
            if isinstance(decoded, OpaqueOperation):
                fail("scenario.writer", "Operation writer produced unsupported content")
            if index < len(original_operations):
                original = original_operations[index]
                baseline = registry.decode(original, types)
                if type(baseline) is type(operation) and json.dumps(spec.encode(baseline, types), sort_keys=True) == json.dumps(encoded, sort_keys=True):
                    encoded = original
            operations.append(encoded)
    data = dict(patch._source.data) if patch._source else {}
    data.update(kind=KIND, schema_version=patch.schema_version, profile=patch.profile, operations=operations)
    optional = {"flow_units": patch.flow_units, "required_capabilities": list(patch.required_capabilities),
                "extensions": patch.extensions.data if patch.extensions else {}}
    for name, value in optional.items():
        if value or name in data:
            data[name] = value
    if patch._source and json.dumps(data, sort_keys=True) == json.dumps(patch._source.data, sort_keys=True):
        return patch._source
    return JsonDocument.from_data(data)


def scenario_schema(*, schema=None, registry=None):
    registry = registry or default_registry()
    definitions = _types(schema).schema()
    definitions["scenario_value"] = _value_schema()
    for spec in registry.specifications:
        definitions[spec.key] = spec.json_schema.data
    operations = [{"$ref": "#/$defs/" + s.key} for s in registry.specifications]
    operations.append({"type": "object", "required": ["type"], "properties": {
        "type": {"type": "string", "not": {"enum": list(registry.capabilities)}}}})
    return {"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object", "$defs": definitions,
        "required": ["kind", "schema_version", "profile", "operations"], "properties": {
            "kind": {"const": KIND}, "schema_version": {"type": "string", "pattern": r"^1\.(0|[1-9][0-9]*)$"},
            "profile": {"type": "string", "minLength": 1}, "flow_units": {"enum": [None, "CFS", "CMS", "LPS", "GPM", "MGD", "MLD"]},
            "operations": {"type": "array", "items": {"anyOf": operations}},
            "required_capabilities": {"type": "array", "items": {"type": "string"}}, "extensions": {"type": "object"}}}
