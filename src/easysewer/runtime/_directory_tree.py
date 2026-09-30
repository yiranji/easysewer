"""Private regular-directory capture with portable, complete content evidence.

No consumer is enabled by this module alone. Its owner must reserve publication,
provide a private target and preserve/clean partial captures under its existing
transaction. These checks detect ordinary changes, not hostile filesystem races.
"""
from dataclasses import dataclass, replace
import hashlib
import os
from pathlib import Path, PurePosixPath
import stat

from ._workspace import copy_input
from ..validation._cooperative import checkpoint_scope, checkpoint as work_checkpoint, checkpointed


@dataclass(frozen=True, kw_only=True)
class DirectoryLimits:
    total_bytes: int = 256 * 1024**2
    entries: int = 100000
    depth: int = 64

    def __post_init__(self):
        for value in (self.total_bytes, self.entries, self.depth):
            if type(value) is not int or value <= 0:
                raise ValueError('Directory limits must be positive integers')


def relative_path(value):
    if type(value) is not str or not value or '\\' in value or ':' in value or '\0' in value:
        raise ValueError('Directory member requires a portable relative path')
    parts = value.split('/')
    if PurePosixPath(value).is_absolute() or any(p in ('', '.', '..') for p in parts):
        raise ValueError('Directory member path escapes its root')
    devices = {'con', 'prn', 'aux', 'nul', *(f'com{i}' for i in range(1, 10)), *(f'lpt{i}' for i in range(1, 10))}
    for part in parts:
        if part[-1] in '. ' or any(c in '<>"|?*' or ord(c) < 32 for c in part) or part.split('.')[0].casefold() in devices:
            raise ValueError('Directory member is not a portable regular filename')
        try:
            part.encode('utf-8', errors='strict')
        except UnicodeError:
            raise ValueError('Directory member name is not valid Unicode') from None
    return Path(*parts)


@dataclass(frozen=True, kw_only=True)
class DirectoryEntry:
    path: str
    kind: str
    sha256: str | None = None
    size: int | None = None
    hardlink_to: str | None = None

    def __post_init__(self):
        relative_path(self.path)
        if self.hardlink_to is not None:
            relative_path(self.hardlink_to)
            if self.kind != 'file' or self.hardlink_to >= self.path:
                raise ValueError('A hardlink must name an earlier regular member in the same tree')
        if self.kind == 'directory':
            if self.sha256 is not None or self.size is not None:
                raise ValueError('Directory entries do not have file content digests')
        elif self.kind == 'file':
            if (type(self.sha256) is not str or len(self.sha256) != 64 or
                    any(c not in '0123456789abcdef' for c in self.sha256) or
                    type(self.size) is not int or self.size < 0):
                raise ValueError('File entry requires a SHA-256 and nonnegative byte count')
        else:
            raise ValueError('Directory tree supports only regular files and directories')


@dataclass(frozen=True, kw_only=True)
class DirectoryManifest:
    entries: tuple[DirectoryEntry, ...]
    contract: str = 'easysewer:directory-tree:1'

    def __post_init__(self):
        if self.contract not in ('easysewer:directory-tree:1','easysewer:directory-tree:2'):
            raise ValueError('Unsupported directory tree contract')
        if type(self.entries) is not tuple or any(type(e) is not DirectoryEntry for e in checkpointed(self.entries)):
            raise TypeError('Directory manifest requires an immutable entry tuple')
        has_links = any(e.hardlink_to is not None for e in checkpointed(self.entries))
        if has_links != (self.contract == 'easysewer:directory-tree:2'):
            raise ValueError('Directory contract must explicitly bind its hardlink topology')
        seen = {}; files = {}; previous = None; folded = set()
        for entry in checkpointed(self.entries):
            if previous is not None and previous >= entry.path:
                raise ValueError('Directory entries must have unique sorted paths')
            previous = entry.path
            key = entry.path.casefold()
            if key in folded:
                raise ValueError('Directory paths collide on a case-insensitive host')
            folded.add(key)
            parts = entry.path.split('/')
            for i in range(1, len(parts)):
                if seen.get('/'.join(parts[:i])) != 'directory':
                    raise ValueError('Directory member lacks its declared parent')
            if entry.hardlink_to is not None:
                original = files.get(entry.hardlink_to)
                if (original is None or original.hardlink_to is not None or
                        (original.sha256, original.size) != (entry.sha256, entry.size)):
                    raise ValueError('Hardlink must reference its canonical member with matching content')
            if entry.kind == 'file':files[entry.path] = entry
            seen[entry.path] = entry.kind

    @property
    def has_hardlinks(self):
        return self.contract == 'easysewer:directory-tree:2'

    @property
    def total_bytes(self):
        return sum(e.size for e in checkpointed(self.entries) if e.kind == 'file')

    @property
    def sha256(self):
        # Each UTF-8 field is length-prefixed, including path, kind and size.
        # Empty directories and distinctions between paths/content stay bound.
        value = hashlib.sha256(self.contract.encode('ascii')+b'\0')
        for entry in checkpointed(self.entries):
            fields = (entry.path, entry.kind, entry.sha256 or '', '' if entry.size is None else str(entry.size))
            if self.has_hardlinks:fields += (entry.hardlink_to or '',)
            for field in fields:
                data = field.encode('utf-8'); value.update(len(data).to_bytes(8, 'big')); value.update(data)
        return value.hexdigest()


