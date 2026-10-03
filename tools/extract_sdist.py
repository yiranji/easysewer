"""Extract a release source archive safely, including on Python 3.10.11.

Only portable relative paths, directories and ordinary files are accepted.
Validate the complete member list before creating a fresh destination. Use
tarfile's data filter when available; older Python copies validated file bytes
without restoring ownership, permissions or other archive metadata.
"""
import argparse
import ntpath
from pathlib import Path, PureWindowsPath
import shutil
import tarfile


_is_reserved = getattr(ntpath, 'isreserved', lambda name: PureWindowsPath(name).is_reserved())


def _validated_members(archive):
    members = archive.getmembers()
    if not members:
        raise ValueError('Source archive is empty')
    paths = {}
    spellings = {}
    for member in members:
        name = member.name.rstrip('/') if member.isdir() else member.name
        parts = name.split('/')
        if (any(part in ('', '.', '..') or part.endswith((' ', '.')) or
                _is_reserved(part) for part in parts) or
                any(ord(char) < 32 or char in '\\:<>"|?*' for char in name)):
            raise ValueError('Unsafe archive member: ' + member.name)
        if member.type not in (tarfile.REGTYPE, tarfile.AREGTYPE, tarfile.DIRTYPE) or member.sparse is not None:
            raise ValueError('Nonregular archive member: ' + member.name)
        if name in paths:
            raise ValueError('Duplicate archive member: ' + member.name)
        paths[name] = member
        # Prevent Windows case aliases, including implicit parent directories.
        for end in range(1, len(parts) + 1):
            prefix = '/'.join(parts[:end])
            previous = spellings.setdefault(prefix.casefold(), prefix)
            if previous != prefix:
                raise ValueError('Case-colliding archive member: ' + member.name)
    for name in paths:
        parts = name.split('/')
        for end in range(1, len(parts)):
            parent = paths.get('/'.join(parts[:end]))
            if parent is not None and not parent.isdir():
                raise ValueError('File used as an archive directory: ' + name)
    return members


def extract_sdist(archive_path, destination):
    destination = Path(destination)
    # Check symlinks too: exists() alone misses a dangling destination link.
    if destination.exists() or destination.is_symlink():
        raise FileExistsError('Extraction destination must be new: ' + str(destination))
    with tarfile.open(archive_path) as archive:
        members = _validated_members(archive)
        destination.mkdir(parents=True, exist_ok=False)
        if callable(getattr(tarfile, 'data_filter', None)):
            archive.extractall(destination, members=members, filter='data')
        else:
            # Never call unfiltered extract/extractall on older interpreters.
            # No links are accepted and no preexisting destination is reused.
            for member in members:
                target = destination.joinpath(*member.name.rstrip('/').split('/'))
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.extractfile(member) as source, target.open('xb') as output:
                        shutil.copyfileobj(source, output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    extract_sdist(args.archive, args.output)


if __name__ == '__main__':
    main()
