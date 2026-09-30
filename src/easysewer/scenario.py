"""Finite, ordered model edits applied to an independent, validated snapshot."""

from dataclasses import dataclass, field, replace

from .model.identity import Ref, canonical_key, namespace_key, require_immutable
from .model.units import UnitContext
from .validation import Diagnostic, Severity, ValidationError, ValidationReport


@dataclass(frozen=True, kw_only=True)
class AddRecord:
    target: Ref
    value: object


@dataclass(frozen=True, kw_only=True)
class ReplaceRecord:
    target: Ref
    value: object


@dataclass(frozen=True, kw_only=True)
class FieldChange:
    """Name is the declared JSON field name, independent of Python attributes."""
    name: str
    value: object

    def __post_init__(self):
        if type(self.name) is not str or not self.name:
            raise ValueError("A field change needs a nonempty wire field name")
        require_immutable(self.value)


@dataclass(frozen=True, kw_only=True)
class SetFields:
    target: Ref
    changes: tuple[FieldChange, ...]

    def __post_init__(self):
        if type(self.changes) is not tuple or not self.changes or any(type(c) is not FieldChange for c in self.changes):
            raise ValueError("SetFields needs a nonempty immutable tuple of FieldChange")
        if len({c.name for c in self.changes}) != len(self.changes):
            raise ValueError("A field may only be changed once per operation")


@dataclass(frozen=True, kw_only=True)
class RemoveRecord:
    target: Ref
    # Cascading deletion must be requested; never implied by a missing record.
    cascade: bool = False


@dataclass(frozen=True, kw_only=True)
class RenameRecord:
    target: Ref
    new_id: str


@dataclass(frozen=True, kw_only=True)
class MoveRecord:
    target: Ref
    before: str | tuple[str, ...] | None = None


@dataclass(frozen=True, kw_only=True)
class AssertRecord:
    """Optimistic precondition: None requires absence; otherwise exact typed value."""
    target: Ref
    value: object = None


@dataclass(frozen=True, kw_only=True)
class ChangeContext:
    context: str
    value: str
    mode: str = "convert"
    basis: str = "engine"

    def __post_init__(self):
        if self.context not in ("flow_units", "link_offsets", "force_main_equation"):
            raise ValueError("Unknown model context")
        if self.mode not in ("convert", "reinterpret") or self.basis not in ("engine", "physical"):
            raise ValueError("Context change requires explicit convert/reinterpret and unit basis")
        if self.context == "flow_units":
            UnitContext(flow_units=self.value)
        elif self.context == "link_offsets" and self.value not in ("DEPTH", "ELEVATION"):
            raise ValueError("Unknown link offset context")
        elif self.context == "force_main_equation":
            if self.mode != "reinterpret" or self.value not in ("H-W", "D-W"):
                raise ValueError("Force-main equation requires explicit reinterpretation")
        if self.context != "flow_units" and self.basis != "engine":
            raise ValueError("A unit basis only applies to flow-unit conversion")


@dataclass(frozen=True, kw_only=True)
class ChangePollutantUnits:
    target: Ref
    units: str
    mode: str = 'convert'

    def __post_init__(self):
        if not isinstance(self.target, Ref) or self.target.collection != 'swmm:pollutants' or type(self.target.key) is not str:
            raise ValueError('Pollutant-unit change requires a scalar swmm:pollutants reference')
        if self.units not in ('MG/L', 'UG/L', '#/L') or self.mode not in ('convert', 'reinterpret'):
            raise ValueError('Pollutant-unit change requires known units and convert/reinterpret mode')


@dataclass(frozen=True, kw_only=True)
class OpaqueOperation:
    document: object


@dataclass(frozen=True, kw_only=True)
class ScenarioChange:
    operation: int
    kind: str
    target: Ref
    before: object | None
    after: object | None
    before_index: int | None
    after_index: int | None


@dataclass(frozen=True, kw_only=True)
class ScenarioResult:
    model: object
    patch: object
    changes: tuple[ScenarioChange, ...]
    report: ValidationReport


