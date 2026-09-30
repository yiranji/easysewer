"""Versioned source association ledger; declarations are not authentication."""

from ._work import copy_json
from ...validation._cooperative import checkpointed
import hashlib

from ...model.identity import Ref
from ...model.provenance import RecordOrigin
from .document import fail
from .types import UnknownValue

FORMAT = 'easysewer:record-origins'
VERSION = '1.0'


def _ref(value, types):
    extras = []
    try:
        result = types.decode(value, extras=extras)
    except UnknownValue as error:
        fail('json.origins', str(error))
    if not isinstance(result, Ref) or extras:
        fail('json.origins', 'Origin identities require exact Ref declarations')
    return result


def read_origins(source, document, types):
    """Return known associations or mark an unknown ledger version opaque."""
    if 'origins' not in source:
        return None, False
    ledger = source['origins']
    if type(ledger) is not dict:
        fail('json.origins', 'Origin ledger must be an object')
    if ledger.get('format') != FORMAT or ledger.get('schema_version') != VERSION:
        return None, True
    if set(ledger) != {'format', 'schema_version', 'source_sha256', 'records'} or type(ledger['records']) is not list:
        fail('json.origins', 'Malformed origin ledger')
    if ledger['source_sha256'] != hashlib.sha256(document.to_bytes()).hexdigest():
        fail('json.origins_source', 'Origin ledger does not match the original INP bytes')
    result, used = {}, set()
    for row in ledger['records']:
        if type(row) is not dict or set(row) != {'owner', 'kind', 'original'}:
            fail('json.origins', 'Origin rows require owner, kind and original')
        owner = _ref(row['owner'], types).canonical
        original = _ref(row['original'], types) if row['original'] is not None else None
        try:
            origin = RecordOrigin(kind=row['kind'], original=original)
        except (ValueError, TypeError) as error:
            fail('json.origins', str(error))
        if owner in result:
            fail('json.origins', 'Duplicate current record in origin ledger')
        if original is not None:
            if original.collection != owner.collection or original.canonical in used:
                fail('json.origins', 'Origin association changes collection or reuses an original record')
            used.add(original.canonical)
        result[owner] = origin
    return result, False


def validate_origins(origins, identities, source_identities):
    for owner, origin in (origins or {}).items():
        if owner not in identities:
            fail('json.origins', 'Origin ledger references a missing current record')
        if origin.original is not None and origin.original not in source_identities:
            fail('json.origins', 'Origin ledger references an unknown original record')


def write_origins(model, types, previous):
    if previous is not None and (previous.get('format') != FORMAT or previous.get('schema_version') != VERSION):
        return copy_json(previous)
    rows = {}
    for spec in checkpointed(model._store.specifications):
        for key in checkpointed(model.collection(spec.key)):
            owner = Ref(collection=spec.key, key=key)
            origin = model._store._origin(owner)
            rows[owner.canonical] = dict(owner=types.encode(owner), kind=origin.kind,
                original=types.encode(origin.original))
    opaque = model._json_source.opaque if model._json_source else ()
    opaque_refs = {ref.canonical for ref in checkpointed(opaque)}
    if previous is None and all(row['kind'] == 'untracked' for row in checkpointed(rows.values())):
        return None  # Preserve an untouched legacy JSON without inventing history.
    ordered = []
    for row in checkpointed(previous.get('records', ()) if previous else ()):
        owner = _ref(row['owner'], types).canonical
        if owner in rows:
            ordered.append(rows.pop(owner))
        elif owner in opaque_refs:
            ordered.append(copy_json(row))
    ordered.extend(checkpointed(rows.values()))
    existing = {_ref(row['owner'], types).canonical for row in checkpointed(ordered)}
    ordered.extend(dict(owner=types.encode(ref), kind='untracked', original=None)
        for ref in checkpointed(opaque) if ref.canonical not in existing)
    return dict(format=FORMAT, schema_version=VERSION, source_sha256=model._source.source_sha256, records=ordered)


def origin_schema():
    row = {'type': 'object', 'required': ['owner', 'kind', 'original'],
        'properties': {'owner': {'$ref': '#/$defs/core:ref'},
            'kind': {'enum': ['inp', 'created', 'untracked']},
            'original': {'anyOf': [{'$ref': '#/$defs/core:ref'}, {'type': 'null'}]}}}
    return {'type': 'object', 'required': ['format', 'schema_version', 'source_sha256', 'records'],
        'properties': {'format': {'const': FORMAT}, 'schema_version': {'type': 'string'},
            'source_sha256': {'type': 'string', 'pattern': '^[0-9a-f]{64}$'},
            'records': {'type': 'array', 'items': row}}}
