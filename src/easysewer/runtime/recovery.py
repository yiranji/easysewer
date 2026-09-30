"""Local process-crash recovery for cooperating Runner output transactions.

The journal is a trusted local ownership record, not a portable result archive
or a security boundary against a writer with access to the same directories.
"""
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import uuid


_DEFAULT_JOURNAL_BYTES = 64*1024*1024
_RESERVATION_BYTES = 128*1024


def _identity(path):
    value = Path(path).lstat()
    return value.st_dev, value.st_ino


def _regular(path):
    if not stat.S_ISREG(Path(path).lstat().st_mode):
        raise ValueError(f'Expected a regular recovery file: {path}')


def _reservation_state(path):
    from ._workspace import fingerprint
    path = Path(path)
    _regular(path)
    before = fingerprint(path)
    with path.open('rb') as stream:
        raw = stream.read(_RESERVATION_BYTES+1)
    if len(raw) > _RESERVATION_BYTES:
        raise ValueError(f'Reservation exceeds size limit: {path}')
    if path.is_symlink() or fingerprint(path) != before or len(raw) != before[2]:
        raise ValueError(f'Reservation changed during reading: {path}')
    return before, hashlib.sha256(raw).hexdigest(), len(raw), raw


class _Lease:
    def __init__(self, path, *, create=False, renameable=False):
        self.path = Path(path)
        if not create:
            _regular(self.path)
        if renameable and os.name == 'nt':
            from ._recovery_archive import _windows_open
            self.stream = _windows_open(self.path, create=create)
        else:
            self.stream = self.path.open('x+b' if create else 'r+b')
        try:
            if create:
                self.stream.write(b'0'); self.stream.flush(); os.fsync(self.stream.fileno())
            self.stream.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            elif os.name == 'posix':
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:
                raise NotImplementedError('Run recovery requires Windows or POSIX file locking')
            value = os.fstat(self.stream.fileno())
            self.identity = value.st_dev, value.st_ino
            if _identity(self.path) != self.identity:
                raise ValueError('Recovery lease was replaced')
        except BaseException:
            self.stream.close()
            raise

    def close(self):
        self.stream.close()  # The OS releases the lock on normal or process exit.


def _write(path, data):
    temporary = path.with_name(path.name+'.'+uuid.uuid4().hex+'.pending')
    try:
        with temporary.open('xb') as stream:
            stream.write(json.dumps(data, ensure_ascii=True, separators=(',', ':')).encode())
            stream.flush(); os.fsync(stream.fileno())
        try:
            os.replace(temporary, path)
        except PermissionError as error:
            if os.name != 'nt' or getattr(error, 'winerror', None) not in (5, 32):
                raise
            # Classic Windows rename rejects an open destination even when
            # its reader shares deletion. Keep atomic replacement in that case.
            from ._recovery_archive import _windows_replace
            _windows_replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _json_value(value):
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (tuple, list)):
        return [_json_value(v) for v in value]
    if isinstance(value, dict):
        return {k: _json_value(v) for k, v in value.items()}
    return value


