"""Evidence-preserving disposition of initial lease/pending remnants.

The immutable archive guard replaces the initial lease's identity while both
leases are locked. A delayed creator must fail its existing identity check.
The guard and ZIP are immutable; cleanup progress is inferred from exact file
identities/content, so interrupted cleanup needs no additional journal writes.
"""
import hashlib
import io
import json
import os
from pathlib import Path
import re
import uuid
import zipfile

from .recovery import RecoveryArchive, _Lease, _identity, _journal_budget, _regular


_MAGIC = b'0easysewer-recovery-archive-v1\n'
_TOKEN = r'[0-9a-f]{32}'


def _windows_open(path, *, create, read_only=False):
    # Explicit file management only; importing the pure runtime loads no ctypes.
    import ctypes
    from ctypes import wintypes
    import msvcrt
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    opening = kernel.CreateFileW
    opening.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    opening.restype = wintypes.HANDLE
    closing = kernel.CloseHandle
    closing.argtypes = (wintypes.HANDLE,); closing.restype = wintypes.BOOL
    if read_only and create:
        raise ValueError("A read-only recovery handle cannot create a file")
    access = 0x80000000 if read_only else 0xC0000000
    handle = opening(str(path), access, 7, None, 1 if create else 3, 128, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        fd = msvcrt.open_osfhandle(handle, (os.O_RDONLY if read_only else os.O_RDWR) | os.O_BINARY)
    except BaseException:
        closing(handle)
        raise
    try:
        return os.fdopen(fd, 'rb' if read_only else 'r+b')
    except BaseException:
        os.close(fd)
        raise



def _windows_replace(source, destination):
    """Atomically replace a journal while shared-delete readers retain its old version.

    FILE_RENAME_INFO follows the Windows SDK layout. FileRenameInfoEx (22)
    with REPLACE_IF_EXISTS | POSIX_SEMANTICS permits an open destination.
    Unsupported filesystems and access failures remain errors; no unlink gap,
    permission changes, or unbounded retry is used.
    """
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    opening = kernel.CreateFileW
    opening.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    opening.restype = wintypes.HANDLE
    closing = kernel.CloseHandle
    closing.argtypes = (wintypes.HANDLE,); closing.restype = wintypes.BOOL
    setting = kernel.SetFileInformationByHandle
    setting.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
    setting.restype = wintypes.BOOL

    class Rename(ctypes.Structure):
        _fields_ = [('flags', wintypes.DWORD), ('root', wintypes.HANDLE),
                    ('length', wintypes.DWORD), ('name', wintypes.WCHAR * 1)]

    name = str(Path(destination).absolute()).encode('utf-16-le', errors='surrogatepass')
    # Include a trailing WCHAR NUL while keeping FileNameLength in bytes
    # excluding that NUL. The native call also requires sizeof(FILE_RENAME_INFO).
    buffer = ctypes.create_string_buffer(max(ctypes.sizeof(Rename), Rename.name.offset + len(name) + 2))
    value = Rename.from_buffer(buffer)
    value.flags = 3
    value.length = len(name)
    ctypes.memmove(ctypes.addressof(buffer) + Rename.name.offset, name, len(name))
    handle = opening(str(source), 0x10000, 7, None, 3, 128, None)  # DELETE; OPEN_EXISTING
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if not setting(handle, 22, buffer, len(buffer)):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        closing(handle)


def _json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('Duplicate recovery archive member')
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=pairs)


def _stamp(path):
    value = path.lstat()
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


def _read_file(path, limit):
    _regular(path); before = _stamp(path)
    with path.open('rb') as stream:
        raw = stream.read(limit+1)
    if len(raw) > limit:
        raise ValueError(f'Recovery archive exceeds max_bytes ({limit} bytes)')
    if _stamp(path) != before or len(raw) != before[2] or path.is_symlink():
        raise ValueError(f'Recovery artifact changed during reading: {path}')
    return raw, before[:2]


