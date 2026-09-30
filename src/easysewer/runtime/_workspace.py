"""Private run storage and cooperative, rollback-capable output publication."""

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import uuid

from .recovery import _Journal, _RESERVATION_BYTES, _reservation_state


def fingerprint(path):
    value = Path(path).stat()
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


def digest_file(path, *, checkpoint=lambda: None, expected_size=None):
    """Hash a fixed observed byte count, rejecting growth before extra work."""
    digest = hashlib.sha256()
    size = 0
    with Path(path).open('rb') as stream:
        limit = os.fstat(stream.fileno()).st_size
        if expected_size is not None and limit != expected_size:
            raise ValueError(f'File size changed before content verification: {path}')
        while True:
            checkpoint()
            chunk = stream.read(min(1024*1024, limit-size+1))
            if not chunk:
                break
            if size+len(chunk)>limit:
                raise ValueError(f'File grew during content verification: {path}')
            size += len(chunk)
            digest.update(chunk)
        if size!=limit:
            raise ValueError(f'File shrank during content verification: {path}')
    return digest.hexdigest(), size


def file_state(path, *, checkpoint=lambda: None):
    """Observe a regular file's identity and bytes, without following a link."""
    path = Path(path)
    if not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError(f'Expected a regular file: {path}')
    before = fingerprint(path)
    digest, size = digest_file(path, checkpoint=checkpoint)
    if path.is_symlink() or fingerprint(path) != before or size != before[2]:
        raise ValueError(f'File changed during content verification: {path}')
    return before, digest, size


def content_state(path, *, directory=False, checkpoint=lambda: None):
    """Bind identities, bytes and the complete membership of an owned tree.

    Rename may change ctime, so content records retain device/inode identities
    rather than timestamps. Callers separately check stamps where appropriate.
    This cooperative observation is not an atomic filesystem snapshot.
    """
    path = Path(path)
    if not directory:
        stamp, digest, size = file_state(path, checkpoint=checkpoint)
        return (('', 'file', stamp[:2], digest, size),)
    records = []
    pending = [path]
    while pending:
        current = pending.pop()
        checkpoint()
        if not stat.S_ISDIR(current.lstat().st_mode):
            raise ValueError(f'Expected an owned directory: {current}')
        before = fingerprint(current)
        relative = current.relative_to(path).as_posix() if current != path else ''
        records.append((relative, 'directory', before[:2]))
        for item in current.iterdir():
            checkpoint()
            mode = item.lstat().st_mode
            if stat.S_ISDIR(mode):
                pending.append(item)
            elif stat.S_ISREG(mode):
                stamp, digest, size = file_state(item, checkpoint=checkpoint)
                records.append((item.relative_to(path).as_posix(), 'file', stamp[:2], digest, size))
            else:
                raise ValueError(f'Owned output contains a link or non-file: {item}')
        if fingerprint(current) != before:
            raise ValueError(f'Owned directory changed during verification: {current}')
    return tuple(sorted(records))


def copy_input(source, target, *, checkpoint=lambda: None, on_create=None, expected_size=None):
    """Capture bytes and verify their identity before inspecting the copy.

    A same-size write can share both timestamps with the original on a coarse
    filesystem clock. Recheck content as well as metadata so such a write cannot
    silently produce a mixture of old and new chunks. This is a cooperative
    consistency check, not an atomic filesystem snapshot against hostile writes.
    """
    source, target = Path(source), Path(target)
    before = fingerprint(source)
    if expected_size is not None and before[2] != expected_size:
        raise ValueError(f'Input size changed before capture: {source}')
    digest = hashlib.sha256()
    size = 0
    target.parent.mkdir(parents=True, exist_ok=True)
    with source.open('rb') as reader, target.open('xb') as writer:
        if on_create:
            value = os.fstat(writer.fileno())
            on_create((value.st_dev, value.st_ino))
        while True:
            checkpoint()
            chunk = reader.read(min(1024*1024, before[2]-size+1))
            if not chunk:
                break
            if size+len(chunk)>before[2]:
                raise ValueError(f'Input grew while being captured: {source}')
            if writer.write(chunk)!=len(chunk):
                raise OSError(f'Short write while capturing input: {target}')
            digest.update(chunk)
            size += len(chunk)
        if size!=before[2]:
            raise ValueError(f'Input shrank while being captured: {source}')
        writer.flush()
        os.fsync(writer.fileno())
    if fingerprint(source) != before:
        raise ValueError(f'Input changed while being captured: {source}')
    captured = digest.hexdigest(), size
    observed = digest_file(source, checkpoint=checkpoint, expected_size=before[2])
    if observed != captured or fingerprint(source) != before:
        raise ValueError(f'Input changed while being captured: {source}')
    return captured