def _node(path, kind=None):
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
        raise ValueError(f'Directory tree cannot contain symbolic links or reparse points: {path}')
    actual = 'directory' if stat.S_ISDIR(info.st_mode) else 'file' if stat.S_ISREG(info.st_mode) else None
    if actual is None or kind is not None and actual != kind:
        raise ValueError(f'Directory tree has an unexpected resource kind: {path}')
    return actual, info


def _stamp(info):
    return info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _file_digest(path, expected):
    kind, before = _node(path, 'file')
    if _stamp(before) != expected:
        raise ValueError(f'Directory file changed before reading: {path}')
    digest = hashlib.sha256(); count = 0
    with path.open('rb') as stream:
        opened = _stamp(os.fstat(stream.fileno()))
        # Windows path and descriptor APIs can expose different ctime values.
        # Compare shared identity/content fields across APIs, then each API's
        # complete stamp against itself. Content is independently hashed.
        if opened[:5] != expected[:5]:
            raise ValueError(f'Directory file identity changed while opening: {path}')
        while True:
            work_checkpoint()
            chunk = stream.read(min(65536, before.st_size-count+1))
            if not chunk:
                break
            count += len(chunk)
            if count > before.st_size:
                raise ValueError(f'Directory file grew during reading: {path}')
            digest.update(chunk)
        work_checkpoint()
        if _stamp(os.fstat(stream.fileno())) != opened:
            raise ValueError(f'Directory file changed during reading: {path}')
    if count != before.st_size or _stamp(_node(path, 'file')[1]) != expected:
        raise ValueError(f'Directory file changed during reading: {path}')
    return digest.hexdigest(), count