def _paths(journal):
    path = Path(journal).absolute()
    match = re.fullmatch(r'\.easysewer-recovery-('+_TOKEN+r')\.json', path.name)
    if not match or path.parent.resolve() != path.parent or not path.parent.is_dir():
        raise ValueError('Expected a canonical recovery path in an existing resolved directory')
    return path, match[1]


def _pending(path, token):
    pattern = re.compile(r'\.easysewer-recovery-'+token+r'\.json\.'+_TOKEN+r'\.pending')
    return sorted(p for p in path.parent.iterdir() if pattern.fullmatch(p.name))


def _identity_value(value):
    return (type(value) is list and len(value) == 2
            and all(type(v) is int and v >= 0 for v in value))


def _digest(value):
    return type(value) is str and re.fullmatch(r'[0-9a-f]{64}', value) is not None


def _load(lease, max_bytes, *, journal=None):
    lease.stream.seek(0)
    prefix = lease.stream.read(len(_MAGIC))
    if prefix != _MAGIC:
        return None
    raw = lease.stream.read(max_bytes+1)
    if len(raw) > max_bytes:
        raise ValueError('Recovery archive guard exceeds max_bytes')
    header = _json(raw)
    path, token = _paths(journal if journal is not None else lease.path.with_suffix('.json'))
    lease_path = path.with_suffix('.lease')
    if (type(header) is not dict or set(header) != {'archive', 'sha256'}
            or type(header['archive']) is not str or not _digest(header['sha256'])
            or not re.fullmatch(r'\.easysewer-recovery-'+token+r'\.'+_TOKEN+r'\.zip', header['archive'])):
        raise ValueError('Invalid recovery archive guard')
    archive_path = path.parent/header['archive']
    payload, archive_identity = _read_file(archive_path, max_bytes)
    if hashlib.sha256(payload).hexdigest() != header['sha256']:
        raise ValueError('Recovery archive content changed')
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            entries = archive.infolist()
            names = [v.filename for v in entries]
            if (len(set(names)) != len(names) or 'manifest.json' not in names
                    or any(v.compress_type != zipfile.ZIP_STORED or v.flag_bits & 1 for v in entries)
                    or sum(v.file_size for v in entries) > max_bytes):
                raise ValueError('Invalid recovery archive members or size')
            data = _json(archive.read('manifest.json'))
            if (type(data) is not dict or set(data) != {'format','version','platform','journal','token',
                    'parent_identity','guard_identity','lease_identity','revoked_lease','reason','files'}
                    or data['format'] != 'easysewer-recovery-artifact-archive' or type(data['version']) is not int
                    or data['version'] != 1 or data['platform'] != os.name or data['journal'] != str(path)
                    or data['token'] != token or not _identity_value(data['parent_identity'])
                    or not _identity_value(data['guard_identity']) or not _identity_value(data['lease_identity'])
                    or tuple(data['parent_identity']) != _identity(path.parent)
                    or tuple(data['guard_identity']) != lease.identity
                    or data['guard_identity'] == data['lease_identity']
                    or type(data['revoked_lease']) is not str or not re.fullmatch(
                        r'\.easysewer-recovery-'+token+r'\.json\.'+_TOKEN+r'\.pending', data['revoked_lease'])
                    or type(data['reason']) is not str or not data['reason'].strip()
                    or type(data['files']) is not list or not data['files']):
                raise ValueError('Invalid recovery archive identity or manifest')
            seen = set()
            for row in data['files']:
                if (type(row) is not dict or set(row) != {'name','identity','sha256','size'}
                        or type(row['name']) is not str or row['name'] in seen
                        or not _identity_value(row['identity']) or not _digest(row['sha256'])
                        or type(row['size']) is not int or row['size'] < 0
                        or row['name'] != lease_path.name and not re.fullmatch(
                            r'\.easysewer-recovery-'+token+r'\.json\.'+_TOKEN+r'\.pending', row['name'])):
                    raise ValueError('Invalid archived recovery file')
                seen.add(row['name'])
                value = archive.read(row['name'])
                if len(value) != row['size'] or hashlib.sha256(value).hexdigest() != row['sha256']:
                    raise ValueError('Archived recovery file content differs')
                if row['name'] == lease_path.name and (value not in (b'', b'0')
                        or row['identity'] != data['lease_identity']):
                    raise ValueError('Invalid archived initial lease')
            if (seen | {'manifest.json'} != set(names) or lease_path.name not in seen
                    or data['revoked_lease'] in seen):
                raise ValueError('Unexpected recovery archive members')
    except (zipfile.BadZipFile, KeyError, TypeError) as error:
        raise ValueError('Malformed recovery archive') from error
    data['_archive_identity'] = archive_identity
    data['_archive_digest'] = header['sha256']
    data['_guard_digest'] = hashlib.sha256(prefix+raw).hexdigest()
    return archive_path, data