class _Journal:
    def __init__(self, parent, transaction):
        token = uuid.uuid4().hex
        self.path = Path(parent)/('.easysewer-recovery-'+token+'.json')
        self.lease = _Lease(self.path.with_suffix('.lease'), create=True)
        self.data = dict(format='easysewer-run-recovery', version=5, platform=os.name,
            run_id=transaction.run_id, pid=os.getpid(), token=token,
            lease_identity=self.lease.identity, parents={}, phase='reserving',
            committed=False, targets=[], workspaces=[], issues=[], recovered=[], failure=None)
        try:
            self.sync(transaction)
        except BaseException:
            self.lease.close()
            self.lease.path.unlink()
            raise

    def sync(self, transaction, *, phase=None):
        if phase is not None:
            self.data['phase'] = phase
        self.data['committed'] = transaction.committed
        targets=[_json_value(asdict(t)) for t in transaction.targets]
        if self.data['version']<7 and any(t['directory'] and (t['expected'] is not None or t['directory_cleanup']) for t in targets):
            self.data['version']=7
        if self.data['version']<7:
            for target in targets:target.pop('directory_cleanup')
        self.data['targets'] = targets
        self.data['issues'] = list(transaction.issues)
        for target in transaction.targets:
            parent = target.path.parent
            self.data['parents'].setdefault(str(parent), _identity(parent))
        self.data['parents'].setdefault(str(self.path.parent), _identity(self.path.parent))
        _write(self.path, self.data)

    def workspace(self, workspace, transaction, *, cleanup_on_crash=False):
        self.data['workspaces'].append(dict(path=str(workspace.root), identity=workspace.identity,
            cleanup_requested=False, cleanup_content=None, cleaned=False,
            cleanup_on_crash=cleanup_on_crash, execution='not-started', worker=None, admission=None))
        self.data['parents'].setdefault(str(workspace.root.parent), _identity(workspace.root.parent))
        self.sync(transaction, phase='executing')

    def prepare_execution(self, workspace, transaction):
        from ._admission import create
        item = next(w for w in self.data['workspaces'] if w['path'] == str(workspace.root))
        if item['execution']!='not-started' or item['admission'] is not None:
            raise ValueError('Execution admission was already prepared')
        item['admission']=create(workspace.root)
        self.sync(transaction)
        return _json_value(item['admission'])

    def workspace_execution(self, workspace, transaction, *, state, identity=None):
        from ._worker_identity import validate
        if state not in ('starting','active'):
            raise ValueError('Invalid workspace execution transition')
        item = next(w for w in self.data['workspaces'] if w['path'] == str(workspace.root))
        if state=='starting' and item['execution']!='not-started':
            raise ValueError('Workspace execution already requested')
        if state=='active' and item['execution']!='starting':
            raise ValueError('Workspace execution was not requested')
        item['execution']=state
        item['worker']=validate(identity) if identity is not None else None
        self.sync(transaction, phase='open')

    def revoke_execution(self, workspace, transaction):
        from ._admission import revoke
        item = next(w for w in self.data['workspaces'] if w['path'] == str(workspace.root))
        if item['admission'] is None:
            return False
        revoke(item['admission'])
        self.sync(transaction, phase='cleanup')
        return True

    def workspace_cleanup(self, workspace, transaction, *, requested=False, completed=False):
        from ._workspace import content_state, fingerprint
        item = next(w for w in self.data['workspaces'] if w['path'] == str(workspace.root))
        item['cleanup_requested'] = True
        if requested:
            self.sync(transaction, phase='cleanup')
            return
        if completed:
            if item['cleanup_content'] is None or workspace.root.exists() or workspace.root.is_symlink():
                raise ValueError('Workspace cleanup has not completed')
            item['cleaned'] = True
        else:
            if workspace.root.is_symlink() or fingerprint(workspace.root)[:2] != workspace.identity:
                raise ValueError('Workspace identity changed before cleanup')
            if item['admission'] is not None:
                from ._admission import revoke
                revoke(item['admission'])
            item['cleanup_content'] = _json_value(content_state(workspace.root, directory=True))
        self.sync(transaction, phase='cleanup')

    def close(self, *, remove):
        try:
            if remove:
                self.path.unlink()
        finally:
            self.lease.close()
        if remove and self.lease.path.exists() and _identity(self.lease.path) == self.lease.identity:
            self.lease.path.unlink()


@dataclass(frozen=True)
class RunRecovery:
    journal: str
    run_id: str
    state: str
    phase: str
    committed: bool
    targets: tuple[str, ...]
    workspaces: tuple[str, ...]
    issues: tuple[str, ...] = ()
    failure: tuple[str, str, str] | None = None
    cleaned_workspaces: tuple[str, ...] = ()
    retirement_reason: str | None = None
    released_reservations: tuple[str, ...] = ()
    retained_reservations: tuple[str, ...] = ()
    retained_artifacts: tuple[str, ...] = ()


@dataclass(frozen=True)
class RecoveryArchive:
    journal: str
    archive: str
    state: str
    reason: str
    members: tuple[str, ...]
    retained_files: tuple[str, ...] = ()
    issues: tuple[str, ...] = ()


@dataclass(frozen=True)
class RunRecoveryFiles:
    """Advisory directory observation, never permission to remove artifacts.

    ``journal`` is the expected canonical path and may not exist. ``files``
    contains the matching names seen during discovery, including unpublished
    pending writes. Only ``recovery`` contains validated canonical run data.
    """
    journal: str
    files: tuple[str, ...]
    state: str
    recovery: RunRecovery | None = None
    issues: tuple[str, ...] = ()
    archive: RecoveryArchive | None = None


def _journal_budget(max_bytes):
    if type(max_bytes) is not int:
        raise TypeError('max_bytes must be an integer')
    if not 0 < max_bytes < sys.maxsize:
        raise ValueError('max_bytes must be positive and smaller than sys.maxsize')


