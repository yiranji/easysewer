"""Generate structural JSON Schemas for all readable result archive versions.

The schemas describe wire data. RunResult.load remains responsible for blob
integrity, constructor invariants, decoding evidence and cross-record relations.
No native library is loaded and no archive or runtime source is modified.
"""
from dataclasses import fields
from datetime import date, datetime, time, timedelta
import argparse
import json
from pathlib import Path
import sys
import types
from typing import Literal, Union, get_args, get_origin, get_type_hints

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from easysewer.runtime import _result_codec as codec

VERSIONS = tuple(f'1.{i}' for i in range(10))


def closed(properties):
    return dict(type='object', properties=properties, required=list(properties), additionalProperties=False)


def tagged(tag, **properties):
    return closed(dict(type={'const': tag}, **properties))


def ref(key):
    return {'$ref': '#/$defs/' + key}


def sequence(item=None, *, fixed=None, tag='core:tuple'):
    array = dict(type='array')
    if fixed is not None:
        array.update(prefixItems=fixed, minItems=len(fixed), maxItems=len(fixed), items=False)
    else:
        array['items'] = item
    if tag == 'core:set':
        array['uniqueItems'] = True
    return tagged(tag, values=array)


def build(version):
    minor = VERSIONS.index(version)
    definitions = {}
    enabled = {}
    for key, kind, names in codec.DECLARATIONS:
        if minor < 2 and kind in (codec.DiagnosticSubject, codec.DiagnosticLocation):
            continue
        if minor < 4 and kind in (codec.DirectoryArtifact, codec.DirectoryEntry, codec.DirectoryManifest):
            continue
        if minor < 8 and kind in codec.GRAPH_TYPES:
            continue
        if set(names.split()) != {f.name for f in fields(kind)}:
            raise ValueError('Unregistered archive field: ' + kind.__name__)
        enabled[kind] = key

    special = {bytes: 'core:bytes', date: 'core:date', datetime: 'core:datetime',
               time: 'core:time', timedelta: 'core:duration', codec.Severity: 'core:severity',
               codec.JsonDocument: 'core:json', codec.HotstartManifest: 'cache:hotstart',
               codec.CacheManifest: 'cache:interface', codec.ResultTable: 'result:table'}

    def annotation(kind):
        origin, args = get_origin(kind), get_args(kind)
        if origin in (Union, types.UnionType):
            return {'anyOf': [annotation(a) for a in args]}
        if origin is Literal:
            return {'enum': list(args)}
        if origin in (tuple, frozenset) or kind is tuple:
            if origin is frozenset:
                if args != (str,):
                    raise ValueError('Codec only accepts string sets')
                return sequence({'type': 'string'}, tag='core:set')
            if not args:
                return sequence(ref('wire-value'))
            if len(args) == 2 and args[1] is Ellipsis:
                return sequence(annotation(args[0]))
            return sequence(fixed=[annotation(a) for a in args])
        if kind is object:
            return ref('wire-value')
        primitive = {str: 'string', bool: 'boolean', int: 'integer', float: 'number', type(None): 'null'}
        if kind in primitive:
            return {'type': primitive[kind]}
        if kind in special:
            return ref(special[kind])
        if kind in enabled:
            if kind is codec.ReportDocument and minor >= 3:
                return {'anyOf': [ref('report:document'), ref('report:source')]}
            return ref(enabled[kind])
        raise ValueError('Unsupported schema annotation: ' + str(kind))

    for kind, key in enabled.items():
        names = list(codec.BY_TYPE[kind][1])
        omit = set()
        if kind is codec.RunResult:
            if minor == 0: omit.add('continuations')
            if minor < 4: omit.add('directory_artifacts')
            if minor < 8: omit.add('directory_group_artifacts')
        if kind is codec.Diagnostic and minor < 2: omit.update(('subject', 'related', 'locations'))
        if kind is codec.ResourceSnapshot:
            if minor < 4: omit.add('tree')
            if minor < 5: omit.add('initial_relative_path')
            if minor < 8: omit.add('directory_group')
        if kind is codec.DirectoryEntry and minor < 7: omit.add('hardlink_to')
        if kind is codec.DirectoryView and minor < 9: omit.add('kind')
        hints = get_type_hints(kind)
        if kind is codec.RunResult:
            hints.update(output_metadata=codec.OutputMetadata | None,
                         report_document=codec.ReportDocument | None,
                         failure_report=codec.ReportCapture | None,
                         backend_results=codec.JsonDocument | None,
                         report_tables=tuple[codec.ResultTable, ...])
        if kind is codec.DirectoryArtifact and minor < 6:
            hints['manifest'] = codec.DirectoryManifest
        props = {name: annotation(hints[name]) for name in names if name not in omit}
        if kind is codec.RunResult:
            props['status'] = {'enum': ['succeeded', 'rejected', 'failed', 'cancelled', 'timed_out']}
        definitions[key] = tagged(key, fields=closed(props))

    digest = dict(type='string', pattern='^[0-9a-f]{64}$', minLength=64, maxLength=64)
    size = dict(type='integer', minimum=0)
    optional_text = {'type': ['string', 'null']}
    definitions['core:bytes'] = tagged('core:bytes', sha256=digest, size=size)
    definitions['core:tuple'] = sequence(ref('wire-value'))
    definitions['core:set'] = sequence({'type': 'string'}, tag='core:set')
    definitions['core:severity'] = tagged('core:severity', value={'enum': [s.value for s in codec.Severity]})
    for name in ('date', 'datetime', 'time'):
        # Python ISO calendars accept forms beyond JSON Schema's RFC 3339 formats.
        definitions['core:' + name] = tagged('core:' + name, value={'type': 'string'},
                                             fold={'enum': [0] if name == 'date' else [0, 1]})
    definitions['core:duration'] = tagged('core:duration',
        days=dict(type='integer', minimum=-999999999, maximum=999999999),
        seconds=dict(type='integer', minimum=0, maximum=86399),
        microseconds=dict(type='integer', minimum=0, maximum=999999))
    definitions['core:json'] = tagged('core:json', raw=ref('core:bytes'), source=optional_text)
    for name in ('cache:hotstart', 'cache:interface', 'result:table'):
        definitions[name] = tagged(name, raw=ref('core:bytes'))
    if minor >= 3:
        definitions['report:source'] = tagged('report:source', version={'const': '1.0'},
            raw=ref('core:bytes'), requested_encoding=optional_text, source=optional_text,
            profile=optional_text, decoded_sha256=digest)
    definitions['wire-value'] = {'anyOf': [{'type': ['null', 'string', 'boolean', 'number']}] +
                                [ref(key) for key in definitions]}
    envelope = closed(dict(kind={'const': 'easysewer:run-result'}, schema_version={'const': version},
        result=ref('run:result'), blobs=dict(type='array', uniqueItems=True,
                                           items=closed(dict(sha256=digest, size=size)))))
    return dict({'$schema': 'https://json-schema.org/draft/2020-12/schema',
                 'title': f'EasySewer result archive {version}',
                 'description': 'Structural contract for result.json. RunResult.load additionally verifies blob bytes, Python scalar types, calendar parsing, evidence and cross-record invariants. Schema validation alone does not establish a loadable or valid simulation result.',
                 '$defs': definitions}, **envelope)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    for version in VERSIONS:
        path = ROOT / 'docs' / f'result-archive-{version}.schema.json'
        raw = (json.dumps(build(version), ensure_ascii=False, indent=2) + '\n').encode('utf-8')
        if args.check:
            if not path.exists() or path.read_bytes() != raw:
                raise ValueError('Schema differs from generated declaration: ' + str(path))
        else:
            path.write_bytes(raw)
    print(json.dumps(dict(versions=VERSIONS, checked=args.check)))


if __name__ == '__main__':
    main()