def _finish(lease, archive_path, data, max_bytes, *, remove):
    path = Path(data['journal']); token = data['token']
    retained = []; issues = []; conflicts = False
    if path.exists() or path.is_symlink():
        return RecoveryArchive(str(path), str(archive_path), 'archive-conflicted', data['reason'],
            tuple(row['name'] for row in data['files']), (str(path),), ('Canonical journal appeared; retained without changes.',))
    expected = {row['name']: row for row in data['files'] if row['name'] != path.with_suffix('.lease').name}
    for artifact in _pending(path, token):
        row = expected.get(artifact.name)
        try:
            identity = _identity(artifact)
            archive_staging = row is None and identity == data['_archive_identity']
            guard_staging = row is None and identity == lease.identity
            revoked_lease = row is None and artifact.name == data['revoked_lease'] and identity == tuple(data['lease_identity'])
            if row is None and not (archive_staging or guard_staging or revoked_lease):
                raise ValueError(f'Unrecorded pending file retained: {artifact}')
            if guard_staging:
                lease.stream.seek(0); raw = lease.stream.read(max_bytes+len(_MAGIC)+1)
                if hashlib.sha256(raw).hexdigest() != data['_guard_digest']:
                    raise ValueError('Archive guard staging changed')
            else:
                raw, identity = _read_file(artifact, max_bytes)
            if revoked_lease:
                if raw not in (b'', b'0'):
                    raise ValueError('Revoked initial lease content changed')
            elif archive_staging:
                if hashlib.sha256(raw).hexdigest() != data['_archive_digest']:
                    raise ValueError('Archive staging content changed')
            elif not guard_staging and (tuple(row['identity']) != identity or row['size'] != len(raw)
                    or row['sha256'] != hashlib.sha256(raw).hexdigest()):
                raise ValueError(f'Changed pending file retained: {artifact}')
            if remove:
                # Recheck after reading and immediately before removing the archived source.
                if _identity(path.parent) != tuple(data['parent_identity']) or _identity(lease.path) != lease.identity:
                    raise ValueError('Recovery archive ownership changed')
                if path.exists() or path.is_symlink():
                    raise ValueError('Canonical journal appeared before source removal')
                saved, saved_identity = _read_file(archive_path, max_bytes)
                if saved_identity != data['_archive_identity'] or hashlib.sha256(saved).hexdigest() != data['_archive_digest']:
                    raise ValueError('Recovery archive changed before source removal')
                lease.stream.seek(0); guard_raw = lease.stream.read(max_bytes+len(_MAGIC)+1)
                if hashlib.sha256(guard_raw).hexdigest() != data['_guard_digest']:
                    raise ValueError('Recovery archive guard changed before source removal')
                if guard_staging:
                    again, current = guard_raw, _identity(artifact)
                else:
                    again, current = _read_file(artifact, max_bytes)
                if current != identity or again != raw:
                    raise ValueError(f'Pending file changed before removal: {artifact}')
                artifact.unlink()
            else:
                retained.append(str(artifact))
        except FileNotFoundError as error:
            if not artifact.exists() and not artifact.is_symlink():
                continue  # Source already removed by an earlier interrupted attempt.
            retained.append(str(artifact)); issues.append(str(error)); conflicts = True
        except (OSError, ValueError, RuntimeError) as error:
            retained.append(str(artifact)); issues.append(str(error))
            conflicts = conflicts or isinstance(error, ValueError)
    state = 'archive-conflicted' if conflicts else 'archiving' if retained else 'archived'
    return RecoveryArchive(str(path), str(archive_path), state, data['reason'],
        tuple(row['name'] for row in data['files']), tuple(retained), tuple(issues))


