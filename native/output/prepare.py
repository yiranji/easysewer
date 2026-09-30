"""Prepare the bounded SMO reader against the pinned EPA public ABI."""

import argparse
import hashlib
import json
from pathlib import Path

PATCH = 'easysewer:output-io:1'


def prepare(source, destination):
    source = Path(source).resolve()
    destination = Path(destination).resolve()
    if destination == source or destination.is_relative_to(source):
        raise ValueError('Prepared tree must be outside the original source')
    if destination.exists() and any(destination.iterdir()):
        raise ValueError('Prepared destination must be empty')
    here = Path(__file__).resolve().parent
    manifest = json.loads((here / 'source.json').read_text(encoding='utf-8'))
    contents = {}
    for name, digest in manifest['files'].items():
        raw = (source / name).read_bytes().replace(b'\r\n', b'\n')
        if hashlib.sha256(raw).hexdigest() != digest:
            raise ValueError('Source differs: ' + name)
        contents[name] = raw
    # Retain the public EPA headers, replace the complete implementation. The
    # unused original error manager remains in the verified provenance only.
    contents['src/outfile/swmm_output.c'] = (here / 'output_reader.c').read_bytes()
    contents['src/outfile/include/swmm_output_export.h'] = b'''#ifndef SWMM_OUTPUT_EXPORT_H
#define SWMM_OUTPUT_EXPORT_H
#ifdef _WIN32
#define EXPORT_OUT_API __declspec(dllexport)
#else
#define EXPORT_OUT_API __attribute__((visibility("default")))
#endif
#endif
'''
    for name, raw in contents.items():
        path = destination / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    record = dict(base=manifest, patch=PATCH, output_io=1,
                  recipe={name: hashlib.sha256((here / name).read_bytes()).hexdigest()
                          for name in ('prepare.py', 'output_reader.c')},
                  files={name: hashlib.sha256(raw).hexdigest() for name, raw in contents.items()})
    (destination / 'prepared-source.json').write_text(json.dumps(record, indent=2) + '\n', encoding='utf-8')
    return record


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True)
    parser.add_argument('--destination', required=True)
    args = parser.parse_args()
    print(json.dumps(prepare(args.source, args.destination), indent=2))