def discover_run_recovery(directory, *, max_bytes=_DEFAULT_JOURNAL_BYTES):
    """Inspect immediate recovery files without changing their bytes or names.

    Group exact journal/lease/pending names by token. Pending JSON is never
    interpreted as a committed journal. An incomplete group is not proof of an
    abandoned owner: the initial writer might not yet have acquired its lease.
    Busy or inaccessible leases and malformed artifacts remain explicit issues.
    The returned observations are advisory; recovery revalidates ownership.
    """
    _journal_budget(max_bytes)
    root = Path(directory).resolve(strict=True)
    if not root.is_dir():
        raise NotADirectoryError(str(root))
    identity = _identity(root)
    pattern = re.compile(r'\.easysewer-recovery-([0-9a-f]{32})\.(?:json|lease|json\.[0-9a-f]{32}\.pending|[0-9a-f]{32}\.zip)')
    groups = {}
    for entry in root.iterdir():
        match = pattern.fullmatch(entry.name)
        if match:
            groups.setdefault(match[1], []).append(entry)
    results = []
    for token, entries in sorted(groups.items()):
        path = root/('.easysewer-recovery-'+token+'.json')
        lease_path = path.with_suffix('.lease')
        files = tuple(str(p) for p in sorted(entries))
        lease = None
        try:
            for entry in entries:
                _regular(entry)
            # A missing canonical file is deliberately not reconstructed from
            # pending data, even when that data is valid JSON.
            if lease_path not in entries:
                state = 'unavailable' if path in entries else 'incomplete'
                results.append(RunRecoveryFiles(str(path), files, state, issues=(
                    'Recovery lease is missing; ownership cannot be established.',)))
                continue
            try:
                lease = _Lease(lease_path)
            except (BlockingIOError, PermissionError) as error:
                results.append(RunRecoveryFiles(str(path), files, 'busy-or-inaccessible',
                    issues=(f'Recovery lease is held or inaccessible: {error}',)))
                continue
            from ._recovery_archive import _inspect
            archived = _inspect(lease, max_bytes=max_bytes)
            if archived is not None:
                results.append(RunRecoveryFiles(str(path), files, archived.state,
                    issues=archived.issues, archive=archived))
                continue
            if path not in entries:
                results.append(RunRecoveryFiles(str(path), files, 'incomplete', issues=(
                    'Canonical journal was not observed; pending writes are not recovery authority. '
                    'This does not prove that an initial writer has exited.',)))
                continue
            path, data = _read(path, max_bytes=max_bytes)
            if lease.identity != tuple(data['lease_identity']):
                raise ValueError('Recovery lease identity changed')
            state = _inspection_state(data)
            recovery = _result(path, data, state,
                (*data['issues'], *data.get('retirement', {}).get('issues', [])))
            results.append(RunRecoveryFiles(str(path), files,
                'recoverable' if state == 'interrupted' else state, recovery, recovery.issues))
        except (OSError, ValueError, RuntimeError, NotImplementedError) as error:
            results.append(RunRecoveryFiles(str(path), files, 'unavailable',
                issues=(f'{type(error).__name__}: {error}',)))
        finally:
            if lease is not None:
                lease.close()
    if _identity(root) != identity:
        raise ValueError('Recovery directory changed during discovery')
    return tuple(results)


def archive_run_recovery_files(journal, *, reason, max_bytes=_DEFAULT_JOURNAL_BYTES):
    """Archive incomplete initial lease/pending files with a durable guard.

    Requires an existing empty/one-byte initial lease and no canonical journal.
    Saves original bytes before changing lease identity, then removes only
    matching archived pending files. The archive and guard remain as evidence.
    Repeated calls continue a recorded operation without affecting outputs or
    adopting unpublished JSON as a run journal.
    """
    from ._recovery_archive import archive
    return archive(journal, reason=reason, max_bytes=max_bytes)


