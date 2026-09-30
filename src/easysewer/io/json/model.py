"""Model JSON 1.0: graph authority, source provenance and opaque preservation."""

import base64
from ._work import copy_json, equal_json
from ...validation._cooperative import checkpointed
from dataclasses import dataclass, replace
import re

from ...model.identity import Ref, canonical_key, namespace_key
from ...model.store import OpaqueConstraint
from ...model.provenance import RecordOrigin
from ...validation import Diagnostic, DiagnosticSubject, Severity, ValidationError, ValidationReport
from .document import JsonDocument, JsonLocations, fail, json_path
from .types import UnknownValue

MODEL_KIND = "easysewer:model"
SCHEMA_VERSION = "1.0"


def version(value, *, path=None):
    if type(value) is not str or not re.fullmatch(r"[0-9]+\.[0-9]+", value):
        fail("json.schema_version", "Schema version must have the form major.minor", path=path)
    return tuple(map(int, value.split(".")))


def _object(value, path):
    if type(value) is not dict:
        fail("json.object", "Expected a JSON object", path=path)
    return value


def _array(value, path):
    if type(value) is not list:
        fail("json.array", "Expected a JSON array", path=path)
    return value


def _key(value, path=None):
    result = tuple(value) if type(value) is list else value
    try:
        canonical_key(result)
    except (TypeError, ValueError) as error:
        fail("json.record_key", str(error), path=path)
    return result


def _required(value, names, path):
    missing = set(names)-set(value)
    if missing:
        fail("json.missing_field", f"Missing required members: {sorted(missing)}", path=path)


def _subject(owner, path=()):
    return DiagnosticSubject(collection=owner.collection, key=owner.key, path=path)


def _preserved_error(error, *, owner, wire, base, types, locations):
    paths = getattr(error, '_json_paths', (None,) * len(error.report.diagnostics))
    return ValidationReport(diagnostics=tuple(replace(issue,
        subject=_subject(owner, types.model_path(wire, path or ())),
        field=json_path((*base, *(path or ()))),
        span=issue.span or locations.span((*base, *(path or ()))))
        for issue, path in checkpointed(zip(error.report.diagnostics, paths))))


@dataclass(frozen=True, kw_only=True)
class KnownRecord:
    owner: Ref
    value: object
    extras: tuple[tuple[tuple, tuple[str, ...]], ...] = ()


@dataclass(frozen=True, kw_only=True)
class JsonModelSource:
    document: JsonDocument
    known: tuple[KnownRecord, ...]
    opaque: tuple[Ref, ...]
    diagnostics: tuple[Diagnostic, ...]
    incomplete: bool = False
    migration: object = None

    def report(self, *, for_run=False):
        issues = list(self.diagnostics)
        if self.incomplete and for_run:
            issues.append(Diagnostic(code="json.incomplete_model", message="Unknown JSON content needs its codec before INP export or execution", feature="easysewer:json"))
        return ValidationReport(diagnostics=tuple(issues))

    def require_inp(self):
        if self.incomplete:
            fail("json.incomplete_model", "Unknown JSON content cannot be silently omitted from INP; register its codec and explicitly reload the JSON")

    def validate_changes(self, model):
        raw = {Ref(collection=block["collection"],key=_key(entry["key"])).canonical:(entry, ('collections', i, 'records', j, 'value'))
               for i, block in checkpointed(enumerate(self.document.data["collections"])) for j, entry in checkpointed(enumerate(block["records"]))}
        locations = JsonLocations(self.document)
        types = model._schema.json_types
        for known in checkpointed(self.known):
            if not known.extras or not model._store.contains(known.owner):
                continue
            current = model.collection(known.owner.collection)[known.owner.key]
            if current == known.value:
                continue
            entry, path = raw[known.owner.canonical]
            try:
                encoded, baseline = types.encode(current), types.encode(known.value)
            except ValidationError as error:
                yield from error.report.diagnostics
                continue
            try:
                _merge_extras(encoded, entry['value'], baseline, known.extras)
            except ValidationError as error:
                yield from _preserved_error(error, owner=known.owner, wire=entry['value'], base=path,
                    types=types, locations=locations).diagnostics