def copy_tree(source, target, *, checkpoint=lambda: None, expected_files=None, expected_tree=None):
    raw_source = Path(source)
    if expected_tree is not None:
        from ._directory_tree import DirectoryManifest, DirectoryLimits, verify_tree
        if type(expected_tree) is not DirectoryManifest:
            raise TypeError('Directory publication requires DirectoryManifest evidence')
        limits = DirectoryLimits(total_bytes=max(1, expected_tree.total_bytes), entries=max(1, len(expected_tree.entries)),
            depth=max((len(entry.path.split('/')) for entry in expected_tree.entries), default=1))
        verify_tree(raw_source, expected_tree, limits=limits, checkpoint=checkpoint)
    tree_members = ({entry.path:entry for entry in expected_tree.entries}
                  if expected_tree is not None else {})
    source, target = raw_source.resolve(), Path(target)
    if expected_tree is not None and expected_files is not None:
        declared_files={(source/e.path).resolve():(e.sha256,e.size) for e in expected_tree.entries if e.kind=='file'}
        if declared_files!={p:v for p,v in expected_files.items() if p.is_relative_to(source)}:
            raise ValueError('Run asset evidence changed before publication')
    if expected_tree is not None and expected_tree.has_hardlinks:
        from ._directory_tree import populate_tree
        populate_tree(raw_source,target,expected_tree,limits=limits,checkpoint=checkpoint)
        if expected_files is not None:
            actual = {(source/e.path).resolve():(e.sha256,e.size) for e in expected_tree.entries if e.kind == 'file'}
            if actual != {p:v for p,v in expected_files.items() if p.is_relative_to(source)}:
                raise ValueError('Run asset files changed before publication')
        return content_state(target,directory=True,checkpoint=checkpoint)
    pending = [(source, target)]
    copied=set()
    records = []
    while pending:
        current, destination = pending.pop()
        checkpoint()
        relative = destination.relative_to(target).as_posix() if destination != target else ''
        records.append((relative, 'directory', fingerprint(destination)[:2]))
        for item in current.iterdir():
            checkpoint()
            if item.is_symlink() or not item.resolve().is_relative_to(source):
                raise ValueError(f'Run asset contains a link outside its owned tree: {item}')
            declared=tree_members.get(item.relative_to(source).as_posix())
            if expected_tree is not None and (declared is None or
                    (declared.kind=='directory')!=item.is_dir()):
                raise ValueError(f'Run asset membership changed before publication: {item}')
            other = destination/item.name
            if item.is_dir():
                other.mkdir()
                pending.append((item, other))
            elif item.is_file():
                identities = []
                expected_size=declared.size if declared is not None else None
                if expected_files is not None:
                    if item.resolve() not in expected_files:
                        raise ValueError(f'Run asset changed before publication: {item}')
                    expected_size=expected_files[item.resolve()][1]
                if expected_size is not None and fingerprint(item)[2]!=expected_size:
                    raise ValueError(f'Run asset changed before publication: {item}')
                captured=copy_input(item, other, checkpoint=checkpoint, on_create=identities.append,expected_size=expected_size)
                records.append((other.relative_to(target).as_posix(), 'file', identities[0], *captured))
                copied.add(item.resolve())
                if expected_files is not None and expected_files.get(item.resolve())!=captured:
                    raise ValueError(f'Run asset changed before publication: {item}')
            else:
                raise ValueError(f'Run asset is not a regular file or directory: {item}')
    if expected_files is not None and copied!={path for path in expected_files if path.is_relative_to(source)}:
        raise ValueError('Run asset files disappeared before publication')
    if expected_tree is not None:
        verify_tree(raw_source, expected_tree, limits=limits, checkpoint=checkpoint)
        verify_tree(target, expected_tree, limits=limits, checkpoint=checkpoint)
    return tuple(sorted(records))