def _read(path, *, max_bytes=_DEFAULT_JOURNAL_BYTES, retaining=False):
    _journal_budget(max_bytes)
    path = Path(path).absolute()
    _regular(path)
    # Inspection must not block the live owner's atomic journal replacement.
    # The shared read handle retains the opened version while the name changes.
    if os.name == 'nt':
        from ._recovery_archive import _windows_open
        stream = _windows_open(path, create=False, read_only=True)
    else:
        stream = path.open('rb')
    with stream:
        raw = stream.read(max_bytes+1)
    if len(raw) > max_bytes:
        raise ValueError(f'Recovery journal exceeds max_bytes ({max_bytes} bytes)')
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('Duplicate recovery journal member')
            result[key] = value
        return result
    data = json.loads(raw, object_pairs_hook=pairs)

    def numbers(value, size):
        return type(value) is list and len(value) == size and all(type(v) is int for v in value)

    def content(value, directory):
        if type(value) is not list or not value:
            raise ValueError('Invalid recovery content evidence')
        names = set()
        for row in value:
            if (type(row) is not list or len(row) not in (3, 5) or type(row[0]) is not str
                    or row[0] in names or not numbers(row[2], 2)):
                raise ValueError('Invalid recovery content record')
            names.add(row[0])
            if row[1] == 'directory' and len(row) == 3 and directory:
                continue
            if (row[1] != 'file' or len(row) != 5 or type(row[3]) is not str
                    or len(row[3]) != 64 or any(c not in '0123456789abcdef' for c in row[3])
                    or type(row[4]) is not int or row[4] < 0):
                raise ValueError('Invalid recovery file evidence')
        if value[0][0] != '' or value[0][1] != ('directory' if directory else 'file') or (not directory and len(value) != 1):
            raise ValueError('Invalid recovery content root')

    try:
        members = {'format', 'version', 'platform', 'run_id', 'pid', 'token',
            'lease_identity', 'parents', 'phase', 'committed', 'targets', 'workspaces', 'issues', 'recovered', 'failure'}
        if type(data) is dict and data.get('version') in (6,8):
            members.add('retirement')
        if (type(data) is not dict or set(data) != members
                or type(data['run_id']) is not str or type(data['pid']) is not int
                or type(data['phase']) is not str or not numbers(data['lease_identity'], 2)
                or type(data['targets']) is not list or type(data['workspaces']) is not list
                or type(data['parents']) is not dict or type(data['recovered']) is not list
                or not all(type(s) is str for s in data['recovered'])
                or len(set(data['recovered'])) != len(data['recovered'])
                or type(data['issues']) is not list or not all(type(s) is str for s in data['issues'])
                or (data['failure'] is not None and (type(data['failure']) is not list or len(data['failure']) != 3
                    or not all(type(s) is str for s in data['failure'])))):
            raise ValueError('Malformed recovery journal')
        if (data['format'] != 'easysewer-run-recovery' or type(data['version']) is not int
                or data['version'] not in (1, 2, 3, 4, 5, 6, 7, 8) or data['platform'] != os.name
                or type(data['committed']) is not bool
                or path.name != '.easysewer-recovery-'+data['token']+'.json'
                or len(data['token']) != 32 or any(c not in '0123456789abcdef' for c in data['token'])):
            raise ValueError('Unsupported recovery journal identity or platform')
        historical_parents = retaining or data['version'] in (6,8)
        for parent, identity in data['parents'].items():
            p = Path(parent)
            if (not numbers(identity, 2) or not p.is_absolute() or '..' in p.parts
                    or not historical_parents and (p.resolve() != p or not p.is_dir() or _identity(p) != tuple(identity))):
                raise ValueError(f'Recovery parent was moved or replaced: {p}')
        if historical_parents and str(path.parent) not in data['parents']:
            raise ValueError('Retirement journal parent is not recorded')
        seen = set()
        for target in data['targets']:
            members = {'path', 'expected', 'directory', 'lock', 'lock_identity',
                'temporary', 'temporary_identity', 'backup', 'published', 'expected_content', 'prepared_content'}
            if data['version'] >= 2:
                members |= {'reservation_source', 'lock_content'}
            if data['version']>=7:members.add('directory_cleanup')
            if type(target) is not dict or set(target) != members:
                raise ValueError('Malformed recovery target')
            for field, length in (('expected', 5), ('published', 5), ('lock_identity', 2), ('temporary_identity', 2)):
                if target[field] is not None and not numbers(target[field], length):
                    raise ValueError('Invalid recovery file identity')
            p = Path(target['path'])
            key = hashlib.sha256(os.path.normcase(str(p)).encode()).hexdigest()[:32]
            if (not p.is_absolute() or '..' in p.parts or not historical_parents and p.parent.resolve() != p.parent
                    or str(p.parent) not in data['parents'] or str(p) in seen
                    or target['lock'] != str(p.parent/('.easysewer-lock-'+key))
                    or type(target['directory']) is not bool):
                raise ValueError('Invalid recovery target')
            seen.add(str(p))
            if data['version'] >= 2:
                seed = target['reservation_source']; bound = target['lock_content']
                if seed is not None:
                    seed = Path(seed)
                    if (seed.parent != p.parent or not seed.name.startswith('.easysewer-reserve-')
                            or len(seed.name) != len('.easysewer-reserve-')+32):
                        raise ValueError('Invalid reservation source location')
                if bound is not None and (type(bound) is not list or len(bound) != 2
                        or type(bound[0]) is not str or len(bound[0]) != 64
                        or any(c not in '0123456789abcdef' for c in bound[0])
                        or type(bound[1]) is not int or not 0 < bound[1] <= _RESERVATION_BYTES):
                    raise ValueError('Invalid reservation content evidence')
                if seed is not None and bound is None:
                    raise ValueError('Reservation source requires content evidence')
            if target['expected_content'] is not None:
                content(target['expected_content'], target['directory'] if data['version']>=7 else False)
                if target['expected'] is None or target['expected_content'][0][2] != target['expected'][:2]:
                    raise ValueError('Original content identity differs')
            elif target['expected'] is not None:
                raise ValueError('Missing original content evidence')
            if target['prepared_content'] is not None:
                content(target['prepared_content'], target['directory'])
                if target['temporary'] is None or target['prepared_content'][0][2] != target['temporary_identity']:
                    raise ValueError('Prepared content identity differs')
            if data['version']>=7:
                cleanup=target['directory_cleanup']
                if type(cleanup) is not dict or set(cleanup)-{'backup','temporary'} or cleanup and not target['directory']:
                    raise ValueError('Invalid directory cleanup authorization')
                for slot,evidence in cleanup.items():
                    content(evidence,True)
                    identity=target['expected'][:2] if slot=='backup' and target['expected'] is not None else target['temporary_identity'] if slot=='temporary' else None
                    if target[slot] is None or evidence[0][2]!=identity:
                        raise ValueError('Directory cleanup identity differs')
                    expected=target['expected_content'] if slot=='backup' else target['prepared_content']
                    if expected is not None and evidence!=expected or slot=='backup' and not data['committed']:
                        raise ValueError('Directory cleanup evidence or commit differs')
            if target['backup'] is not None and target['expected'] is None:
                raise ValueError('Backup without original content')
            for field, prefix in (('temporary', '.easysewer-publish-'), ('backup', '.easysewer-backup-')):
                value = target[field]
                if value is not None:
                    other = Path(value)
                    if other.parent != p.parent or not other.name.startswith(prefix) or len(other.name) != len(prefix)+32:
                        raise ValueError('Invalid recovery artifact location')
        if not set(data['recovered']).issubset(seen):
            raise ValueError('Unknown recovered target')
        if data['version'] in (6,8):
            retirement = data['retirement']
            if (type(retirement) is not dict or set(retirement) != {'reason', 'complete', 'reservations', 'issues'}
                    or type(retirement['reason']) is not str or not retirement['reason'].strip()
                    or type(retirement['complete']) is not bool or type(retirement['reservations']) is not list
                    or type(retirement['issues']) is not list
                    or not all(type(issue) is str for issue in retirement['issues'])):
                raise ValueError('Invalid retirement decision')
            targets = {t['path']: t['lock'] for t in data['targets']}
            recorded = set()
            for row in retirement['reservations']:
                if (type(row) is not dict or set(row) != {'target', 'path', 'state', 'evidence', 'issue'}
                        or type(row['target']) is not str or row['target'] not in targets
                        or row['target'] in recorded or row['path'] != targets[row['target']]
                        or row['state'] not in ('pending', 'released', 'absent', 'retained')
                        or row['issue'] is not None and type(row['issue']) is not str):
                    raise ValueError('Invalid retirement reservation')
                recorded.add(row['target'])
                bound = row['evidence']
                if bound is not None and (type(bound) is not list or len(bound) != 3
                        or not numbers(bound[0], 2) or type(bound[1]) is not str or len(bound[1]) != 64
                        or any(c not in '0123456789abcdef' for c in bound[1])
                        or type(bound[2]) is not int or not 0 < bound[2] <= _RESERVATION_BYTES):
                    raise ValueError('Invalid retirement reservation evidence')
                if row['state'] in ('pending', 'released') and bound is None:
                    raise ValueError('Missing retirement reservation evidence')
                if row['state'] == 'retained' and not row['issue']:
                    raise ValueError('Retained reservation lacks diagnostic')
            if retirement['complete'] and (recorded != set(targets)
                    or any(row['state'] == 'pending' for row in retirement['reservations'])):
                raise ValueError('Incomplete retirement marked complete')
        workspace_paths = set()
        for item in data['workspaces']:
            members = {'path', 'identity'}
            if data['version'] >= 3:
                members |= {'cleanup_requested', 'cleanup_content', 'cleaned'}
            if data['version'] >= 4:
                members |= {'cleanup_on_crash', 'execution', 'worker'}
            if data['version'] >= 5:
                members.add('admission')
            if (type(item) is not dict or set(item) != members or not numbers(item['identity'], 2)
                    or not Path(item['path']).is_absolute() or item['path'] in workspace_paths):
                raise ValueError('Invalid workspace record')
            workspace_paths.add(item['path'])
            if data['version'] >= 5 and item['admission'] is not None:
                from ._admission import validate
                validate(item['admission'],item['path'])
                workspace=Path(item['path'])
                if (str(workspace.parent) not in data['parents'] or not workspace.name.startswith('.easysewer-')
                        or item['cleanup_content'] is not None and not item['admission']['revoked']):
                    raise ValueError('Invalid execution admission ownership')
            if data['version'] >= 4:
                if (type(item['cleanup_on_crash']) is not bool
                        or item['execution'] not in ('not-started','starting','active','unknown')):
                    raise ValueError('Invalid workspace execution record')
                if item['worker'] is not None:
                    from ._worker_identity import validate
                    validate(item['worker'])
                    if item['execution']!='active':raise ValueError('Unexpected workspace worker')
                if item['cleanup_on_crash']:
                    workspace=Path(item['path'])
                    if str(workspace.parent) not in data['parents'] or not workspace.name.startswith('.easysewer-'):
                        raise ValueError('Invalid crash cleanup ownership')
            if data['version'] >= 3:
                if type(item['cleaned']) is not bool or type(item['cleanup_requested']) is not bool:
                    raise ValueError('Invalid workspace cleanup status')
                evidence = item['cleanup_content']
                if evidence is not None:
                    content(evidence, True)
                    workspace = Path(item['path'])
                    if (not item['cleanup_requested'] or evidence[0][2] != item['identity'] or str(workspace.parent) not in data['parents']
                            or not workspace.name.startswith('.easysewer-')):
                        raise ValueError('Invalid workspace cleanup ownership')
                elif item['cleaned']:
                    raise ValueError('Workspace cleanup lacks ownership evidence')
    except (KeyError, TypeError) as error:
        raise ValueError('Malformed recovery journal') from error
    if data['version'] == 1:
        # Legacy reservations have no prewritten source or complete byte digest.
        # Preserve their existing marker/identity requirements on migration.
        for target in data['targets']:
            target.update(reservation_source=None, lock_content=None)
    if data['version'] < 3:
        for item in data['workspaces']:
            item.update(cleanup_requested=False, cleanup_content=None, cleaned=False)
    if data['version'] < 4:
        for item in data['workspaces']:
            item.update(cleanup_on_crash=False, execution='unknown', worker=None)
    if data['version'] < 5:
        for item in data['workspaces']:
            item['admission']=None
        data['version'] = 5
    return path, data