def _error(code, message, *, index=None):
    raise ValidationError(ValidationReport(diagnostics=(Diagnostic(
        code=code, message=message, feature="easysewer:scenario",
        field=None if index is None else f"operations[{index}]",
    ),)))


def _rows(model):
    return {Ref(collection=spec.key, key=key).canonical: (Ref(collection=spec.key, key=key), value, index)
            for spec in model._store.specifications
            for index, (key, value) in enumerate(model.collection(spec.key).items())}


def _same(types, before, after):
    # Python equality conflates bool/int and datetime fold; compare explicit wire values.
    import json
    return json.dumps(types.encode(before), sort_keys=True) == json.dumps(types.encode(after), sort_keys=True)


@dataclass(frozen=True, kw_only=True)
class ScenarioPatch:
    operations: tuple = ()
    profile: str = "epa-swmm:5.2.4"
    # Numeric payloads are interpreted in this starting context, never silently converted.
    flow_units: str | None = None
    required_capabilities: tuple[str, ...] = ()
    extensions: object | None = None
    schema_version: str = "1.0"
    _source: object | None = field(default=None, repr=False, compare=False)
    _unknown_root: tuple[str, ...] = field(default=(), repr=False)

    def __post_init__(self):
        from .io.json.document import JsonDocument
        if type(self.operations) is not tuple or type(self.required_capabilities) is not tuple:
            raise TypeError("Operations and capabilities must be immutable tuples")
        require_immutable(self.operations)
        if type(self.profile) is not str or not self.profile:
            raise ValueError("Scenario profile must be a nonempty string")
        if self.flow_units is not None:
            UnitContext(flow_units=self.flow_units)
        for capability in self.required_capabilities:
            namespace_key(capability)
        if self.extensions is not None and (not isinstance(self.extensions, JsonDocument) or type(self.extensions.data) is not dict):
            raise TypeError("Extensions must be a JSON object document")
        from .io.json.scenario import check_version
        check_version(self.schema_version)

    @classmethod
    def from_json_document(cls, document, *, schema=None, registry=None):
        from .io.json.scenario import read_patch
        return read_patch(document, schema=schema, registry=registry)

    @classmethod
    def from_json(cls, path, **kwargs):
        from .io.json import JsonDocument
        return cls.from_json_document(JsonDocument.read(path), **kwargs)

    def to_json_document(self, *, schema=None, registry=None):
        from .io.json.scenario import write_patch
        return write_patch(self, schema=schema, registry=registry)

    def to_json(self, path, **kwargs):
        return self.to_json_document(**kwargs).write(path)

    def apply(self, model, *, registry=None):
        """Return a changed copy and actual graph changes; never mutate the input."""
        from .io.json.scenario import default_registry
        registry = (registry or default_registry()).snapshot()
        if self.profile != model.profile.key:
            _error("scenario.profile", "Scenario profile does not match the model")
        if self.flow_units is not None and self.flow_units != model.units.flow_units:
            _error("scenario.units", "Scenario numeric values require the declared starting flow units")
        missing = set(self.required_capabilities) - set(registry.capabilities)
        if missing or self._unknown_root:
            _error("scenario.unsupported", f"Unsupported scenario requirements: {sorted(missing) or self._unknown_root}")
        # Validate the entire operation set before invoking any handler (including extensions).
        self.to_json_document(schema=model._schema, registry=registry)
        for index, operation in enumerate(self.operations):
            if isinstance(operation, OpaqueOperation):
                _error("scenario.opaque_operation", "Unknown operation or fields cannot be executed", index=index)
            registry.for_value(operation)
        result = model.copy()
        changes = []
        types = result._schema.json_types
        with result.transaction():
            for index, operation in enumerate(self.operations):
                before = _rows(result)
                spec = registry.for_value(operation)
                try:
                    spec.apply(operation, result, types)
                except ValidationError as error:
                    raise ValidationError(ValidationReport(diagnostics=tuple(
                        replace(d, field=f"operations[{index}]" + (f".{d.field}" if d.field else ""))
                        for d in error.report.diagnostics))) from error
                except (KeyError, TypeError, ValueError) as error:
                    _error("scenario.operation", str(error), index=index)
                after = _rows(result)
                for key in dict.fromkeys((*before, *after)):
                    old, new = before.get(key), after.get(key)
                    if old and new and old[2] == new[2] and _same(types, old[1], new[1]):
                        continue
                    changes.append(ScenarioChange(operation=index, kind=spec.key, target=(new or old)[0],
                        before=old[1] if old else None, after=new[1] if new else None,
                        before_index=old[2] if old else None, after_index=new[2] if new else None))
        diagnostics = list(result.validate().diagnostics)
        if self.extensions and self.extensions.data:
            diagnostics.append(Diagnostic(code="scenario.metadata_preserved", severity=Severity.INFO,
                message="Informational extensions are retained on the scenario document"))
        if self.schema_version != "1.0":
            diagnostics.append(Diagnostic(code="scenario.newer_minor", severity=Severity.WARNING,
                message="A newer minor schema is retained; all operations were explicitly recognized"))
        return ScenarioResult(model=result, patch=self, changes=tuple(changes), report=ValidationReport(diagnostics=tuple(diagnostics)))


