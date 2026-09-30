"""Verify that an sdist preserves the project's authored test inputs exactly.

This checks archive contents, not whether every test passes on a given host.
No archive member is extracted or executed.
"""
from pathlib import Path, PurePosixPath
import argparse
import hashlib
import json
import tarfile


def audit(project, archive):
    project, archive = Path(project).resolve(), Path(archive).resolve()
    expected = {p.relative_to(project).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in (project/'tests').rglob('*')
        if p.is_file() and '__pycache__' not in p.parts and p.suffix not in ('.pyc', '.pyo')}
    if not expected:
        raise ValueError('Project contains no authored test files')
    found = {}
    with tarfile.open(archive) as stream:
        roots = set()
        for member in stream.getmembers():
            path = PurePosixPath(member.name)
            if path.is_absolute() or '..' in path.parts or '\\' in member.name:
                raise ValueError('Unsafe archive member: '+member.name)
            if not path.parts:
                continue
            roots.add(path.parts[0])
            name = '/'.join(path.parts[1:])
            if not name.startswith('tests/'):
                continue
            if member.isdir():
                continue
            if not member.isfile() or name in found:
                raise ValueError('Duplicate or nonregular test member: '+member.name)
            found[name] = hashlib.sha256(stream.extractfile(member).read()).hexdigest()
        if len(roots) != 1:
            raise ValueError('Source archive must have one root directory')
    missing = sorted(expected.keys()-found.keys())
    different = sorted(name for name in expected.keys() & found.keys() if expected[name] != found[name])
    unexpected = sorted(found.keys()-expected.keys())
    return dict(archive_sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
        expected_files=len(expected),archived_files=len(found),missing=missing,different=different,
        unexpected=unexpected,passed=not (missing or different or unexpected),test_digests=expected)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project',type=Path,default=Path(__file__).resolve().parents[1])
    parser.add_argument('--archive',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    result=audit(args.project,args.archive)
    args.output.write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({k:len(v) if k in ('missing','different','unexpected') else v
        for k,v in result.items() if k!='test_digests'}))
    return not result['passed']


if __name__=='__main__':
    raise SystemExit(main())