def _result(path, data, state, issues=()):
    retirement = data.get('retirement')
    rows = retirement['reservations'] if retirement is not None else ()
    artifacts = (tuple(dict.fromkeys(
        [t[key] for t in data['targets'] for key in ('backup', 'temporary', 'reservation_source') if t[key]]
        + [w['path'] for w in data['workspaces']])) if retirement is not None else ())
    return RunRecovery(str(path), data['run_id'], state, data['phase'], data['committed'],
        tuple(t['path'] for t in data['targets']), tuple(w['path'] for w in data['workspaces']), tuple(issues),
        tuple(data['failure']) if data['failure'] is not None else None,
        tuple(w['path'] for w in data['workspaces'] if w['cleaned']),
        retirement['reason'] if retirement is not None else None,
        tuple(row['path'] for row in rows if row['state'] == 'released'),
        tuple(row['path'] for row in rows if row['state'] == 'retained'), artifacts)


def _inspection_state(data):
    retirement = data.get('retirement')
    if retirement is None:
        return 'interrupted'
    return 'retired' if retirement['complete'] else 'retiring'


def inspect_run_recovery(journal, *, max_bytes=_DEFAULT_JOURNAL_BYTES):
    """Read a local journal and probe its OS lease without changing run files."""
    path, data = _read(journal, max_bytes=max_bytes)
    try:
        lease = _Lease(path.with_suffix('.lease'))
    except (BlockingIOError, PermissionError):
        return _result(path, data, 'active', data['issues'])
    try:
        if lease.identity != tuple(data['lease_identity']):
            raise ValueError('Recovery lease identity changed')
        path, data = _read(path, max_bytes=max_bytes)
        if lease.identity != tuple(data['lease_identity']):
            raise ValueError('Recovery lease identity changed')
        return _result(path, data, _inspection_state(data),
            (*data['issues'], *data.get('retirement', {}).get('issues', [])))
    finally:
        lease.close()