def _source_document(source, types):
    from ..inp import InpDocument
    _object(source, ('source',))
    _required(source, ("format","encoding","path","bytes_base64","known_records"), ('source',))
    if source["format"] != "swmm-inp" or type(source["encoding"]) is not str or type(source["bytes_base64"]) is not str:
        fail("json.source", "Source requires an encoded SWMM INP document", path=('source',))
    if source["path"] is not None and type(source["path"]) is not str:
        fail("json.source", "Source path must be a string or null", path=('source', 'path'))
    try:
        data = base64.b64decode(source["bytes_base64"], validate=True)
        document = InpDocument.from_bytes(data, encoding=source["encoding"], source=source["path"])
    except (ValueError, TypeError, LookupError) as error:
        fail("json.source", str(error), path=('source', 'bytes_base64'))
    refs, extras = set(), []
    for index, value in enumerate(_array(source["known_records"], ('source', 'known_records'))):
        try:
            ref = types.decode(value, extras=extras, path=('source', 'known_records', index))
        except UnknownValue as error:
            fail("json.source", str(error), path=('source', 'known_records', index))
        if not isinstance(ref, Ref):
            fail("json.source", "Source known_records must contain references", path=('source', 'known_records', index))
        if ref.canonical in refs:
            fail("json.source", "Duplicate source record identity", path=('source', 'known_records', index))
        refs.add(ref.canonical)
    if extras:
        path, names = extras[0]
        fail("json.source", "Unknown members in source identity declarations", path=(*path, names[0]))
    return document, frozenset(refs)


def read_model(document, *, schema=None, profile=None, strict=False, migrations=None):
    locations = JsonLocations(document)
    try:
        result = _read_model(document, schema=schema, profile=profile, migrations=migrations, locations=locations)
    except ValidationError as error:
        paths = getattr(error, '_json_paths', (None,) * len(error.report.diagnostics))
        report = ValidationReport(diagnostics=tuple(replace(issue, span=locations.span(path or ()))
            if issue.span is None else issue for issue, path in zip(error.report.diagnostics, paths)))
        raise ValidationError(report) from error
    if strict:
        result.validate().raise_for_errors()
    return result