def _inspect(lease, *, max_bytes):
    loaded = _load(lease, max_bytes)
    return None if loaded is None else _finish(lease, *loaded, max_bytes, remove=False)


def _prepared(path, max_bytes, original_identity):
    """Find a fully published, identity-bound interrupted archive preparation."""
    _, token = _paths(path)
    matches = []
    try:
        for candidate in _pending(path, token):
            _regular(candidate)
            with candidate.open('rb') as stream:
                if stream.read(len(_MAGIC)) != _MAGIC:
                    continue
            guard = _Lease(candidate, renameable=True)
            try:
                loaded = _load(guard, max_bytes, journal=path)
            except ValueError:
                guard.close()
                continue  # An unpublished partial guard is only opaque evidence.
            except BaseException:
                guard.close()
                raise
            if loaded is None:
                guard.close(); continue
            archive_path, data = loaded
            if original_identity is not None:
                eligible = tuple(data['lease_identity']) == original_identity
            else:
                revoked = path.parent/data['revoked_lease']
                eligible = revoked.exists() and not revoked.is_symlink() and _identity(revoked) == tuple(data['lease_identity'])
            if eligible:
                matches.append((guard, archive_path, data))
            else:
                guard.close()
        if len(matches) > 1:
            raise ValueError('Multiple prepared recovery archives; retained without choosing one')
        return matches[0] if matches else None
    except BaseException:
        for guard, _, _ in matches:
            guard.close()
        raise


def _activate(path, original, guard, archive_path, data, max_bytes):
    """Replace the initial path identity without an unlocked active-owner gap."""
    if path.exists() or path.is_symlink():
        raise ValueError('Canonical journal appeared before archive activation')
    if _identity(path.parent) != tuple(data['parent_identity']):
        raise ValueError('Recovery archive parent changed')
    lease_path = path.with_suffix('.lease')
    revoked = path.parent/data['revoked_lease']
    if original is not None:
        if _identity(lease_path) != original.identity or original.identity != tuple(data['lease_identity']):
            raise ValueError('Initial lease identity changed')
        original.stream.seek(0)
        if original.stream.read(2) not in (b'', b'0'):
            raise ValueError('Initial lease content changed before revocation')
        if revoked.exists() or revoked.is_symlink():
            _regular(revoked)
            if _identity(revoked) != original.identity:
                raise ValueError('Revoked lease staging was replaced')
            lease_path.unlink()
        elif os.name == 'nt':
            # Rename to an absent destination works with the held byte lock.
            # A live legacy creator's non-delete-sharing handle blocks it.
            try:
                os.rename(lease_path, revoked)
            except OSError as error:
                return RecoveryArchive(str(path), str(archive_path), 'archive-blocked', data['reason'],
                    tuple(row['name'] for row in data['files']), tuple(str(path.parent/row['name']) for row in data['files']), (str(error),))
        else:
            os.link(lease_path, revoked)  # Exclusive destination, no overwrite.
            lease_path.unlink()
    else:
        if not revoked.exists() or _identity(revoked) != tuple(data['lease_identity']):
            raise ValueError('Missing initial lease has no verified revocation staging')
        revoked_raw, _ = _read_file(revoked, max_bytes)
        if revoked_raw not in (b'', b'0'):
            raise ValueError('Revoked initial lease changed before guard publication')
    os.link(guard.path, lease_path)  # Exclusive publication protects a replacement path.
    guard.path = lease_path
    if original is not None:
        original.close()
    loaded = _load(guard, max_bytes)
    return _finish(guard, *loaded, max_bytes, remove=True)


