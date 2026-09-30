"""Explicit, versioned wire types; no class discovery or payload-directed imports."""

from dataclasses import dataclass, is_dataclass, replace
from datetime import date, datetime, time, timedelta
import math

from ...model.fields import validate_fields
from ...model.identity import namespace_key, require_immutable
from ...validation import ValidationError, ValidationReport
from ...validation._cooperative import checkpointed
from .document import fail


@dataclass(frozen=True, kw_only=True)
class JsonField:
    name: str
    attribute: str
    shape: tuple

    def __post_init__(self):
        if not self.name or self.name == "type" or not self.attribute:
            raise ValueError("JSON fields require explicit wire and Python names")
        require_immutable(self.shape)


@dataclass(frozen=True, kw_only=True)
class JsonType:
    key: str
    value_type: type
    fields: tuple[JsonField, ...] = ()
    bases: tuple[str, ...] = ()
    defaults: tuple[tuple[str, object], ...] = ()
    singleton: object = None
    # Optional fields introduced after a source row was previously opaque.
    # Applies to a collection record with a same-type source counterpart only.
    source_defaults: tuple[str, ...] = ()

    def __post_init__(self):
        namespace_key(self.key)
        if not isinstance(self.value_type, type):
            raise TypeError("Expected a registered Python value type")
        if not isinstance(self.fields, tuple) or any(not isinstance(f, JsonField) for f in self.fields):
            raise TypeError("Fields must be explicit immutable JsonField declarations")
        for names in ([f.name for f in self.fields], [f.attribute for f in self.fields]):
            if len(names) != len(set(names)):
                raise ValueError("Duplicate JSON field mapping")
        if not set(dict(self.defaults)) <= {f.name for f in self.fields}:
            raise ValueError("JSON defaults must name declared wire fields")
        if len(dict(self.defaults)) != len(self.defaults):
            raise ValueError("Duplicate JSON default declarations")
        require_immutable(self.bases)
        for key in self.bases:
            namespace_key(key)
        require_immutable(self.defaults)
        require_immutable(self.singleton)
        if (not isinstance(self.source_defaults, tuple) or
                len(set(self.source_defaults)) != len(self.source_defaults) or
                not set(self.source_defaults) <= set(dict(self.defaults))):
            raise ValueError('Source defaults must name distinct optional JSON fields')
        if self.source_defaults and not is_dataclass(self.value_type):
            raise TypeError('Source defaults require a dataclass record')


class UnknownValue(ValueError):
    def __init__(self, message, *, path=None):
        self.path = path
        super().__init__(message)