def _reservation_evidence(p, target, journal, token):
    stamp, digest, size, raw = _reservation_state(p)
    if target['lock_identity'] is not None and stamp[:2] != tuple(target['lock_identity']):
        raise ValueError(f'Reservation identity changed: {p}')
    if target['lock_content'] is not None and [digest, size] != target['lock_content']:
        raise ValueError(f'Reservation content changed: {p}')
    value = json.loads(raw)
    if (type(value) is not dict or value.get('journal') != str(journal)
            or value.get('token') != token or value.get('target') != target['path']):
        raise ValueError(f'Reservation changed: {p}')
    return stamp[:2], digest, size


def _retire_locked(path, data):
    """Continue a durable retain decision, never enter rollback or cleanup."""
    decision = data['retirement']
    if decision['complete']:
        return _result(path, data, 'retired', (*data['issues'], *decision['issues']))
    rows = {row['target']: row for row in decision['reservations']}
    issues = []
    def check_parent(lock):
        parent = lock.parent
        if (parent.resolve() != parent or not parent.is_dir()
                or _identity(parent) != tuple(data['parents'][str(parent)])):
            raise ValueError(f'Reservation parent changed; reservation retained: {lock}')
    for target in data['targets']:
        row = rows.get(target['path'])
        if row is not None and row['state'] != 'pending':
            continue  # Never inspect or release a later run's reservation.
        lock = Path(target['lock'])
        try:
            check_parent(lock)
            if not lock.exists() and not lock.is_symlink():
                if row is None:
                    row = dict(target=target['path'], path=str(lock), state='absent', evidence=None, issue=None)
                    decision['reservations'].append(row); rows[target['path']] = row
                else:
                    row.update(state='absent', issue=None)
                _write(path, data)
                continue
            observed = _json_value(_reservation_evidence(lock, target, path, data['token']))
            if row is not None and row['evidence'] != observed:
                raise ValueError(f'Reservation changed since retirement release was prepared: {lock}')
            if row is None:
                row = dict(target=target['path'], path=str(lock), state='pending', evidence=observed, issue=None)
                decision['reservations'].append(row); rows[target['path']] = row
                _write(path, data)  # Bind identity and bytes before release, including legacy locks.
            if _json_value(_reservation_evidence(lock, target, path, data['token'])) != observed:
                raise ValueError(f'Reservation changed before retirement release: {lock}')
            check_parent(lock)
            lock.unlink()
            row.update(state='released', issue=None)
            _write(path, data)
        except ValueError as error:
            if row is None:
                row = dict(target=target['path'], path=str(lock), state='retained', evidence=None, issue=str(error))
                decision['reservations'].append(row); rows[target['path']] = row
            else:
                row.update(state='retained', issue=str(error))
            _write(path, data)
        except (OSError, RuntimeError) as error:
            issues.append(str(error))  # A transient access/release failure remains retryable.
    decision['issues'] = [row['issue'] for row in decision['reservations'] if row['issue']] + issues
    decision['complete'] = (len(rows) == len(data['targets'])
        and all(row['state'] != 'pending' for row in rows.values()) and not issues)
    _write(path, data)
    return _result(path, data, _inspection_state(data), (*data['issues'], *decision['issues']))