def _scan(root, limits):
    if type(limits) is not DirectoryLimits:
        raise TypeError('Expected DirectoryLimits')
    root = Path(root)
    entries = []; identities = {}; total = 0; visited = set()

    def walk(directory, relative, depth):
        nonlocal total
        work_checkpoint()
        _, before = _node(directory, 'directory'); identity = (before.st_dev, before.st_ino)
        if identity in visited:
            raise ValueError('Directory tree contains repeated directory identity')
        visited.add(identity); identities[relative] = _stamp(before)
        children = []
        with os.scandir(directory) as iterator:
            for item in iterator:
                work_checkpoint()
                if len(entries) + len(children) >= limits.entries:
                    raise ValueError('Directory tree exceeds its entry budget')
                relative_path(item.name)
                children.append(item.name)
        children.sort()
        if len({n.casefold() for n in checkpointed(children)}) != len(children):
            raise ValueError('Directory names collide on a case-insensitive host')
        for name in checkpointed(children, interval=1):
            member = f'{relative}/{name}' if relative else name
            if depth+1 > limits.depth:
                raise ValueError('Directory tree exceeds its depth budget')
            path = directory/name; kind, info = _node(path)
            if len(entries) >= limits.entries:
                raise ValueError('Directory tree exceeds its entry budget')
            if kind == 'directory':
                entries.append(DirectoryEntry(path=member, kind=kind))
                walk(path, member, depth+1)
            else:
                if total+info.st_size > limits.total_bytes:
                    raise ValueError('Directory tree exceeds its byte budget')
                stamp = _stamp(info); digest, size = _file_digest(path, stamp)
                identities[member] = stamp; total += size
                entries.append(DirectoryEntry(path=member, kind=kind, sha256=digest, size=size))
        work_checkpoint()
        if _stamp(_node(directory, 'directory')[1]) != identities[relative]:
            raise ValueError(f'Directory changed during traversal: {directory}')
        # Names are verified independently of directory timestamp resolution.
        with os.scandir(directory) as iterator:
            after = []
            for item in iterator:
                work_checkpoint()
                if len(after) >= limits.entries:
                    raise ValueError('Directory tree exceeds its entry budget')
                after.append(item.name)
        if sorted(after) != children:
            raise ValueError(f'Directory membership changed during traversal: {directory}')
    walk(root, '', 0)
    canonical = {}; ordered = []; linked = False
    for entry in checkpointed(sorted(entries, key=lambda e:e.path)):
        if entry.kind == 'file':
            identity = identities[entry.path][:2]
            if not identity[1]:raise ValueError('Directory topology requires stable nonzero file identities')
            first = canonical.setdefault(identity, entry.path)
            if first != entry.path:
                entry = replace(entry, hardlink_to=first); linked = True
        ordered.append(entry)
    result = DirectoryManifest(entries=tuple(ordered), contract='easysewer:directory-tree:2' if linked else 'easysewer:directory-tree:1')
    return result, identities


def inspect_tree(root, *, limits=DirectoryLimits(), checkpoint=None):
    """Observe complete regular-tree content; no writes or format validation."""
    with checkpoint_scope(checkpoint):
        return _scan(root, limits)[0]


def verify_tree(root, expected, *, limits=DirectoryLimits(), checkpoint=None):
    """Reject any missing, extra, renamed or changed entry, including empty dirs."""
    if type(expected) is not DirectoryManifest:
        raise TypeError('Expected DirectoryManifest')
    with checkpoint_scope(checkpoint):
        actual = _scan(root, limits)[0]
        if actual != expected:
            raise ValueError('Directory tree differs from its captured content')
        return actual


def link_member(root, entry, *, expected_identity, checkpoint=None):
    """Create only an internal private hardlink; never link to caller/blob data."""
    with checkpoint_scope(checkpoint):
        if type(entry) is not DirectoryEntry or entry.hardlink_to is None:
            raise TypeError('Expected an explicit hardlink member')
        root = Path(root)
        for name in (entry.hardlink_to, entry.path):
            parts = relative_path(name).parts
            for count in range(len(parts)):
                work_checkpoint(); _node(root.joinpath(*parts[:count]), 'directory')
        source, target = root/entry.hardlink_to, root/entry.path
        _, before = _node(source, 'file')
        if (before.st_dev,before.st_ino) != expected_identity:
            raise ValueError('Private hardlink representative was replaced')
        if _file_digest(source, _stamp(before)) != (entry.sha256, entry.size):
            raise ValueError('Private hardlink representative content changed')
        work_checkpoint(); os.link(source, target)
        _, a = _node(source, 'file'); _, b = _node(target, 'file')
        if (a.st_dev,a.st_ino) != (before.st_dev,before.st_ino) or (a.st_dev,a.st_ino) != (b.st_dev,b.st_ino):
            raise ValueError('Private hardlink representative identity changed')
        work_checkpoint()