def remove_owned_tree(path, *, parent, identity):
    """Never recursively remove a substituted root or escape its known parent."""
    path, parent = Path(path), Path(parent)
    if not path.exists():
        return
    if path.is_symlink() or path.parent.resolve() != parent.resolve() or path.resolve() != parent.resolve()/path.name:
        raise RuntimeError(f'Owned directory location changed: {path}')
    if fingerprint(path)[:2] != identity:
        raise RuntimeError(f'Owned directory identity changed: {path}')
    shutil.rmtree(path)


class Workspace:
    def __init__(self, base, run_id, *, nested=False):
        self.root = Path(tempfile.mkdtemp(prefix='.easysewer-'+run_id+'-', dir=base)).resolve()
        self.path = self.root/'execution' if nested else self.root
        self.identity = fingerprint(self.root)[:2]
        self.retained = False

    def close(self):
        if not self.retained:
            remove_owned_tree(self.root, parent=self.root.parent, identity=self.identity)


@dataclass
class _Target:
    path: Path
    expected: tuple | None
    directory: bool
    lock: Path
    lock_identity: tuple | None
    temporary: Path | None = None
    temporary_identity: tuple | None = None
    backup: Path | None = None
    published: tuple | None = None
    expected_content: tuple | None = None
    prepared_content: tuple | None = None
    reservation_source: Path | None = None
    lock_content: tuple | None = None
    directory_cleanup: dict = field(default_factory=dict)