def retire_run_recovery(journal, *, reason, max_bytes=_DEFAULT_JOURNAL_BYTES):
    """Explicitly retain current artifacts and end automatic run recovery.

    Persist a versioned retain decision before releasing verified reservations.
    Outputs, backups, partial copies and workspaces are retained without being
    inspected, altered or deleted. Unknown/replaced reservations are retained
    with diagnostics. Access failures remain retryable via this function or
    recover_run; neither can roll back a persisted retirement decision.
    The journal and lease remain as the local receipt. This does not stop workers
    or authorize later deletion of retained artifacts.
    """
    if type(reason) is not str:
        raise TypeError('reason must be a string')
    if not reason.strip():
        raise ValueError('reason must explain the explicit retain decision')
    path, data = _read(journal, max_bytes=max_bytes, retaining=True)
    lease = _Lease(path.with_suffix('.lease'))
    try:
        path, data = _read(path, max_bytes=max_bytes, retaining=True)
        if lease.identity != tuple(data['lease_identity']):
            raise ValueError('Recovery lease identity changed')
        if 'retirement' in data:
            if data['retirement']['reason'] != reason:
                raise ValueError('Retirement reason differs from the recorded decision')
        else:
            data['version'] = 8 if data['version']>=7 else 6
            data['retirement'] = dict(reason=reason, complete=False, reservations=[], issues=[])
            _write(path, data)  # Old readers reject before any reservation is released.
        return _retire_locked(path, data)
    finally:
        lease.close()