def _capture_tree(source, target, *, limits=DirectoryLimits(), checkpoint=None, existing=False, expected=None):
    """Capture into a new private root; leave partial output to the owning caller.

    Source bytes are never written. Existing destinations are never reused.
    A final independent source pass detects same-size/same-timestamp content
    changes and membership changes; each destination member is verified too.
    """
    with checkpoint_scope(checkpoint):
        source, target = Path(source), Path(target)
        source_abs, target_abs = source.resolve(), target.resolve()
        if source_abs.is_relative_to(target_abs) or target_abs.is_relative_to(source_abs):
            raise ValueError('Directory source and capture target overlap')
        _node(target.parent, 'directory')
        manifest, identities = _scan(source, limits)
        if expected is not None and manifest != expected:
            raise ValueError('Directory source differs from its observed publication evidence')
        work_checkpoint()
        if existing:
            _node(target, 'directory')
            with os.scandir(target) as entries:
                if next(entries, None) is not None:raise ValueError('Private population target must be empty')
        else:
            target.mkdir()
        target_identity = _stamp(_node(target, 'directory')[1])[:2]
        target_directories = {'': target_identity}
        target_files = {}
        for entry in checkpointed(manifest.entries, interval=1):
            relative = relative_path(entry.path); origin = source/relative; destination = target/relative
            # Recheck all source parent identities before opening each member.
            parent_parts = relative.parts[:-1]
            for i in range(len(parent_parts)+1):
                key = '/'.join(parent_parts[:i]); parent = source.joinpath(*parent_parts[:i])
                if _stamp(_node(parent, 'directory')[1]) != identities[key]:
                    raise ValueError('Directory source parent changed during capture')
            for i in range(len(parent_parts)+1):
                key = '/'.join(parent_parts[:i]); parent = target.joinpath(*parent_parts[:i])
                if _stamp(_node(parent, 'directory')[1])[:2] != target_directories[key]:
                    raise ValueError('Private directory parent changed during capture')
            kind, info = _node(origin, entry.kind)
            if _stamp(info) != identities[entry.path]:
                raise ValueError('Directory source member changed during capture')
            if kind == 'directory':
                destination.mkdir()
                target_directories[entry.path] = _stamp(_node(destination, 'directory')[1])[:2]
            elif entry.hardlink_to is not None:
                link_member(target, entry, expected_identity=target_files[entry.hardlink_to])
            else:
                created = []
                if copy_input(origin, destination, checkpoint=work_checkpoint, on_create=created.append,expected_size=entry.size) != (entry.sha256, entry.size):
                    raise ValueError('Directory source content changed during capture')
                target_files[entry.path] = created[0]
        observed, after = _scan(source, limits)
        if observed != manifest or after != identities:
            raise ValueError('Directory source changed during capture')
        if _stamp(_node(target, 'directory')[1])[:2] != target_identity:
            raise ValueError('Directory capture root identity changed')
        verify_tree(target, manifest, limits=limits)
        return manifest


def capture_tree(source, target, *, limits=DirectoryLimits(), checkpoint=None):
    return _capture_tree(source,target,limits=limits,checkpoint=checkpoint)


def populate_tree(source, target, expected, *, limits=DirectoryLimits(), checkpoint=None):
    """Populate a reserved empty private root without replacing its identity."""
    if type(expected) is not DirectoryManifest:raise TypeError('Expected DirectoryManifest')
    return _capture_tree(source,target,limits=limits,checkpoint=checkpoint,existing=True,expected=expected)


def tree_file_identities(root, *, limits=DirectoryLimits(), checkpoint=None):
    """Bounded identity-only traversal for read-only output alias protection.

    This does not inspect file contents or certify directory format/completeness.
    Duplicate hardlinks are retained as one identity; entry budgets count names.
    """
    if type(limits) is not DirectoryLimits:
        raise TypeError('Expected DirectoryLimits')
    with checkpoint_scope(checkpoint):
        identities = set(); visited = set(); count = 0
        def walk(path, depth):
            nonlocal count
            work_checkpoint(); _, before = _node(path, 'directory')
            identity = (before.st_dev, before.st_ino)
            if identity in visited:
                raise ValueError('Directory tree contains repeated directory identity')
            visited.add(identity)
            with os.scandir(path) as children:
                for child in children:
                    work_checkpoint(); count += 1
                    if count > limits.entries or depth+1 > limits.depth:
                        raise ValueError('Directory alias inspection exceeds its entry/depth budget')
                    item = path/child.name; kind, info = _node(item)
                    if kind == 'directory':
                        walk(item, depth+1)
                    else:
                        identities.add((info.st_dev, info.st_ino))
            if _stamp(_node(path, 'directory')[1]) != _stamp(before):
                raise ValueError('Directory changed during alias inspection')
        walk(Path(root), 0)
        return frozenset(identities)