class JsonTypes:
    def __init__(self, declarations=()):
        self._keys, self._types = {}, {}
        for declaration in declarations:
            self.register(declaration)

    def register(self, declaration):
        if not isinstance(declaration, JsonType):
            raise TypeError("Expected JsonType")
        if declaration.key in ("core:date","core:datetime","core:time","core:duration") or declaration.value_type in (str,int,float,bool,type(None),tuple,date,datetime,time,timedelta):
            raise ValueError("Primitive JSON codecs cannot be overridden")
        if declaration.key in self._keys or declaration.value_type in self._types:
            raise ValueError(f"Conflicting JSON type registration: {declaration.key}")
        self._keys[declaration.key] = declaration
        self._types[declaration.value_type] = declaration

    @property
    def declarations(self):
        return tuple(self._keys.values())

    def promote_source_defaults(self, value, wire, source):
        """Fill explicitly registered absent fields from a same-type INP record.

        Explicit JSON fields, including null/empty values, always take precedence.
        No identity, field or type is guessed from the source document.
        """
        if type(value) is not type(source):
            return value, ()
        declaration = self._types[type(value)]
        updates, promoted = {}, []
        for field in declaration.fields:
            if field.name in declaration.source_defaults and field.name not in wire:
                child = getattr(source, field.attribute)
                if child != getattr(value, field.attribute):
                    updates[field.attribute] = child
                    promoted.append(field.name)
        return (replace(value, **updates), tuple(promoted)) if updates else (value, ())

    def encode(self, value):
        if value is None or type(value) in (bool, str, int, float):
            if type(value) is int and abs(value) > 9007199254740991:
                fail("json.integer_precision", "Integer exceeds interoperable JSON precision")
            if type(value) is float and not math.isfinite(value):
                fail("json.number", "JSON numbers must be finite")
            return value
        if type(value) is tuple:
            return [self.encode(item) for item in checkpointed(value)]
        if type(value) is timedelta:
            return {"type":"core:duration", "days":value.days, "seconds":value.seconds, "microseconds":value.microseconds}
        if type(value) in (date, datetime, time):
            if getattr(value, "tzinfo", None) is not None:
                fail("json.timezone", "SWMM timestamps use local model time without a timezone")
            key = {date:"core:date", datetime:"core:datetime", time:"core:time"}[type(value)]
            result = {"type":key, "value":value.isoformat()}
            if type(value) is not date:
                result["fold"] = value.fold
            return result
        declaration = self._types.get(type(value))
        if declaration is None:
            fail("json.missing_writer", f"No registered JSON writer for {type(value).__name__}")
        if declaration.singleton is not None and value is not declaration.singleton:
            fail("json.singleton", "Value differs from the explicitly registered singleton")
        ValidationReport(diagnostics=tuple(validate_fields(value))).raise_for_errors()
        result = {"type":declaration.key}
        for field in checkpointed(declaration.fields):
            child = getattr(value, field.attribute)
            try:
                self._check_shape(child, field.shape, (declaration.key,field.name))
            except UnknownValue as error:
                fail("json.unsupported_value", str(error))
            result[field.name] = self.encode(child)
        return result

    def decode(self, value, *, path=(), extras=None):
        extras = [] if extras is None else extras
        if value is None or type(value) in (bool, str, int, float):
            return value
        if type(value) is list:
            return tuple(self.decode(item, path=(*path,index), extras=extras) for index,item in enumerate(value))
        if type(value) is not dict or type(value.get("type")) is not str:
            fail("json.typed_value", "Structured values require a string type discriminator", path=path)
        key = value["type"]
        special = {"core:duration": {"days", "seconds", "microseconds"}, "core:date": {"value"},
                   "core:time": {"value", "fold"}, "core:datetime": {"value", "fold"}}
        if key in special:
            expected = special[key] | {"type"}
            self._members(value, expected, path, extras)
            try:
                if key == "core:duration":
                    args = {name:value[name] for name in special[key]}
                    if any(type(v) is not int for v in args.values()) or not 0 <= args["seconds"] < 86400 or not 0 <= args["microseconds"] < 1000000:
                        raise ValueError("Duration requires normalized integer days, seconds and microseconds")
                    return timedelta(**args)
                if type(value["value"]) is not str:
                    raise ValueError("Date/time requires an ISO string")
                cls = {"core:date":date,"core:time":time,"core:datetime":datetime}[key]
                decoded = cls.fromisoformat(value["value"])
                if getattr(decoded, "tzinfo", None) is not None:
                    raise ValueError("Timestamp must be local model time")
                if key != "core:date":
                    if type(value["fold"]) is not int or value["fold"] not in (0,1):
                        raise ValueError("Fold must be 0 or 1")
                    decoded = decoded.replace(fold=value["fold"])
                return decoded
            except (ValueError, TypeError, OverflowError) as error:
                fail("json.temporal_value", str(error), path=path)
        declaration = self._keys.get(key)
        if declaration is None:
            raise UnknownValue(f"Unknown JSON value type {key}", path=path)
        names = {field.name for field in declaration.fields}
        defaults = dict(declaration.defaults)
        self._members(value, names | {"type"}, path, extras, optional=set(defaults))
        arguments = {}
        for field in declaration.fields:
            arguments[field.attribute] = (self.decode(value[field.name], path=(*path,field.name), extras=extras)
                                          if field.name in value else defaults[field.name])
            self._check_shape(arguments[field.attribute], field.shape, (*path,field.name))
        try:
            result = declaration.singleton if declaration.singleton is not None else declaration.value_type(**arguments)
        except (TypeError, ValueError, OverflowError) as error:
            fail("json.invalid_value", str(error), path=path)
        report = ValidationReport(diagnostics=tuple(validate_fields(result)))
        if not report.is_valid:
            error = ValidationError(report)
            error._json_paths = tuple((*path, *self.wire_path(result,
                issue.subject.path if issue.subject is not None else ())) for issue in report.diagnostics)
            raise error
        return result

    def wire_path(self, value, path):
        """Map known Python attributes to wire names, stopping at unknown data."""
        result = []
        for part in path:
            if type(part) is int and type(value) is tuple and 0 <= part < len(value):
                result.append(part); value = value[part]
                continue
            declaration = self._types.get(type(value))
            field = next((f for f in declaration.fields if f.attribute == part), None) if declaration else None
            if field is None: break
            result.append(field.name); value = getattr(value, field.attribute)
        return tuple(result)

    def model_path(self, wire, path):
        """Use registered field mappings, never a JSON display string, as identity."""
        result = []
        for part in path:
            if type(part) is int and type(wire) is list and 0 <= part < len(wire):
                result.append(part); wire = wire[part]
                continue
            discriminator = wire.get('type') if type(wire) is dict else None
            declaration = self._keys.get(discriminator) if type(discriminator) is str else None
            field = next((f for f in declaration.fields if f.name == part), None) if declaration else None
            if field is None or not field.attribute.isidentifier() or field.attribute.startswith('_'): break
            result.append(field.attribute); wire = wire.get(part)
        return tuple(result)

    def _check_shape(self, value, rule, path):
        kind, *args = rule
        if kind == "union":
            unknown = None
            for choice in checkpointed(args):
                try:
                    self._check_shape(value, choice, path)
                    return
                except UnknownValue as error:
                    unknown = error
                except ValidationError:
                    pass
            if unknown:
                raise unknown
        elif kind == "array" and type(value) is tuple:
            for index, item in checkpointed(enumerate(value)):
                self._check_shape(item, args[0], (*path,index))
            return
        elif kind == "literal":
            if any(type(value) is type(item) and value == item for item in checkpointed(args)):
                return
            if any(type(value) is type(item) for item in checkpointed(args)):
                raise UnknownValue(f"Unknown variant value {value!r} at {path}", path=path)
        elif kind == "object":
            special = {"core:date":date,"core:datetime":datetime,"core:time":time,"core:duration":timedelta}
            declaration = self._types.get(type(value))
            if type(value) is special.get(args[0]) or declaration and args[0] in (declaration.key, *declaration.bases):
                return
        elif kind in ("string","number","integer","boolean","null"):
            expected = {"string":(str,),"number":(int,float),"integer":(int,),"boolean":(bool,),"null":(type(None),)}[kind]
            if type(value) in expected:
                return
        fail("json.field_type", f"Value does not satisfy declared JSON field shape {kind}", path=path)

    @staticmethod
    def _members(value, expected, path, extras, optional=frozenset()):
        missing = expected - set(value) - optional
        if missing:
            fail("json.missing_field", f"Missing required JSON members: {sorted(missing)}", path=path)
        unknown = set(value) - expected
        if unknown:
            extras.append((path, tuple(sorted(unknown))))

    def schema(self):
        """JSON Schema 2020-12 definitions from explicit wire declarations."""
        def shape(rule):
            kind, *args = rule
            if kind in ("string", "number", "integer", "boolean", "null"):
                return {"type":kind}
            if kind == "literal":
                return {"enum":[self.encode(value) for value in args]}
            if kind == "array":
                return {"type":"array", "items":shape(args[0])}
            if kind == "union":
                return {"anyOf":[shape(value) for value in args]}
            if kind == "object":
                matches = [d.key for d in self.declarations if d.key == args[0] or args[0] in d.bases]
                if args[0].startswith("core:") and args[0] in ("core:date","core:time","core:datetime","core:duration"):
                    matches.append(args[0])
                return {"anyOf":[{"$ref":"#/$defs/"+key} for key in dict.fromkeys(matches)] + [{"$ref":"#/$defs/unknown_value"}]}
            raise ValueError(f"Unknown JSON field shape: {kind}")
        definitions = {}
        for declaration in self.declarations:
            properties = {"type":{"const":declaration.key}}
            properties.update((field.name,shape(field.shape)) for field in declaration.fields)
            for name in declaration.source_defaults:
                properties[name]['description'] = ('When omitted in a Model JSON collection record, '
                    'use the same-type original INP record field if present; explicit JSON values take precedence.')
            definitions[declaration.key] = {"type":"object", "properties":properties,
                "required":["type", *(f.name for f in declaration.fields if f.name not in dict(declaration.defaults))],
                "additionalProperties":True}
        definitions["core:duration"] = {"type":"object", "properties":{"type":{"const":"core:duration"},
            "days":{"type":"integer","minimum":-999999999,"maximum":999999999},
            "seconds":{"type":"integer","minimum":0,"maximum":86399},
            "microseconds":{"type":"integer","minimum":0,"maximum":999999}},
            "required":["type","days","seconds","microseconds"]}
        for key, format in (("core:date","date"),("core:time","time"),("core:datetime","date-time")):
            properties = {"type":{"const":key},"value":{"type":"string", "description":"ISO local "+format}}
            if key != "core:date":
                properties["fold"] = {"enum":[0,1]}
            definitions[key] = {"type":"object", "properties":properties, "required":list(properties)}
        definitions["unknown_value"] = {"type":"object", "properties":{"type":{"type":"string", "not":{"enum":list(definitions)}}}, "required":["type"]}
        return definitions