class OutputTransaction:
    """Coordinate Runner instances and check identities and content before writes.

    This is not a security boundary against a hostile process sharing the user's
    permissions. Explicit overwrite replaces a declared output tree only after
    complete original evidence is captured; it never merges directory members.
    """
    def __init__(self, run_id, *, overwrite, protected=(), protected_directories=(), directory_limits=None):
        self.run_id = run_id
        self.overwrite = overwrite
        from ._directory_tree import DirectoryLimits
        if directory_limits is not None and type(directory_limits) is not DirectoryLimits:
            raise TypeError('Directory alias protection requires DirectoryLimits')
        self.directory_limits = directory_limits or DirectoryLimits()
        explicit_directories = tuple(Path(path).resolve() for path in protected_directories)
        self.protected = tuple(dict.fromkeys((*[Path(path).resolve() for path in protected], *explicit_directories)))
        self.protected_directories = frozenset((*explicit_directories, *(p for p in self.protected if p.is_dir())))
        self.targets = []
        self.created_directories = []
        self.issues = []
        self.committed = False
        self.journal = None

    def track_workspace(self, workspace, *, cleanup_on_crash=False):
        if self.journal is not None:
            self.journal.workspace(workspace, self, cleanup_on_crash=cleanup_on_crash)

    def workspace_execution(self, workspace, *, state, identity=None):
        if self.journal is not None:
            self.journal.workspace_execution(workspace, self, state=state, identity=identity)

    def prepare_execution(self, workspace):
        if self.journal is not None:
            return self.journal.prepare_execution(workspace, self)

    def revoke_execution(self, workspace):
        return self.journal is not None and self.journal.revoke_execution(workspace, self)

    def workspace_cleanup(self, workspace, *, requested=False, completed=False):
        if self.journal is not None:
            self.journal.workspace_cleanup(workspace, self, requested=requested, completed=completed)

    def ensure_directory(self, path):
        path = Path(path)
        missing = []
        while not path.exists():
            missing.append(path)
            path = path.parent
        if not path.is_dir():
            raise NotADirectoryError(path)
        for item in reversed(missing):
            try:
                item.mkdir()
            except FileExistsError:
                if not item.is_dir():
                    raise
            else:
                self.created_directories.append((item, fingerprint(item)[:2]))

    def _verify_protection(self, path, directory, checkpoint):
        candidate_identities = None
        for protected in self.protected:
            checkpoint()
            protected_directory = protected in self.protected_directories or protected.is_dir()
            if (path == protected or protected_directory and path.is_relative_to(protected) or
                    directory and protected.is_relative_to(path)):
                raise ValueError(f'Output aliases an input resource: {path}')
            if not path.exists() or not protected.exists():
                continue
            if path.samefile(protected):
                raise ValueError(f'Output aliases an input resource: {path}')
            if not (protected_directory or directory):
                continue
            from ._directory_tree import tree_file_identities
            if candidate_identities is None:
                candidate_identities = (tree_file_identities(path, limits=self.directory_limits, checkpoint=checkpoint)
                    if directory and path.is_dir() else frozenset((fingerprint(path)[:2],)))
            identities = (tree_file_identities(protected, limits=self.directory_limits, checkpoint=checkpoint)
                if protected_directory and protected.is_dir() else frozenset((fingerprint(protected)[:2],)))
            if candidate_identities & identities:
                raise ValueError(f'Output aliases an input resource member: {path}')

    def reserve(self, destinations, *, checkpoint=lambda: None):
        """Reserve every (path, is_directory) before native code can run."""
        for raw, directory in sorted(destinations, key=lambda pair: os.path.normcase(str(pair[0]))):
            checkpoint()
            raw = Path(raw)
            if not raw.is_absolute():
                raise ValueError('Output reservation requires absolute paths')
            if raw.exists() or raw.is_symlink():
                from ._directory_tree import _node
                _node(raw,'directory' if directory else 'file')
            path = raw.resolve()
            self._verify_protection(path, directory, checkpoint)
            for other in self.targets:
                if path == other.path or path.is_relative_to(other.path) or other.path.is_relative_to(path) or (
                    path.exists() and other.path.exists() and path.samefile(other.path)):
                    raise ValueError(f'Output destinations overlap: {path}')
            if path.exists():
                if not (path.is_dir() if directory else path.is_file()):
                    raise ValueError(f'Output kind differs from its declaration: {path}')
                if not self.overwrite:
                    raise FileExistsError(path)
            self.ensure_directory(path.parent)
            key = hashlib.sha256(os.path.normcase(str(path)).encode('utf-8')).hexdigest()[:32]
            lock = path.parent/('.easysewer-lock-'+key)
            if self.journal is None:
                self.journal = _Journal(path.parent, self)
            payload = json.dumps(dict(run_id=self.run_id, pid=os.getpid(), target=str(path),
                journal=str(self.journal.path), token=self.journal.data['token'])).encode('utf-8')
            if len(payload) > _RESERVATION_BYTES:
                raise ValueError('Reservation record exceeds size limit')
            seed = path.parent/('.easysewer-reserve-'+uuid.uuid4().hex)
            record = _Target(path=path, expected=None, directory=directory,
                lock=lock, lock_identity=None, reservation_source=seed,
                lock_content=(hashlib.sha256(payload).hexdigest(), len(payload)))
            self.targets.append(record)
            self.journal.sync(self)
            # Publish a complete reservation exclusively, after its identity
            # and bytes are durable in the journal. A crash while writing the
            # private source can no longer strand an empty destination lock.
            with seed.open('xb') as stream:
                value = os.fstat(stream.fileno())
                record.lock_identity = (value.st_dev, value.st_ino)
                stream.write(payload)
                stream.flush(); os.fsync(stream.fileno())
            self.journal.sync(self)
            self._verify_reservation(record, seed)
            try:
                os.link(seed, lock)
            except FileExistsError:
                try:
                    self._clean_reservation_source(record)
                except (OSError, ValueError, RuntimeError) as error:
                    self.issues.append(str(error))
                else:
                    self.targets.remove(record)
                self.journal.sync(self)
                raise FileExistsError(f'Output is reserved by another run; inspect {lock}') from None
            self._verify_reservation(record)
            self._clean_reservation_source(record)
            self.journal.sync(self)
            if path.exists():
                if not (path.is_dir() if directory else path.is_file()) or path.is_symlink() or not self.overwrite:
                    raise FileExistsError(f'Output appeared while being reserved: {path}')
                if directory:
                    from ._publication_directory import state
                    stamp=fingerprint(path)
                    evidence=state(path,limits=self.directory_limits,checkpoint=checkpoint)
                    if fingerprint(path)!=stamp:raise ValueError('Output directory changed during reservation')
                    record.expected,record.expected_content=stamp,evidence
                else:
                    stamp, digest, size = file_state(path, checkpoint=checkpoint)
                    record.expected = stamp
                    record.expected_content = (('', 'file', stamp[:2], digest, size),)
            self.journal.sync(self)

    def _verify(self, target, *, checkpoint=lambda: None):
        if target.path.is_symlink() or target.path.resolve() != target.path:
            raise ValueError(f'Output location changed: {target.path}')
        current = fingerprint(target.path) if target.path.exists() else None
        if current != target.expected:
            raise ValueError(f'Output changed after reservation: {target.path}')
        if target.expected is not None:
            from ._publication_directory import state
            observed=(state(target.path,limits=self.directory_limits,checkpoint=checkpoint) if target.directory
                else content_state(target.path,checkpoint=checkpoint))
            if observed!=target.expected_content:
                raise ValueError(f'Output content changed after reservation: {target.path}')
        self._verify_reservation(target)
        self._verify_protection(target.path, target.directory, checkpoint)

    def _verify_reservation(self, target, path=None):
        path = target.lock if path is None else path
        stamp, digest, size, _ = _reservation_state(path)
        if stamp[:2] != target.lock_identity or (digest, size) != target.lock_content:
            raise ValueError(f'Output reservation was removed or replaced: {path}')

    def _clean_reservation_source(self, target):
        path = target.reservation_source
        if path is not None and (path.exists() or path.is_symlink()):
            self._verify_reservation(target, path)
            path.unlink()

    def publish(self, sources, *, checkpoint=lambda: None, expected_sources=None, expected_trees=None):
        """Copy to target volumes first, then replace; roll back partial commits."""
        sources = {Path(path).resolve(): Path(source) for path, source in sources.items()}
        expected_sources = ({Path(path).resolve(): value for path,value in expected_sources.items()}
                            if expected_sources is not None else None)
        known = {target.path for target in self.targets}
        if sources.keys()-known:
            raise ValueError('An unreserved output cannot be published')
        active = [target for target in self.targets if target.path in sources]
        if expected_trees is not None:
            from ._directory_tree import DirectoryManifest
            entries = tuple(expected_trees.items())
            if any(type(tree) is not DirectoryManifest for _, tree in entries):
                raise TypeError('Directory publication requires DirectoryManifest evidence')
            expected_trees = {Path(path).absolute(): tree for path, tree in entries}
            if len(expected_trees) != len(entries):
                raise ValueError('Directory publication evidence aliases a source path')
            required = {sources[target.path].absolute() for target in active if target.directory}
            if set(expected_trees) != required:
                raise ValueError('Directory publication evidence must cover exactly its source trees')
        try:
            for target in active:
                checkpoint()
                self._verify(target, checkpoint=checkpoint)
                if target.temporary is not None and (target.temporary.exists() or target.temporary.is_symlink()):
                    raise ValueError('Previous publication temporary is still retained')
                target.directory_cleanup.pop('temporary',None)
                temporary = target.path.parent/('.easysewer-publish-'+uuid.uuid4().hex)
                target.temporary = temporary
                self.journal.sync(self, phase='preparing_publication')
                if target.directory:
                    temporary.mkdir()
                    target.temporary_identity = fingerprint(temporary)[:2]
                    self.journal.sync(self)
                    if fingerprint(temporary)[:2] != target.temporary_identity:
                        raise ValueError('Publication directory was replaced before copying')
                    options = {} if expected_trees is None else {'expected_tree': expected_trees[sources[target.path].absolute()]}
                    target.prepared_content = copy_tree(sources[target.path], temporary, checkpoint=checkpoint,
                        expected_files=expected_sources, **options)
                    if fingerprint(temporary)[:2] != target.temporary_identity:
                        raise ValueError('Publication directory was replaced while copying')
                else:
                    if expected_sources is not None and sources[target.path].resolve() not in expected_sources:
                        raise ValueError(f'Run output changed before publication: {sources[target.path]}')
                    expected_size=(expected_sources.get(sources[target.path].resolve(),(None,None))[1]
                                   if expected_sources is not None else None)
                    if expected_size is not None and fingerprint(sources[target.path])[2]!=expected_size:
                        raise ValueError(f'Run output changed before publication: {sources[target.path]}')
                    captured=copy_input(sources[target.path], temporary, checkpoint=checkpoint,expected_size=expected_size,
                        on_create=lambda identity, target=target: setattr(target, 'temporary_identity', identity))
                    target.prepared_content = (('', 'file', target.temporary_identity, *captured),)
                    if expected_sources is not None and expected_sources.get(sources[target.path].resolve())!=captured:
                        raise ValueError(f'Run output changed before publication: {sources[target.path]}')
                self.journal.sync(self)
            for target in active:
                if target.expected is not None:
                    target.backup = target.path.parent/('.easysewer-backup-'+uuid.uuid4().hex)
            self.journal.sync(self, phase='publishing')
            for target in active:
                checkpoint()
                if content_state(target.temporary, directory=target.directory, checkpoint=checkpoint) != target.prepared_content:
                    raise ValueError(f'Prepared output changed before publication: {target.path}')
                # A large prepared copy can take time to verify. Observe the
                # requested destination last, immediately before replacing it.
                self._verify(target, checkpoint=checkpoint)
                if target.expected is not None:
                    if target.backup.exists() or target.backup.is_symlink():
                        raise FileExistsError(f'Backup destination appeared after preparation: {target.backup}')
                    os.replace(target.path, target.backup)
                os.replace(target.temporary, target.path)
                target.published = fingerprint(target.path)
            for target in active:
                checkpoint()
                self._verify_published(target, checkpoint=checkpoint)
            self.committed = True
            self.journal.sync(self, phase='committed')
        except BaseException:
            self.committed = False
            self._rollback(active)
            raise
        finally:
            self._clean_temporaries()
        for target in active:
            if target.backup:
                try:
                    if target.directory:self._clean_directory(target,'backup',expected=target.expected_content)
                    else:
                        self._verify_backup(target)
                        target.backup.unlink()
                    target.backup = None
                    target.directory_cleanup.pop('backup',None)
                except (OSError, RuntimeError, ValueError) as error:
                    self.issues.append(f'Cannot remove owned backup {target.backup}: {error}')

    def _verify_backup(self, target):
        from ._publication_directory import state
        actual=state(target.backup,limits=self.directory_limits) if target.directory else content_state(target.backup)
        if actual != target.expected_content:
            raise RuntimeError(f'Output backup changed; retained: {target.backup}')

    def _verify_published(self, target, *, checkpoint=lambda: None):
        if (target.path.is_symlink() or not target.path.exists()
                or fingerprint(target.path) != target.published
                or content_state(target.path, directory=target.directory, checkpoint=checkpoint) != target.prepared_content):
            raise RuntimeError(f'Published output changed; backup retained: {target.path}')

    def _rollback(self, targets):
        for target in reversed(targets):
            try:
                if target.backup and (target.published is not None or target.backup.exists()):
                    self._verify_backup(target)
                if target.published is not None:
                    self._verify_published(target)
                    if target.directory:
                        if target.temporary.exists() or target.temporary.is_symlink():
                            raise RuntimeError('Cannot move published directory over a retained temporary')
                        os.replace(target.path,target.temporary)
                    else:
                        target.path.unlink()
                    target.published = None
                if target.backup and target.backup.exists():
                    if target.path.exists() or target.path.is_symlink():
                        raise RuntimeError(f'Cannot restore backup over an external file: {target.path}')
                    os.replace(target.backup, target.path)
                    target.backup = None
            except (OSError, RuntimeError, ValueError) as error:
                self.issues.append(str(error))

    def _clean_temporaries(self):
        for target in self.targets:
            if target.temporary is None or not target.temporary.exists():
                continue
            try:
                if target.directory:
                    self._clean_directory(target,'temporary',expected=target.prepared_content)
                elif target.temporary_identity is not None and fingerprint(target.temporary)[:2] == target.temporary_identity:
                    target.temporary.unlink()
                else:
                    raise RuntimeError(f'Publication temporary was replaced: {target.temporary}')
            except (OSError, RuntimeError, ValueError) as error:
                self.issues.append(str(error))

    def _clean_directory(self,target,slot,*,expected):
        from ._publication_directory import state,remove
        path=getattr(target,slot)
        evidence=target.directory_cleanup.get(slot)
        if evidence is None:
            identity=target.expected[:2] if slot=='backup' else target.temporary_identity
            if fingerprint(path)[:2]!=identity:raise ValueError('Directory cleanup root was replaced')
            evidence=state(path,limits=self.directory_limits)
            if expected is not None and evidence!=expected:raise ValueError('Directory content changed before cleanup')
            target.directory_cleanup[slot]=evidence
            try:self.journal.sync(self)
            except BaseException:
                # A failed flush is not a cleanup permit, even on a later retry.
                target.directory_cleanup.pop(slot,None)
                raise
        remove(path,evidence)

    def close(self):
        self._clean_temporaries()
        for target in self.targets:
            try:
                self._clean_reservation_source(target)
            except (OSError, ValueError, RuntimeError) as error:
                self.issues.append(str(error))
        if self.journal is not None:
            try:
                if not self.issues:
                    self.journal.data['recovered'] = [str(t.path) for t in self.targets]
                self.journal.sync(self, phase='committed' if self.committed else 'closed')
            except Exception as error:
                self.issues.append(f'Cannot update recovery journal {self.journal.path}: {error}')
        # A conflict retains its reservation and recovery record. Releasing it
        # would permit a later run to overwrite evidence needed for recovery.
        for target in reversed(self.targets):
            if self.issues:
                break
            try:
                if target.lock.exists() or target.lock.is_symlink():
                    self._verify_reservation(target)
                    target.lock.unlink()
            except (OSError, ValueError, RuntimeError) as error:
                self.issues.append(str(error))
        if self.journal is not None:
            try:
                if self.issues:
                    self.journal.sync(self)
                self.journal.close(remove=not self.issues)
            except Exception as error:
                self.issues.append(f'Cannot close recovery journal {self.journal.path}: {error}')
                self.journal.lease.close()
        for path, identity in reversed(self.created_directories):
            try:
                if path.exists() and fingerprint(path)[:2] == identity:
                    path.rmdir()  # Only an empty directory created by this run.
            except OSError:
                pass