def archive(journal, *, reason, max_bytes):
    _journal_budget(max_bytes)
    if type(reason) is not str:
        raise TypeError('reason must be a string')
    if not reason.strip():
        raise ValueError('reason must explain the archival decision')
    path, token = _paths(journal)
    lease = None; guard = None; staging = []; prepared = False
    try:
        try:
            lease = _Lease(path.with_suffix('.lease'), renameable=True)
        except FileNotFoundError:
            resume = _prepared(path, max_bytes, None)
            if resume is None:
                raise
            guard, archive_path, data = resume
            if data['reason'] != reason:
                raise ValueError('Archive reason differs from the recorded decision')
            return _activate(path, None, guard, archive_path, data, max_bytes)
        loaded = _load(lease, max_bytes)
        if loaded is not None:
            if loaded[1]['reason'] != reason:
                raise ValueError('Archive reason differs from the recorded decision')
            return _finish(lease, *loaded, max_bytes, remove=True)
        if path.exists() or path.is_symlink():
            raise ValueError('Canonical journal exists; inspect/recover/retire it instead')
        lease.stream.seek(0); original = lease.stream.read(2)
        if original not in (b'', b'0'):
            raise ValueError('Initial lease has unrecognized content; retained')
        resume = _prepared(path, max_bytes, lease.identity)
        if resume is not None:
            guard, archive_path, data = resume
            if data['reason'] != reason:
                raise ValueError('Archive reason differs from the recorded decision')
            return _activate(path, lease, guard, archive_path, data, max_bytes)
        parent_identity = _identity(path.parent)
        contents = [(lease.path, original, lease.identity)]
        total = len(original)
        for candidate in _pending(path, token):
            raw, identity = _read_file(candidate, max_bytes-total)
            contents.append((candidate, raw, identity)); total += len(raw)
        operation = uuid.uuid4().hex
        guard_path = path.with_name(path.name+'.'+operation+'.pending')
        guard = _Lease(guard_path, create=True, renameable=True)
        staging.append((guard_path, guard.identity))
        data = dict(format='easysewer-recovery-artifact-archive', version=1, platform=os.name,
            journal=str(path), token=token, parent_identity=list(parent_identity), guard_identity=list(guard.identity),
            lease_identity=list(lease.identity), revoked_lease=path.name+'.'+uuid.uuid4().hex+'.pending', reason=reason,
            files=[dict(name=p.name, identity=list(identity), sha256=hashlib.sha256(raw).hexdigest(), size=len(raw))
                   for p, raw, identity in contents])
        pending_zip = path.with_name(path.name+'.'+uuid.uuid4().hex+'.pending')
        with pending_zip.open('xb') as stream:
            staging.append((pending_zip, _identity(pending_zip)))
            with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_STORED) as output:
                output.writestr('manifest.json', json.dumps(data, ensure_ascii=True, separators=(',', ':')))
                for source, raw, _ in contents:
                    output.writestr(source.name, raw)
            stream.flush(); os.fsync(stream.fileno())
        payload, _ = _read_file(pending_zip, max_bytes)
        archive_path = path.parent/('.easysewer-recovery-'+token+'.'+operation+'.zip')
        os.link(pending_zip, archive_path)  # Exclusive publication; never overwrite an existing archive.
        header = dict(archive=archive_path.name, sha256=hashlib.sha256(payload).hexdigest())
        guard.stream.seek(0)
        guard.stream.write(_MAGIC+json.dumps(header, separators=(',', ':')).encode('ascii'))
        guard.stream.flush(); os.fsync(guard.stream.fileno())
        if (_identity(lease.path) != lease.identity or _identity(path.parent) != parent_identity
                or path.exists() or path.is_symlink()):
            raise ValueError('Initial recovery files changed before archive activation')
        prepared = True
        loaded = _load(guard, max_bytes, journal=path)
        result = _activate(path, lease, guard, *loaded, max_bytes)
        if result.state == 'archive-blocked':
            prepared = False  # Source lease never moved; only the completed ZIP copy remains.
        return result
    finally:
        if guard is not None:
            guard.close()
        if lease is not None:
            lease.close()
        for temporary, identity in staging:
            if prepared and temporary == staging[0][0]:
                continue  # A published prepared guard makes interruption resumable.
            if temporary.exists() and not temporary.is_symlink() and _identity(temporary) == identity:
                temporary.unlink()
