"""Check the documentation publication list, local links and optional archives."""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import tarfile
from urllib.parse import unquote, urlsplit


ROOT = Path(__file__).resolve().parents[1]


def published_files(root=ROOT):
    docs = root / 'docs'
    manifest = json.loads((docs / 'publish.json').read_bytes())
    assert manifest['format'] == 'easysewer:published-docs' and manifest['version'] == 1
    names = manifest['files']
    assert names == sorted(set(names)) and 'publish.json' in names
    for name in names:
        path = PurePosixPath(name)
        assert not path.is_absolute() and '..' not in path.parts and '\\' not in name
        assert ':' not in name and path.as_posix() == name
        assert path.suffix in ('.md', '.json') and 'qualification' not in path.parts
        target = (docs / name).resolve()
        assert target.is_relative_to(docs.resolve()) and target.is_file(), name
    return names


def audit(root=ROOT, archive_root=None, sdist=None):
    docs = root / 'docs'
    names = published_files(root)
    assert set(names) == {p.relative_to(docs).as_posix() for p in docs.rglob('*') if p.is_file()}
    rules = (root / 'MANIFEST.in').read_text(encoding='utf-8')
    assert not re.search(r'(?m)^recursive-include docs\b', rules)
    assert set(re.findall(r'(?m)^include docs/(.+)$', rules)) == set(names)
    links = 0
    broken = []
    for source in [root / 'README.md', *docs.glob('*.md'), *(root / 'native').rglob('*.md')]:
        text = source.read_text(encoding='utf-8-sig')
        assert '\ufffd' not in text, source
        assert len(re.findall(r'(?m)^```', text)) % 2 == 0, source
        prose = re.sub(r'(?ms)^```[^\n]*\n.*?^```[^\n]*', '', text)
        for destination in re.findall(r'\[[^\]]*\]\(([^)]+)\)', prose):
            reference = urlsplit(destination.strip('<>'))
            if reference.scheme or reference.netloc or not reference.path:
                continue
            target = (source.parent / unquote(reference.path)).resolve()
            links += 1
            if not target.exists():
                broken.append((str(source.relative_to(root)), destination))
    assert not broken, broken
    historical = 0
    if archive_root is not None:
        archive_root = Path(archive_root).resolve()
        index = json.loads((docs / 'development-archive.json').read_bytes())
        for record in index['files']:
            target = (archive_root / record['path']).resolve()
            assert target.is_relative_to(archive_root)
            raw = target.read_bytes()
            assert len(raw) == record['bytes']
            assert hashlib.sha256(raw).hexdigest() == record['sha256'], target
            historical += 1
    if sdist is not None:
        with tarfile.open(sdist) as archive:
            members = {}
            for member in archive.getmembers():
                parts = PurePosixPath(member.name).parts
                if len(parts) > 2 and parts[1] == 'docs' and member.isfile():
                    name = '/'.join(parts[2:])
                    assert name not in members
                    members[name] = member
            assert set(members) == set(names), set(members) ^ set(names)
            for name, member in members.items():
                assert archive.extractfile(member).read() == (docs / name).read_bytes(), name
    return dict(passed=True, published_files=len(names),
                published_bytes=sum((docs / name).stat().st_size for name in names),
                local_links_checked=links, original_files_verified=historical,
                sdist_checked=sdist is not None)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive-root', type=Path)
    parser.add_argument('--sdist', type=Path)
    args = parser.parse_args()
    print(json.dumps(audit(archive_root=args.archive_root, sdist=args.sdist), indent=2))
