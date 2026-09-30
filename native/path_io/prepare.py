"""Prepare a path I/O candidate from verified standard13/custom11 sources."""
import argparse
import hashlib
import json
from pathlib import Path
import runpy


def prepare(source, destination):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if source == destination or destination.is_relative_to(source):
        raise ValueError('Destination must be outside the source')
    if destination.exists() and any(destination.iterdir()):
        raise ValueError('Destination must be empty')
    raw_manifest = (source/'prepared-source.json').read_bytes()
    record = json.loads(raw_manifest)
    custom = record.get('patch') == 'easysewer:flexible-ponding:abi:201'
    if not ((custom and record.get('native_io_fixes') == 11) or
            record.get('patch') == 'easysewer:standard:5.2.4:13'):
        raise ValueError('Requires standard13 or custom11 prepared source')
    if record.get('checkpoint', {}).get('abi') != 2:
        raise ValueError('Requires checkpoint ABI 2')
    contents = {}
    for name, digest in record['files'].items():
        path = source/name
        if not path.resolve().is_relative_to(source):
            raise ValueError('Source path escapes tree: ' + name)
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != digest:
            raise ValueError('Prepared source changed: ' + name)
        contents[name] = raw
    root = Path(__file__).resolve().parent
    recipe = runpy.run_path(str(root/'patch.py'))
    recipe['patch'](contents, custom=custom)
    for name, raw in contents.items():
        path = destination/name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    evidence = dict(
        kind='easysewer:path-io-candidate', version=2,
        family='custom' if custom else 'standard', path_io=1,
        native_revision=12 if custom else 14,
        qualified=False,
        base_manifest_sha256=hashlib.sha256(raw_manifest).hexdigest(), base=record,
        recipes={name: hashlib.sha256((root/name).read_bytes()).hexdigest()
                 for name in ('prepare.py', *recipe['RECIPE_FILES'])},
        files={name: hashlib.sha256(raw).hexdigest() for name, raw in contents.items()},
    )
    (destination/'path-source.json').write_text(json.dumps(evidence, indent=2)+'\n', encoding='utf-8')
    return evidence


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True)
    parser.add_argument('--destination', required=True)
    args = parser.parse_args()
    evidence = prepare(args.source, args.destination)
    print(json.dumps({key: evidence[key] for key in ('family', 'native_revision', 'qualified')}))