def recover_run(journal, *, max_bytes=_DEFAULT_JOURNAL_BYTES):
    """Roll back an interrupted publication, or finish committed cleanup.

    Refuses an active owner. Conflicts retain their locks, journal and backups.
    Workspaces with a flushed post-execution cleanup inventory can be reclaimed.
    Unverified or deliberately retained execution workspaces are preserved.
    Recovery may be retried after an interruption or a resolved conflict.
    """
    from ._workspace import content_state, remove_owned_tree
    path, data = _read(journal, max_bytes=max_bytes)
    lease = _Lease(path.with_suffix('.lease'))  # Active owner/recoverer: no writes.
    issues = []
    try:
        path, data = _read(path, max_bytes=max_bytes)  # Re-read under ownership.
        if lease.identity != tuple(data['lease_identity']):
            raise ValueError('Recovery lease identity changed')
        if 'retirement' in data:
            return _retire_locked(path, data)

        def exists(p):
            return p is not None and (p.exists() or p.is_symlink())

        def matches(p, evidence, directory=False):
            return (exists(p) and evidence is not None and
                    _json_value(content_state(p, directory=directory)) == evidence)

        def reservation(p, target):
            return _reservation_evidence(p, target, path, data['token'])

        def clean_directory(target,slot,evidence):
            from ._publication_directory import remove
            if slot not in target.get('directory_cleanup',{}):
                if data['version']<7:
                    data['version']=7
                    for row in data['targets']:row['directory_cleanup']={}
                target['directory_cleanup'][slot]=evidence
                _write(path,data)
            remove(Path(target[slot]),target['directory_cleanup'][slot])

        for target in reversed(data['targets']):
            destination = Path(target['path']); lock = Path(target['lock'])
            temporary = Path(target['temporary']) if target['temporary'] else None
            backup = Path(target['backup']) if target['backup'] else None
            seed = Path(target['reservation_source']) if target['reservation_source'] else None
            try:
                resolved = str(destination) in data['recovered']
                observed_lock = None
                if exists(lock):
                    observed_lock = reservation(lock, target)
                elif not resolved and not (seed is not None and temporary is None and backup is None
                        and target['prepared_content'] is None):
                    raise ValueError(f'Reservation is missing: {lock}')
                if exists(seed):
                    reservation(seed, target)
                    seed.unlink()
                if not resolved:
                    original = target['expected_content']; prepared = target['prepared_content']
                    # No destructive publication is possible before prepared
                    # content and the planned backup are flushed to the journal.
                    if prepared is not None:
                        old_here = matches(destination, original, target['directory'])
                        new_here = matches(destination, prepared, target['directory'])
                        if exists(backup):
                            authorized=target.get('directory_cleanup',{}).get('backup')
                            if authorized is not None:
                                from ._publication_directory import remaining
                                remaining(backup,authorized)
                            elif not matches(backup,original,target['directory']):
                                raise ValueError(f'Backup changed; retained: {backup}')
                        if data['committed']:
                            if not new_here:
                                raise ValueError(f'Committed output changed; retained: {destination}')
                        else:
                            if exists(destination) and not old_here and not new_here:
                                raise ValueError(f'Output changed; retained: {destination}')
                            if original is not None and not old_here and not exists(backup):
                                raise ValueError(f'Original backup missing: {destination}')
                            if new_here:
                                if exists(temporary):
                                    raise ValueError(f'Publication temporary is occupied: {temporary}')
                                # Move aside before restoring the old output; a
                                # crash during later tree cleanup cannot leave
                                # the destination only partly removed.
                                os.replace(destination, temporary)
                            if exists(backup):
                                if exists(destination):
                                    raise ValueError(f'Cannot restore over existing output: {destination}')
                                os.replace(backup, destination)
                        if data['committed'] and exists(backup):
                            if target['directory']:clean_directory(target,'backup',original)
                            else:backup.unlink()
                    if exists(temporary):
                        authorized=target.get('directory_cleanup',{}).get('temporary')
                        if authorized is None and not matches(temporary, prepared, target['directory']):
                            raise ValueError(f'Incomplete or changed temporary retained: {temporary}')
                        if target['directory']:clean_directory(target,'temporary',authorized if authorized is not None else prepared)
                        else:temporary.unlink()
                    data['recovered'].append(str(destination))
                    _write(path, data)  # Record completion before releasing lock.
                if exists(lock):
                    # Hashing outputs and flushing the completion record can
                    # take time. Never release a replacement reservation.
                    if reservation(lock, target) != observed_lock:
                        raise ValueError(f'Reservation changed before release: {lock}')
                    lock.unlink()
            except (OSError, ValueError, RuntimeError) as error:
                issues.append(str(error))
        for item in data['workspaces']:
            if item['cleaned'] or not (item['cleanup_requested'] or item['cleanup_on_crash']):
                continue
            workspace = Path(item['path'])
            try:
                if item['admission'] is not None:
                    from ._admission import revoke
                    revoke(item['admission'],allow_missing=item['cleanup_content'] is not None)
                    _write(path,data)  # Persist revocation before cleanup inventory/deletion.
                if item['cleanup_content'] is None:
                    if not item['cleanup_on_crash']:
                        raise ValueError(f'Workspace cleanup lacks verified post-execution inventory: {workspace}')
                    if item['execution']!='not-started' and item['admission'] is None:
                        from ._worker_identity import has_exited
                        if item['execution']!='active' or item['worker'] is None:
                            raise ValueError(f'Workspace worker identity is incomplete: {workspace}')
                        if not has_exited(item['worker']):
                            raise ValueError(f'Workspace worker is still active: {workspace}')
                    observed=(_json_value(content_state(workspace,directory=True)) if exists(workspace)
                              else [['','directory',item['identity']]])
                    if observed[0][2]!=item['identity']:
                        raise ValueError(f'Workspace root was replaced: {workspace}')
                    item['cleanup_requested']=True
                    item['cleanup_content']=observed
                    _write(path,data)  # Authorize only after exit/ownership proof, before deleting.
                if exists(workspace):
                    expected = {row[0]: row for row in item['cleanup_content']}
                    observed = _json_value(content_state(workspace, directory=True))
                    # A previous deletion may have stopped after any file or
                    # directory. Remaining entries must be unchanged members.
                    if any(expected.get(row[0]) != row for row in observed):
                        raise ValueError(f'Workspace changed after cleanup was authorized: {workspace}')
                    remove_owned_tree(workspace, parent=workspace.parent, identity=tuple(item['identity']))
                item['cleaned'] = True
                _write(path, data)
            except (OSError, ValueError, RuntimeError) as error:
                issues.append(str(error))
        if issues:
            data['issues'] = issues
            _write(path, data)
            return _result(path, data, 'conflicted', issues)
        result = _result(path, data, 'recovered')
        path.unlink()
    finally:
        lease.close()
    if lease.path.exists() and _identity(lease.path) == lease.identity:
        lease.path.unlink()
    return result