def _read_model(document, *, schema, profile, migrations, locations):
    from ...model import Model
    data = _object(document.data, ())
    if data.get("kind") != MODEL_KIND:
        fail("json.document_kind", "Expected an easysewer:model document; use ScenarioPatch for scenario documents", path=('kind',))
    _required(data, ("schema_version",), ())
    current = version(data["schema_version"], path=('schema_version',))
    migration_issues = ()
    migrated = None
    if migrations is not None and current < version(SCHEMA_VERSION):
        migrated = migrations.upgrade(document, target=SCHEMA_VERSION)
        document, migration_issues = migrated.document, migrated.report.diagnostics
        locations.document = document
        locations.positions = None
        data, current = document.data, version(migrated.document.data["schema_version"], path=('schema_version',))
    if current[0] != version(SCHEMA_VERSION)[0]:
        fail("json.schema_major", f"Unsupported Model JSON schema major version {current[0]}", path=('schema_version',))
    _required(data, ("profile","collections"), ())
    result = Model(schema=schema, profile=profile)
    if data["profile"] != result.profile.key:
        fail("json.profile", "Model JSON profile is not the explicitly selected profile", path=('profile',))
    types = result._schema.json_types
    diagnostics = list(migration_issues)
    def warning(code, message, *, path=(), owner=None, model_path=()):
        return Diagnostic(code=code, message=message, field=json_path(path), span=locations.span(path),
            subject=_subject(owner, model_path) if owner is not None else None,
            severity=Severity.WARNING, feature='easysewer:json')

    def record_error(error, owner, wire, base, value=None):
        paths = getattr(error, '_json_paths', (None,) * len(error.report.diagnostics))
        issues, absolute = [], []
        for issue, path in zip(error.report.diagnostics, paths):
            if path is None:
                path = types.wire_path(value, issue.subject.path) if value is not None and issue.subject is not None else ()
            absolute.append((*base, *path))
            subject = issue.subject if issue.subject is not None and issue.subject.collection is not None else _subject(owner, types.model_path(wire, path))
            issues.append(replace(issue, subject=subject, field=json_path(absolute[-1]),
                span=issue.span or locations.span(absolute[-1])))
        result = ValidationError(ValidationReport(diagnostics=tuple(issues)))
        result._json_paths = tuple(absolute)
        return result

    if current > version(SCHEMA_VERSION):
        diagnostics.append(warning("json.future_minor", "Reading a newer schema minor version; unknown content will be preserved", path=('schema_version',)))
    source = data.get("source")
    baseline, prior_source_keys = {}, frozenset()
    source_constraints = ()
    origins, unknown_origins = None, False
    if source is not None:
        inp, prior_source_keys = _source_document(source, types)
        imported = Model.from_document(inp, schema=result._schema, profile=result.profile)
        result._source = imported._source
        source_constraints = imported._store.opaque_constraints
        baseline = {Ref(collection=spec.key,key=key).canonical:(spec.key,record)
                    for spec in imported._store.specifications for key,record in imported.collection(spec.key).items()}
        from .provenance import read_origins
        origins, unknown_origins = read_origins(source, inp, types)
    known, opaque, decoded, identities = [], [], [], set()
    namespaces, incomplete = set(), False
    spec_by_key = {spec.key:spec for spec in result._store.specifications}

    def unknown_members(value, expected, path, owner=None):
        nonlocal incomplete
        for key in sorted(set(value)-set(expected)):
            diagnostics.append(warning("json.unknown_field", f"Unknown member {key!r} is preserved", path=(*path, key), owner=owner))
            incomplete = True

    unknown_members(data, ("kind","schema_version","profile","collections","source","extensions"), ())
    extensions = _object(data.get("extensions", {}), ('extensions',))
    for key in extensions:
        try:
            namespace_key(key)
        except ValueError as error:
            fail('json.extension', str(error), path=('extensions', key))
        diagnostics.append(warning("json.unknown_extension", f"Extension payload {key} is preserved", path=('extensions', key)))
        incomplete = True
    if source is not None:
        unknown_members(source, ("format","encoding","path","bytes_base64","known_records", "origins"), ('source',))
    if unknown_origins:
        incomplete = True
        diagnostics.append(warning('json.origins_version', 'Unknown source association ledger is preserved; lineage is untracked', path=('source', 'origins')))
    for block_index, block in enumerate(_array(data["collections"], ('collections',))):
        path = ('collections', block_index)
        _object(block, path)
        _required(block, ("collection","records"), path)
        try:
            namespace = namespace_key(block["collection"])
        except ValueError as error:
            fail("json.collection", str(error), path=path)
        if namespace in namespaces:
            fail("json.duplicate_collection", f"Duplicate collection {namespace}", path=path)
        namespaces.add(namespace)
        unknown_members(block, ("collection","records"), path)
        spec = spec_by_key.get(namespace)
        if spec is None:
            diagnostics.append(warning("json.unknown_collection", f"Collection {namespace} has no registered codec", path=path))
            incomplete = True
        for index, entry in enumerate(_array(block["records"], (*path, 'records'))):
            item_path = (*path, 'records', index)
            _object(entry, item_path)
            _required(entry, ("key","value"), item_path)
            owner = Ref(collection=namespace, key=_key(entry["key"], (*item_path, 'key')))
            if owner.canonical in identities:
                fail("json.duplicate_record", f"Duplicate record {owner}", path=item_path, subject=_subject(owner))
            identities.add(owner.canonical)
            unknown_members(entry, ("key","value"), item_path, owner)
            extras = []
            try:
                if spec is None:
                    raise UnknownValue(f"Unknown collection {namespace}")
                value = types.decode(entry["value"], extras=extras)
            except ValidationError as error:
                raise record_error(error, owner, entry['value'], (*item_path, 'value')) from error
            except UnknownValue as error:
                opaque.append(owner)
                local = error.path or ()
                diagnostics.append(warning("json.unknown_value", str(error), path=(*item_path, 'value', *local),
                    owner=owner, model_path=types.model_path(entry['value'], local)))
                incomplete = True
                continue
            if not isinstance(value, spec.record_type):
                fail("json.record_type", "Value type does not belong to its collection", path=(*item_path, 'value'), subject=_subject(owner))
            if spec.key_of(value) != owner.key:
                actual = Ref(collection=namespace, key=spec.key_of(value))
                fail("json.identity_mismatch", "Record key and value identity disagree", path=(*item_path, 'key'),
                    subject=_subject(owner), related=(_subject(actual),))
            if extras:
                incomplete = True
                for value_path, names in extras:
                    for name in names:
                        diagnostics.append(warning("json.unknown_field", f"Unknown value member {name!r} is preserved",
                            path=(*item_path, 'value', *value_path, name), owner=owner,
                            model_path=types.model_path(entry['value'], value_path)))
            known.append(KnownRecord(owner=owner, value=value, extras=tuple(extras)))
            if owner.canonical in baseline:
                value, promoted = types.promote_source_defaults(value, entry['value'], baseline[owner.canonical][1])
                if spec.key_of(value) != owner.key:
                    fail('json.identity_mismatch', 'Source defaults cannot change a record identity', path=item_path, subject=_subject(owner))
                for name in promoted:
                    diagnostics.append(Diagnostic(code='json.source_field_promoted', severity=Severity.INFO,
                        message='Newly supported field recovered from original INP; explicit JSON fields take precedence',
                        field=json_path((*item_path, 'value', name)), feature='easysewer:json',
                        subject=_subject(owner, types.model_path(entry['value'], (name,))), span=locations.span((*item_path, 'value'))))
            decoded.append((namespace,value,item_path,entry['value']))
    # Reparse newly supported source records, but do not resurrect source records
    # intentionally deleted from the previously structured JSON graph.
    from .provenance import validate_origins
    validate_origins(origins, identities, set(baseline) | set(prior_source_keys))
    with result._store._permit_context_change("import"):
        for ref, (namespace, value) in baseline.items():
            if ref not in prior_source_keys and ref not in identities:
                result.collection(namespace).add(value)
                result._store._set_origin(ref, RecordOrigin(kind='inp', original=ref))
        for namespace,value,item_path,wire in decoded:
            ref = Ref(collection=namespace, key=spec_by_key[namespace].key_of(value))
            try:
                result.collection(namespace).add(value)
            except ValidationError as error:
                raise record_error(error, ref, wire, (*item_path, 'value'), value) from error
            result._store._set_origin(ref, (origins or {}).get(ref.canonical, RecordOrigin(kind='untracked')))
    constraints = (*source_constraints, *((OpaqueConstraint(description="Preserved JSON content has unproven identities, references or units"),) if incomplete else ()))
    result._store.set_opaque_constraints(constraints)
    result._json_source = JsonModelSource(document=document, known=tuple(known), opaque=tuple(opaque),
                                           diagnostics=tuple(diagnostics), incomplete=incomplete, migration=migrated)
    return result


