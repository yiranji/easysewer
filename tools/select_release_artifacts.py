"""Select only four qualified native platform wheels and the matching sdist.

This tool never publishes. The manual publication workflow first verifies a
successful validation run for its exact master commit and supplies only that
run's five named artifacts. The pure wheel is intentionally not a PyPI payload.
"""
import argparse
import base64
import csv
import email
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import shutil
import tarfile
import zipfile


TAGS = {
    'windows-x86_64': 'win_amd64',
    'linux-x86_64': 'linux_x86_64',
    'linux-aarch64': 'manylinux_2_28_aarch64',
    'darwin-arm64': 'macosx_14_0_arm64',
}


def verify_wheel(path, version, target):
    expected_name = f'easysewer-{version}-py3-none-{TAGS[target]}.whl'
    if path.name != expected_name:
        raise ValueError('Unexpected native wheel name: ' + path.name)
    with zipfile.ZipFile(path) as wheel:
        names = wheel.namelist()
        if len(names) != len(set(names)):
            raise ValueError('Duplicate wheel members')
        for name in names:
            if (PurePosixPath(name).is_absolute() or '\\' in name or ':' in name
                    or any(part in ('', '.', '..') for part in name.rstrip('/').split('/'))):
                raise ValueError('Unsafe wheel member: ' + name)
        metadata = email.message_from_bytes(wheel.read(f'easysewer-{version}.dist-info/METADATA'))
        if metadata['Name'] != 'easysewer' or metadata['Version'] != version:
            raise ValueError('Wheel project/version mismatch')
        tags = email.message_from_bytes(wheel.read(f'easysewer-{version}.dist-info/WHEEL'))
        if tags['Root-Is-Purelib'] != 'false' or tags.get_all('Tag') != ['py3-none-' + TAGS[target]]:
            raise ValueError('Wheel must have exactly one truthful native platform tag')
        manifest = json.loads(wheel.read('easysewer/libs/runtime-manifest.json'))
        if manifest.get('schema') != 'easysewer:native-runtime:1' or set(manifest.get('targets', {})) != {target}:
            raise ValueError('Native wheel must contain only its declared target')
        libraries = manifest['targets'][target]['libraries']
        if not {'swmm5', 'swmm-output'} <= set(libraries):
            raise ValueError('Required native libraries are absent')
        for library in libraries.values():
            if library['external_dependencies']:
                raise ValueError('A release wheel has unbundled runtime dependencies')
            rows = [library, *library['dependencies']]
            for row in rows:
                raw = wheel.read('easysewer/' + row['path'])
                if len(raw) != row['size'] or hashlib.sha256(raw).hexdigest() != row['sha256']:
                    raise ValueError('Final native artifact hash mismatch')
            raw = wheel.read('easysewer/' + library['build_record'])
            if hashlib.sha256(raw).hexdigest() != library['build_record_sha256']:
                raise ValueError('Final native build record hash mismatch')
        record_name = f'easysewer-{version}.dist-info/RECORD'
        recorded = set()
        for name, digest, size in csv.reader(io.StringIO(wheel.read(record_name).decode())):
            if name in recorded:
                raise ValueError('Duplicate RECORD member')
            recorded.add(name)
            if name == record_name:
                if digest or size:
                    raise ValueError('RECORD cannot hash itself')
                continue
            raw = wheel.read(name)
            expected = 'sha256=' + base64.urlsafe_b64encode(hashlib.sha256(raw).digest()).rstrip(b'=').decode()
            if digest != expected or size != str(len(raw)):
                raise ValueError('Wheel RECORD does not match final bytes: ' + name)
        if recorded != {name for name in names if not name.endswith('/')}:
            raise ValueError('Wheel RECORD is not a complete file inventory')


def select(packages, version, output):
    packages, output = Path(packages), Path(output)
    import re
    if not re.fullmatch(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)', version):
        raise ValueError('Expected a final major.minor.patch version')
    selected = []
    for target, tag in TAGS.items():
        matches = list(packages.rglob(f'easysewer-{version}-py3-none-{tag}.whl'))
        if len(matches) != 1:
            raise ValueError('Expected exactly one qualified wheel for ' + target)
        verify_wheel(matches[0], version, target)
        selected.extend(matches)
    native_wheels = [path for path in packages.rglob('*.whl') if path.parent.name != 'pure']
    if set(native_wheels) != set(selected):
        raise ValueError('Unexpected additional native wheel in release artifacts')
    archives = list(packages.rglob('*.tar.gz'))
    if len(archives) != 1 or archives[0].name != f'easysewer-{version}.tar.gz':
        raise ValueError('Expected exactly the matching source archive')
    with tarfile.open(archives[0], 'r:gz') as archive:
        raw = archive.extractfile(f'easysewer-{version}/PKG-INFO').read()
        metadata = email.message_from_bytes(raw)
        if metadata['Name'] != 'easysewer' or metadata['Version'] != version:
            raise ValueError('Source archive project/version mismatch')
    selected.extend(archives)
    output.mkdir(parents=True, exist_ok=False)
    records = []
    for path in selected:
        shutil.copyfile(path, output / path.name)
        records.append(dict(name=path.name, sha256=hashlib.sha256(path.read_bytes()).hexdigest()))
    return records


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--packages', type=Path, required=True)
    parser.add_argument('--version', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(select(args.packages, args.version, args.output), indent=2))