def apply_record(operation, model, types):
    target = operation.target
    if not isinstance(target, Ref):
        raise TypeError("Operation target must be Ref")
    rows = model.collection(target.collection)
    if isinstance(operation, AssertRecord):
        actual = rows[target.key] if target.key in rows else None
        if not _same(types, actual, operation.value):
            _error("scenario.precondition", f"Record precondition failed for {target}")
    elif isinstance(operation, (AddRecord, ReplaceRecord)):
        if not isinstance(operation.value, rows.spec.record_type):
            raise TypeError(f"Wrong record type for {target.collection}")
        if canonical_key(rows.spec.key_of(operation.value)) != canonical_key(target.key):
            raise ValueError("Operation target does not match record identity")
        if isinstance(operation, AddRecord):
            rows.add(operation.value)
        else:
            rows.replace(target.key, operation.value)
    elif isinstance(operation, SetFields):
        value = rows[target.key]
        declaration = next((d for d in types.declarations if d.value_type is type(value)), None)
        if declaration is None:
            raise ValueError("SetFields requires an explicit record JSON declaration")
        by_name = {f.name: f for f in declaration.fields}
        updates = {}
        for change in operation.changes:
            if change.name not in by_name:
                raise ValueError(f"Unknown wire field {change.name!r}")
            item = by_name[change.name]
            types._check_shape(change.value, item.shape, (change.name,))
            if item.attribute == rows.spec.identity_field:
                raise ValueError("Use RenameRecord for record identity")
            updates[item.attribute] = change.value
        updated = replace(value, **updates)
        types.encode(updated)
        rows.replace(target.key, updated)
    elif isinstance(operation, RemoveRecord):
        rows.remove(target.key, cascade=operation.cascade)
    elif isinstance(operation, RenameRecord):
        rows.rename(target.key, operation.new_id)
    elif isinstance(operation, MoveRecord):
        rows.move(target.key, before=operation.before)
    else:
        raise TypeError("Unsupported record operation")


def apply_context(operation, model, types):
    if operation.context == "flow_units":
        if operation.mode == "convert":
            model.convert_units(operation.value, basis=operation.basis)
        else:
            model.reinterpret_units(operation.value)
    elif operation.context == "link_offsets":
        (model.convert_link_offsets if operation.mode == "convert" else model.reinterpret_link_offsets)(operation.value)
    else:
        model.reinterpret_force_main_equation(operation.value)


def apply_pollutant_units(operation, model, types):
    method = model.convert_pollutant_units if operation.mode == 'convert' else model.reinterpret_pollutant_units
    method(operation.target.key, operation.units)