def _merge_extras(output, original, baseline, extras):
    for path, names in checkpointed(extras):
        target, before, known = output, original, baseline
        for part in checkpointed(path):
            if isinstance(part, int) and not equal_json(target, known):
                fail("json.unknown_field_edit", "Editing an array containing unknown fields requires the corresponding codec", path=path)
            try:
                target, before, known = target[part], before[part], known[part]
            except (KeyError, IndexError, TypeError):
                fail("json.unknown_field_edit", "An edited value no longer contains preserved fields", path=path)
        if type(target) is not dict or target.get("type") != before.get("type"):
            fail("json.unknown_field_edit", "Changing a variant with unknown fields requires its codec", path=path)
        for name in checkpointed(names):
            if name in target:
                fail("json.unknown_field_conflict", "A preserved field now has a writer; explicitly reload with its codec", path=(*path,name))
            target[name] = copy_json(before[name])
    return output


def _write_source(model, types, previous=None):
    document = model.document
    if document is None:
        return None
    specs = {s.key:s for s in checkpointed(model._store.specifications)}
    refs = []
    for output in checkpointed(model._source.decoded.features.values()):
        for entry in checkpointed(output.value.records):
            refs.append(Ref(collection=entry.collection,key=specs[entry.collection].key_of(entry.value)))
    result = copy_json(previous) if previous else {}
    # Preserve the old ownership ledger even when the current reader lacks a
    # previously enabled feature. Otherwise a later upgrade could resurrect a
    # record deliberately deleted from the authoritative JSON graph.
    ledger = copy_json(result.get("known_records", []))
    identities = {Ref(collection=row["collection"],key=_key(row["key"])).canonical for row in checkpointed(ledger)}
    for ref in checkpointed(refs):
        if ref.canonical not in identities:
            ledger.append(types.encode(ref))
            identities.add(ref.canonical)
    result.update(format="swmm-inp", encoding=document.encoding, path=document.source,
                  bytes_base64=base64.b64encode(document.to_bytes()).decode("ascii"),
                  known_records=ledger)
    from .provenance import write_origins
    origins = write_origins(model, types, result.get('origins'))
    if origins is not None:
        result['origins'] = origins
    return result


