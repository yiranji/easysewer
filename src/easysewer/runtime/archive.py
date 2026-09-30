"""Self-contained, movable result directories with verified, bounded file IO.

This restores observed results, never native state or executable code. Original
source/destination paths remain provenance; only FileArtifact paths are rebound.
"""

from dataclasses import fields
import hashlib
import os
from pathlib import Path
import stat

from .results import RunResult
from ._result_codec import Codec, exact
from ._workspace import fingerprint, remove_owned_tree
from ..io.json import JsonDocument
from ..io.output_metadata import OutputMetadata
from ..results.series import digest


def _limit(value):
    if type(value) is not int or value <= 0:
        raise ValueError('Archive byte limits must be positive integers')


def _regular(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or path.is_symlink():
        raise ValueError('Archive content must be a regular file: '+str(path))
    return info.st_size


def _stream(path, expected_size, *, writer=None):
    if _regular(path) != expected_size:
        raise ValueError('Archive/source file size changed: '+str(path))
    before = fingerprint(path)
    hashed = hashlib.sha256(); count = 0
    with path.open('rb') as stream:
        while True:
            chunk = stream.read(min(1024*1024, expected_size-count+1))
            if not chunk:
                break
            count += len(chunk)
            if count > expected_size:
                raise ValueError('Archive/source grew while reading')
            hashed.update(chunk)
            if writer is not None:
                writer.write(chunk)
    if count != expected_size or fingerprint(path) != before:
        raise ValueError('Archive/source changed while reading')
    return hashed.hexdigest()


class _Blobs:
    def __init__(self, root, max_bytes, *, inventory=None, metadata_limit=64*1024**2):
        self.root = root
        self.max_bytes = max_bytes
        self.inventory = {} if inventory is None else inventory
        self.used = set()
        self.metadata_limit = metadata_limit
        self.loaded = {}
        self.loaded_size = 0

    def _reserve(self, sha, size):
        digest(sha)
        if sha is None or type(size) is not int or size < 0:
            raise ValueError('Invalid archive blob identity')
        if sha in self.inventory and self.inventory[sha] != size:
            raise ValueError('Inconsistent blob size')
        if sha not in self.inventory:
            if sum(self.inventory.values())+size > self.max_bytes:
                raise ValueError('Archive exceeds max_bytes')
            self.inventory[sha] = size
        return self.root/sha

    def put_bytes(self, raw):
        sha = hashlib.sha256(raw).hexdigest()
        path = self._reserve(sha, len(raw))
        if not path.exists():
            with path.open('xb') as stream:
                stream.write(raw); stream.flush(); os.fsync(stream.fileno())
        return dict(sha256=sha, size=len(raw))

    def put_artifact(self, artifact):
        path = self._reserve(artifact.sha256, artifact.size)
        source = Path(artifact.path)
        if path.exists():
            observed = _stream(source, artifact.size)
        else:
            with path.open('xb') as stream:
                observed = _stream(source, artifact.size, writer=stream)
                stream.flush(); os.fsync(stream.fileno())
        if observed != artifact.sha256:
            raise ValueError('Artifact bytes changed since this run: '+artifact.path)

    def get_path(self, sha, size):
        digest(sha)
        if sha is None or type(size) is not int or self.inventory.get(sha) != size:
            raise ValueError('Unlisted or mismatched archive blob')
        self.used.add(sha)
        return self.root/sha

    def get_bytes(self, sha, size):
        path = self.get_path(sha, size)
        if sha in self.loaded:
            return self.loaded[sha]
        if self.loaded_size+size > self.metadata_limit:
            raise ValueError('In-memory result evidence exceeds max_manifest_bytes')
        if _regular(path) != size:
            raise ValueError('Archive blob size changed')
        with path.open('rb') as stream:
            raw = stream.read(size+1)
        if len(raw) != size or hashlib.sha256(raw).hexdigest() != sha:
            raise ValueError('Archive blob changed')
        self.loaded[sha] = raw
        self.loaded_size += size
        return raw


def _verify_output(result):
    """Check persisted OUT offsets/layout against bytes, not the JSON claim."""
    if result.output_metadata is None:
        return
    saved = result.output_metadata
    observed = OutputMetadata.read(result.output.path, encoding=saved.identifier_encoding)
    contextual = {'semantics', 'averages', 'producer', 'result_context'}
    if any(getattr(observed, f.name) != getattr(saved, f.name)
           for f in fields(OutputMetadata) if f.name not in contextual):
        raise ValueError('Archived OUT metadata differs from its bytes')
    if (saved.flow_units != result.snapshot.units.flow_units or
        saved.engine_version != result.backend.engine_version or
        saved.producer != result.backend.key or saved.semantics != result.backend.output_semantics):
        raise ValueError('Archived OUT metadata differs from execution evidence')


def save_result(result, directory, *, max_bytes=8*1024**3, max_manifest_bytes=64*1024**2):
    """Create a new archive. Existing paths are never replaced or merged.

    The manifest is written last. A process crash can leave an incomplete new
    directory; load rejects it. Ordinary exceptions remove only this owned tree.
    """
    if type(result) is not RunResult:
        raise TypeError('Expected a RunResult')
    _limit(max_bytes); _limit(max_manifest_bytes)
    target = Path(directory).absolute()
    target.mkdir()  # exclusive creation, including refusal of symlinks
    identity = fingerprint(target)[:2]
    try:
        root = target/'blobs'; root.mkdir()
        blobs = _Blobs(root, max_bytes)
        version = '1.4' if result.directory_artifacts or (result.snapshot is not None and any(r.tree is not None for r in result.snapshot.resources)) else '1.3'
        if result.snapshot is not None and any(r.initial_relative_path is not None for r in result.snapshot.resources):version = '1.5'
        if any(a.manifest is None for a in result.directory_artifacts) or (result.snapshot is not None and any(
                r.initial_relative_path is not None and r.tree is None for r in result.snapshot.resources)):version = '1.6'
        if any(a.manifest is not None and a.manifest.has_hardlinks for a in result.directory_artifacts) or (
                result.snapshot is not None and any(r.tree is not None and r.tree.has_hardlinks for r in result.snapshot.resources)):version = '1.7'
        if result.directory_group_artifacts or (result.snapshot is not None and any(r.directory_group is not None for r in result.snapshot.resources)):version='1.8'
        if result.snapshot is not None and any(r.directory_group is not None and r.kind=='file' for r in result.snapshot.resources):version='1.9'
        data = Codec(blobs, result_version=version).encode(result)
        manifest = JsonDocument.from_data(dict(kind='easysewer:run-result', schema_version=version,
            result=data, blobs=[dict(sha256=sha, size=size) for sha, size in sorted(blobs.inventory.items())])).to_bytes()
        if len(manifest) > max_manifest_bytes:
            raise ValueError('Result metadata exceeds max_manifest_bytes')
        with (target/'result.json').open('xb') as stream:
            stream.write(manifest); stream.flush(); os.fsync(stream.fileno())
        # Reject inconsistent directly constructed result evidence before the
        # caller can consider the save successful. Verification uses the copies.
        load_result(target, max_bytes=max_bytes, max_manifest_bytes=max_manifest_bytes)
    except BaseException:
        remove_owned_tree(target, parent=target.parent, identity=identity)
        raise
    return target


def load_result(directory, *, max_bytes=8*1024**3, max_manifest_bytes=64*1024**2):
    """Load and verify every listed file without consulting original paths."""
    _limit(max_bytes); _limit(max_manifest_bytes)
    target = Path(directory).absolute()
    if target.is_symlink() or not target.is_dir():
        raise ValueError('Expected an archive directory')
    root = target/'blobs'
    if root.is_symlink() or not root.is_dir():
        raise ValueError('Expected an owned blob directory')
    manifest = target/'result.json'
    if _regular(manifest) > max_manifest_bytes:
        raise ValueError('Result metadata exceeds max_manifest_bytes')
    with manifest.open('rb') as stream:
        raw = stream.read(max_manifest_bytes+1)
    if len(raw) > max_manifest_bytes:
        raise ValueError('Result metadata exceeds max_manifest_bytes')
    envelope = JsonDocument.from_bytes(raw).data
    exact(envelope, ('kind', 'schema_version', 'result', 'blobs'))
    if envelope['kind'] != 'easysewer:run-result' or envelope['schema_version'] not in ('1.0','1.1','1.2','1.3','1.4','1.5','1.6','1.7','1.8','1.9'):
        raise ValueError('Unknown run result archive contract')
    if type(envelope['blobs']) is not list:
        raise TypeError('Expected an archive inventory')
    inventory = {}; total = 0
    for entry in envelope['blobs']:
        exact(entry, ('sha256', 'size'))
        sha, size = entry['sha256'], entry['size']
        digest(sha)
        if sha is None or sha in inventory or type(size) is not int or size < 0:
            raise ValueError('Invalid or duplicate archive blob')
        total += size
        if total > max_bytes:
            raise ValueError('Archive exceeds max_bytes')
        inventory[sha] = size
    if {p.name for p in root.iterdir()} != set(inventory):
        raise ValueError('Archive blob inventory differs from directory contents')
    for sha, size in inventory.items():
        if _stream(root/sha, size) != sha:
            raise ValueError('Archive blob hash mismatch: '+sha)
    blobs = _Blobs(root, max_bytes, inventory=inventory, metadata_limit=max_manifest_bytes)
    result = Codec(blobs,result_version=envelope['schema_version']).decode(envelope['result'])
    if type(result) is not RunResult or blobs.used != set(inventory):
        raise ValueError('Invalid result or unused archive content')
    _verify_output(result)
    return result
