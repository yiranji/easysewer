"""Verify immutable PyPI bytes against prepared artifacts, without publishing.

A successful upload is not a successful installation. Check the complete public
file inventory, then download this runner's native wheel from the official PyPI
file service. Installation and doctor execution are separate workflow steps.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlsplit
from urllib.request import urlopen

from select_release_artifacts import TAGS


def sha256(raw):
    return hashlib.sha256(raw).hexdigest()


def read_url(url, limit):
    with urlopen(url, timeout=30) as response:
        final = urlsplit(response.geturl())
        original = urlsplit(url)
        if final.scheme != 'https' or final.hostname != original.hostname:
            raise ValueError('Unexpected download redirect')
        raw = response.read(limit + 1)
    if len(raw) > limit:
        raise ValueError('Response exceeds the expected size limit')
    return raw


def expected_files(directory, version):
    if not re.fullmatch(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)', version):
        raise ValueError('Expected a final major.minor.patch version')
    names = {f'easysewer-{version}-py3-none-{tag}.whl' for tag in TAGS.values()}
    names.add(f'easysewer-{version}.tar.gz')
    files = {path.name: path for path in Path(directory).iterdir() if path.is_file()}
    if set(files) != names:
        raise ValueError('Expected exactly the prepared four wheels and source archive')
    return {name: {'sha256': sha256(path.read_bytes()), 'size': path.stat().st_size}
            for name, path in files.items()}


def verify_index(data, expected, version):
    if data['info']['name'] != 'easysewer' or data['info']['version'] != version:
        raise ValueError('Unexpected project or version in PyPI metadata')
    rows = data['urls']
    files = {row['filename']: row for row in rows}
    if len(files) != len(rows) or not set(files) <= set(expected):
        raise ValueError('Unexpected or duplicate PyPI distribution')
    for name, row in files.items():
        if row['yanked'] or row['digests']['sha256'] != expected[name]['sha256'] or row['size'] != expected[name]['size']:
            raise ValueError('PyPI file differs from the exact prepared artifact: ' + name)
        url = urlsplit(row['url'])
        if (url.scheme != 'https' or url.hostname != 'files.pythonhosted.org'
                or url.username or url.password or unquote(url.path.rsplit('/', 1)[-1]) != name):
            raise ValueError('Unexpected PyPI distribution URL')
    return files if set(files) == set(expected) else None


def verify_and_download(directory, version, target, output, *, max_wait=300):
    expected = expected_files(directory, version)
    if target not in TAGS:
        raise ValueError('Unknown native target')
    index = f'https://pypi.org/pypi/easysewer/{version}/json'
    deadline = time.monotonic() + max_wait
    while True:
        try:
            data = json.loads(read_url(index, 1024 * 1024))
            files = verify_index(data, expected, version)
        except HTTPError as error:
            if error.code != 404 and not 500 <= error.code <= 599:
                raise
            files = None
        except (URLError, TimeoutError):
            files = None
        if files is not None:
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError('PyPI did not expose the complete release before the verification deadline')
        time.sleep(min(10, remaining))
    name = f'easysewer-{version}-py3-none-{TAGS[target]}.whl'
    raw = read_url(files[name]['url'], expected[name]['size'])
    if len(raw) != expected[name]['size'] or sha256(raw) != expected[name]['sha256']:
        raise ValueError('Downloaded PyPI bytes differ from the prepared artifact: ' + name)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    (output / name).write_bytes(raw)
    return {'version': version, 'target': target, 'files': expected,
            'downloaded_wheel': name, 'source': files[name]['url']}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--expected', type=Path, required=True)
    parser.add_argument('--version', required=True)
    parser.add_argument('--target', choices=sorted(TAGS), required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(verify_and_download(args.expected, args.version, args.target, args.output), indent=2))