def write_model(model):
    types, state = model._schema.json_types, model._json_source
    original = state.document.data if state else {}
    locations = JsonLocations(state.document) if state else None
    raw_paths = {Ref(collection=block['collection'], key=_key(entry['key'])).canonical:
        ('collections', i, 'records', j, 'value') for i, block in checkpointed(enumerate(original.get('collections', ())))
        for j, entry in checkpointed(enumerate(block['records']))}
    data = copy_json(original) if state else {"kind":MODEL_KIND,"schema_version":SCHEMA_VERSION,"profile":model.profile.key}
    known = {row.owner.canonical:row for row in checkpointed(state.known)} if state else {}
    opaque = {ref.canonical for ref in checkpointed(state.opaque)} if state else set()
    raw = {Ref(collection=block["collection"],key=_key(entry["key"])).canonical:entry
           for block in checkpointed(original.get("collections",[])) for entry in checkpointed(block["records"])}
    specs = {s.key:s for s in checkpointed(model._store.specifications)}
    blocks = []
    emitted = set()

    def records(namespace):
        current = []
        for key, value in checkpointed(model.collection(namespace).items()):
            ref = Ref(collection=namespace,key=key).canonical
            old = known.get(ref)
            # Validate even apparent no-ops: Python considers True == 1 and
            # datetime equality ignores fold, neither is a wire-level no-op.
            encoded = types.encode(value)
            baseline = types.encode(old.value) if old is not None else None
            if old is not None and equal_json(encoded, baseline):
                current.append(copy_json(raw[ref]))
                continue
            if old and old.extras:
                try:
                    encoded = _merge_extras(encoded, raw[ref]["value"], baseline, old.extras)
                except ValidationError as error:
                    report = _preserved_error(error, owner=old.owner, wire=raw[ref]['value'], base=raw_paths[ref],
                        types=types, locations=locations)
                    raise ValidationError(model._resolve_diagnostics(report)) from error
            entry = copy_json(raw[ref]) if old else {}
            entry.update(key=list(key) if isinstance(key,tuple) else key, value=encoded)
            current.append(entry)
        return current

    for block in checkpointed(original.get("collections",[])):
        namespace = block["collection"]
        emitted.add(namespace)
        if namespace not in specs:
            blocks.append(copy_json(block))
            continue
        updated = copy_json(block)
        rows = records(namespace)
        # Opaque identities cannot be reordered, removed or renamed; inserting
        # them at their original positions preserves the mixed collection order.
        for index, entry in checkpointed(enumerate(block["records"])):
            if Ref(collection=namespace,key=_key(entry["key"])).canonical in opaque:
                rows.insert(index,copy_json(entry))
        updated["records"] = rows
        blocks.append(updated)
    for namespace in checkpointed(specs):
        if namespace not in emitted and len(model.collection(namespace)):
            blocks.append({"collection":namespace,"records":records(namespace)})
    data["collections"] = blocks
    source = _write_source(model, types, original.get("source"))
    if source is not None or "source" in original:
        data["source"] = source
    if "extensions" not in data:
        data["extensions"] = {}
    if state and equal_json(data, original):
        return state.document
    return JsonDocument.from_data(data, source=state.document.source if state else None)


def model_schema(schema):
    from .provenance import origin_schema
    definitions = schema.json_types.schema()
    definitions["record_key"] = {"anyOf":[{"type":"string","minLength":1},{"type":"array","minItems":1,"items":{"type":"string","minLength":1}}]}
    definitions["record"] = {"type":"object","required":["key","value"],"properties":{
        "key":{"$ref":"#/$defs/record_key"}, "value":{"anyOf":[{"$ref":"#/$defs/"+d.key} for d in schema.json_types.declarations]+[{"$ref":"#/$defs/unknown_value"}]}}}
    definitions["collection_base"] = {"type":"object","required":["collection","records"],"properties":{
        "collection":{"type":"string","pattern":r"^[a-z][a-z0-9_.-]*:[a-z][a-z0-9_.-]*$"},
        "records":{"type":"array","items":{"type":"object","required":["key","value"],"properties":{"key":{"$ref":"#/$defs/record_key"}}}}}}
    specifications = schema.new_store().specifications
    collections = []
    for specification in specifications:
        values = [{"$ref":"#/$defs/"+declaration.key} for declaration in schema.json_types.declarations
                  if issubclass(declaration.value_type, specification.record_type)]
        values.append({"$ref":"#/$defs/unknown_value"})
        collections.append({"allOf":[{"$ref":"#/$defs/collection_base"}, {"properties":{
            "collection":{"const":specification.key}, "records":{"items":{"properties":{"value":{"anyOf":values}}}}}}]})
    collections.append({"allOf":[{"$ref":"#/$defs/collection_base"}, {"properties":{
        "collection":{"not":{"enum":[specification.key for specification in specifications]}}}}]})
    definitions["collection"] = {"oneOf":collections}
    definitions["source"] = {"type":"object","required":["format","encoding","path","bytes_base64","known_records"],"properties":{
        "format":{"const":"swmm-inp"},"encoding":{"type":"string"},"path":{"type":["string","null"]},
        "bytes_base64":{"type":"string","contentEncoding":"base64"},
        "known_records":{"type":"array","items":{"$ref":"#/$defs/core:ref"}}, "origins": origin_schema()}}
    return {"$schema":"https://json-schema.org/draft/2020-12/schema", "$id":"urn:easysewer:model:1.0",
        "title":"EasySewer Model JSON 1.0", "type":"object", "required":["kind","schema_version","profile","collections"],
        "properties":{"kind":{"const":MODEL_KIND}, "schema_version":{"type":"string","pattern":r"^1\.[0-9]+$"},
            "profile":{"type":"string","minLength":1}, "collections":{"type":"array","items":{"$ref":"#/$defs/collection"}},
            "source":{"anyOf":[{"type":"null"},{"$ref":"#/$defs/source"}]}, "extensions":{"type":"object"}},
        "$defs":definitions}
