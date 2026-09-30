"""Private, versioned checkpoint directory; loading never executes native code.

The commit file is written last. Existing directories are never merged/replaced.
This layer binds and verifies bytes; the native coordinator must still validate
every owner against a reconstructed solver before committing a restoration.
"""
from dataclasses import dataclass, replace
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import stat
import struct
import sys
import uuid

from ._result_codec import Codec, exact
from ._workspace import fingerprint, remove_owned_tree
from .results import RunSnapshot
from ..io.json import JsonDocument
from ..validation._cooperative import checkpoint_scope, checkpointed

KIND = 'easysewer:checkpoint'
VERSION = '1.0'
DIRECTORY_VERSION = '1.1'
MUTABLE_DIRECTORY_VERSION = '1.2'
ABSENT_DIRECTORY_VERSION = '1.3'
LINKED_DIRECTORY_VERSION = '1.4'
GROUP_DIRECTORY_VERSION = '1.5'
MIXED_RESOURCE_VERSION = '1.6'
SHA = re.compile(r'[0-9a-f]{64}\Z')
INPUT_KEY = re.compile(r'swmm:(?:input:(?:climate|rain|runoff|rdii|routing)|table:[012]:(?:0|[1-9][0-9]*))\Z')


@dataclass(frozen=True)
class Limits:
    total_bytes: int = 8 * 1024**3
    manifest_bytes: int = 64 * 1024**2
    native_bytes: int = 512 * 1024**2
    worker_bytes: int = 16 * 1024**2

    def __post_init__(self):
        if any(type(v) is not int or v <= 0 for v in
               (self.total_bytes, self.manifest_bytes, self.native_bytes, self.worker_bytes)):
            raise ValueError('Checkpoint limits must be positive integers')


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False,
                      separators=(',', ':')).encode('ascii')


def descriptor(value):
    exact(value, ('sha256', 'size'))
    if (type(value['sha256']) is not str or not SHA.fullmatch(value['sha256']) or
            type(value['size']) is not int or value['size'] < 0):
        raise ValueError('Invalid checkpoint blob descriptor')
    return value['sha256'], value['size']


def _regular(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError('Checkpoint requires regular files without symbolic links')
    return info


def _stream(path, size, *, write=None, checkpoint=lambda: None):
    before = _regular(path)
    if before.st_size != size:
        raise ValueError('Checkpoint source size changed')
    stamp = fingerprint(path)
    digest = hashlib.sha256()
    count = 0
    with path.open('rb') as stream:
        opened = os.fstat(stream.fileno())
        if (opened.st_dev, opened.st_ino) != stamp[:2]:
            raise ValueError('Checkpoint source identity changed')
        while True:
            checkpoint()
            chunk = stream.read(min(1024**2, size-count+1))
            if not chunk:
                break
            count += len(chunk)
            if count > size:
                raise ValueError('Checkpoint source grew while copying')
            digest.update(chunk)
            if write is not None:
                if write.write(chunk) != len(chunk):
                    raise OSError('Short checkpoint write')
    if count != size or fingerprint(path) != stamp:
        raise ValueError('Checkpoint source changed while copying')
    return digest.hexdigest()


def _relative(value):
    if type(value) is not str or not value or '\\' in value or ':' in value or '\0' in value:
        raise ValueError('Checkpoint resource needs a portable relative path')
    parts = value.split('/')
    if any(p in ('', '.', '..') for p in parts) or PurePosixPath(value).is_absolute():
        raise ValueError('Checkpoint resource path escapes its workspace')
    devices = {'con','prn','aux','nul',*(f'com{i}' for i in range(1,10)),*(f'lpt{i}' for i in range(1,10))}
    if any(p[-1] in '. ' or any(ch in p for ch in '<>"|?*') or
           any(ord(ch)<32 for ch in p) or p.split('.')[0].casefold() in devices for p in parts):
        raise ValueError('Checkpoint resource path is not a portable regular filename')
    return Path(*parts)


def snapshot_codec_version(snapshot):
    if any(r.directory_group is not None and r.kind=='file' for r in checkpointed(snapshot.resources)):return '1.9'
    if any(r.directory_group is not None for r in checkpointed(snapshot.resources)):return '1.8'
    if any(r.tree is not None and r.tree.has_hardlinks for r in checkpointed(snapshot.resources)):
        return '1.7'
    if any(r.initial_relative_path is not None and r.tree is None for r in checkpointed(snapshot.resources)):
        return '1.6'
    if any(item.initial_relative_path is not None for item in checkpointed(snapshot.resources)):
        return '1.5'
    return '1.4' if any(item.tree is not None for item in checkpointed(snapshot.resources)) else '1.1'


def _snapshot_trees(snapshot):
    from ._directory_state import initial_path
    from ._directory_graph import resource_groups
    result = {g.initial_relative_path or g.relative_path:g.tree for g in resource_groups(snapshot.resources).values()}
    for item in checkpointed(snapshot.resources):
        if item.directory_group is not None:continue
        if item.tree is not None or item.initial_relative_path is not None:
            path = initial_path(item)
            _relative(path)
            if result.setdefault(path, item.tree) != item.tree:
                raise ValueError('Shared snapshot directory has conflicting content')
    return result


def _verify_snapshot_trees(snapshot, checkpoint):
    from ._directory_tree import DirectoryLimits, verify_tree
    workspace = Path(snapshot.execution_directory).resolve()
    for relative, manifest in _snapshot_trees(snapshot).items():
        path = workspace/_relative(relative)
        if not path.resolve().is_relative_to(workspace):
            raise ValueError('Snapshot directory escaped workspace')
        if manifest is None:
            from ._directory_state import verify_absent
            checkpoint(); verify_absent(path,checkpoint=checkpoint); continue
        limits = DirectoryLimits(total_bytes=max(1, manifest.total_bytes), entries=max(1, len(manifest.entries)),
            depth=max((len(e.path.split('/')) for e in manifest.entries), default=1))
        verify_tree(path, manifest, limits=limits, checkpoint=checkpoint)


def _snapshot_files(snapshot):
    result = {}
    trees = _snapshot_trees(snapshot)
    tree_nodes = {}
    def node(path, kind, desc=None):
        _relative(path)
        prior = tree_nodes.setdefault(path.casefold(), (path, kind, desc))
        if prior != (path, kind, desc):
            raise ValueError('Snapshot directory paths alias or have conflicting content')
    for root, manifest in checkpointed(trees.items()):
        node(root, 'directory' if manifest is not None else 'absent_directory')
        for entry in checkpointed(manifest.entries if manifest is not None else ()):
            path = root+'/'+entry.path
            desc = dict(sha256=entry.sha256, size=entry.size) if entry.kind == 'file' else None
            node(path, entry.kind, desc)
            if desc is not None:
                result[path] = desc
    for root in checkpointed(trees):
        prefix = root.casefold()+'/'
        if any(other.casefold().startswith(prefix) for other in checkpointed(trees) if other != root):
            raise ValueError('Captured snapshot directory roots overlap')
    for item in checkpointed(snapshot.resources):
        if item.directory_group is not None:continue
        if item.sha256 is None:
            if (item.tree is None and item.active and item.required and item.kind == 'directory' and
                    item.access != 'write' and item.role != 'swmm:temporary_directory'):
                raise ValueError('Required snapshot directory lacks captured membership')
            if item.active and item.required and item.kind == 'file' and item.access != 'write':
                raise ValueError('Required snapshot input lacks captured content')
            continue
        if item.kind != 'file' or item.access == 'write':
            raise ValueError('Only immutable snapshot inputs may carry captured bytes')
        _relative(item.relative_path)
        desc = dict(sha256=item.sha256, size=item.size)
        descriptor(desc)
        if result.setdefault(item.relative_path, desc) != desc:
            raise ValueError('Shared snapshot path has conflicting content')
        if trees:
            node(item.relative_path, 'file', desc)
    if trees:
        for path, kind, desc in checkpointed(tuple(tree_nodes.values())):
            for parent in _relative(path).parents:
                if parent != Path('.'):
                    # Include implicit root parents: Inputs/a and inputs/b must
                    # not reconstruct differently on case-sensitive hosts.
                    node(parent.as_posix(), 'directory')
        for root, manifest in checkpointed(trees.items()):
            expected = {root+'/'+e.path for e in checkpointed(manifest.entries if manifest is not None else ()) if e.kind == 'file'}
            if {p for p in checkpointed(result) if p.casefold().startswith(root.casefold()+'/')} != expected:
                raise ValueError('Snapshot file lies outside the captured directory membership')
    return result


class _DigestOnly:
    @staticmethod
    def put_bytes(raw):
        return dict(sha256=hashlib.sha256(raw).hexdigest(), size=len(raw))


def execution_digest(snapshot, inputs):
    """Bind complete execution evidence, excluding its two physical locations.

    Original paths/run identity remain provenance in the saved snapshot. Resume
    uses that snapshot with a relocated execution directory/library, never a
    newly invented snapshot that silently changes model/configuration identity.
    """
    if type(snapshot) is not RunSnapshot:
        raise TypeError('Checkpoint requires a RunSnapshot')
    data = Codec(_DigestOnly(), result_version=snapshot_codec_version(snapshot)).encode(snapshot)
    data['fields']['execution_directory'] = ''
    data['fields']['backend']['fields']['library'] = None
    return hashlib.sha256(canonical(dict(snapshot=data, inputs=inputs, native_abi=2))).hexdigest()


class _Blobs:
    def __init__(self, root, limits, checkpoint, inventory=None):
        self.root, self.limits, self.checkpoint = root, limits, checkpoint
        self.inventory = {} if inventory is None else dict(inventory)
        self.total = sum(self.inventory.values())
        self.used = set()
        self.loaded = {}
        self.loaded_size = 0

    def _reserve(self, sha, size):
        descriptor(dict(sha256=sha, size=size))
        if sha in self.inventory:
            if self.inventory[sha] != size:
                raise ValueError('Conflicting checkpoint blob size')
        else:
            if self.total + size > self.limits.total_bytes:
                raise ValueError('Checkpoint exceeds total byte limit')
            self.inventory[sha] = size
            self.total += size
        return self.root/sha

    def put_bytes(self, raw):
        if type(raw) is not bytes:
            raise TypeError('Checkpoint byte content must be immutable')
        desc = _DigestOnly.put_bytes(raw)
        path = self._reserve(desc['sha256'], desc['size'])
        self.checkpoint()
        if not path.exists():
            with path.open('xb') as out:
                if out.write(raw) != len(raw): raise OSError('Short checkpoint write')
                out.flush(); os.fsync(out.fileno())
        return desc

    def put_file(self, source, expected=None, *, expected_size=None):
        source = Path(source)
        size = _regular(source).st_size
        if expected_size is not None and (type(expected_size) is not int or expected_size != size):
            raise ValueError('Checkpoint source differs from native output extent')
        if size > self.limits.total_bytes:
            raise ValueError('Checkpoint file exceeds total byte limit')
        if expected is not None and descriptor(expected)[1] != size:
            raise ValueError('Checkpoint source differs from captured size')
        temporary = self.root/('.copy-'+uuid.uuid4().hex)
        try:
            with temporary.open('xb') as out:
                sha = _stream(source, size, write=out, checkpoint=self.checkpoint)
                out.flush(); os.fsync(out.fileno())
            desc = dict(sha256=sha, size=size)
            if expected is not None and desc != expected:
                raise ValueError('Checkpoint source differs from captured hash')
            destination = self._reserve(sha, size)
            if not destination.exists():
                temporary.rename(destination)
            return desc
        finally:
            if temporary.exists(): temporary.unlink()

    def get_path(self, sha, size):
        descriptor(dict(sha256=sha, size=size))
        if self.inventory.get(sha) != size:
            raise ValueError('Unlisted checkpoint blob')
        self.used.add(sha)
        return self.root/sha

    def read(self, desc, limit):
        sha, size = descriptor(desc)
        path = self.get_path(sha, size)
        if size > limit: raise ValueError('Checkpoint state exceeds byte limit')
        if _regular(path).st_size != size: raise ValueError('Checkpoint blob size changed')
        self.checkpoint()
        with path.open('rb') as stream: raw = stream.read(size+1)
        if len(raw) != size or hashlib.sha256(raw).hexdigest() != sha:
            raise ValueError('Checkpoint blob content changed')
        return raw

    def get_bytes(self, sha, size):
        if sha not in self.loaded:
            if self.loaded_size + size > self.limits.manifest_bytes:
                raise ValueError('Checkpoint metadata exceeds byte limit')
            self.loaded[sha] = self.read(dict(sha256=sha, size=size), self.limits.manifest_bytes)
            self.loaded_size += size
        self.get_path(sha, size)
        return self.loaded[sha]


def _native_header(raw, snapshot, binding):
    # ABI 2 fixed prefix: envelope(52), ESCLK001(8), four i32, four f64,
    # five i32, eight f64, three u64 and warnings i32. Full owner validation
    # belongs to native Validate after solver reconstruction.
    if len(raw) < 220 or raw[:8] != b'ESCKPT02' or raw[52:60] != b'ESCLK001':
        raise ValueError('Unsupported native checkpoint envelope')
    version, family, engine = struct.unpack_from('<Iii', raw, 8)
    if version != 2 or family not in (0, 1) or engine != snapshot.backend.engine_version:
        raise ValueError('Native checkpoint engine mismatch')
    if raw[20:52].hex() != binding or struct.unpack_from('<i', raw, 60)[0] != family:
        raise ValueError('Native checkpoint execution binding mismatch')
    custom = snapshot.backend.key == 'easysewer:flexible-ponding'
    if snapshot.backend.key not in ('swmm:standard', 'easysewer:flexible-ponding') or bool(family) != custom:
        raise ValueError('Unsupported checkpoint backend or family mismatch')
    duration = struct.unpack_from('<d', raw, 100)[0] / 1000
    seconds = struct.unpack_from('<d', raw, 160)[0] / 86400000 * 86400
    if not all(math.isfinite(v) for v in (duration, seconds)) or seconds < 0 or seconds > duration+1e-6:
        raise ValueError('Invalid native checkpoint clock')
    if duration != snapshot.options.duration.total_seconds():
        raise ValueError('Native checkpoint duration differs from snapshot')
    return custom, seconds


def _tree_files(root, manifest):
    if manifest is None:return {}
    return {root+'/'+e.path:dict(sha256=e.sha256,size=e.size)
            for e in checkpointed(manifest.entries) if e.kind == 'file'}


def _verify_directory_states(workspace, states, checkpoint):
    from ._directory_tree import DirectoryLimits, verify_tree
    for relative, manifest in checkpointed(states.items()):
        if manifest is None:
            from ._directory_state import verify_absent
            checkpoint(); verify_absent(Path(workspace)/_relative(relative),checkpoint=checkpoint); continue
        limits = DirectoryLimits(total_bytes=max(1,manifest.total_bytes),entries=max(1,len(manifest.entries)),
            depth=max((len(e.path.split('/')) for e in checkpointed(manifest.entries)),default=1))
        verify_tree(Path(workspace)/_relative(relative),manifest,limits=limits,checkpoint=checkpoint)


def _decode_directory_states(data, snapshot, blobs):
    from ._directory_state import states_for
    from ._checkpoint_directory_outputs import unwrap
    data=unwrap(data)
    if data['schema_version'] not in (MUTABLE_DIRECTORY_VERSION, ABSENT_DIRECTORY_VERSION, LINKED_DIRECTORY_VERSION, GROUP_DIRECTORY_VERSION, MIXED_RESOURCE_VERSION):
        return states_for(snapshot.resources, {})
    entries = data['directory_states']
    if type(entries) is not list:raise TypeError('Expected mutable directory state inventory')
    states = {}; order = []
    for entry in checkpointed(entries):
        exact(entry, ('relative_path','tree'))
        path = entry['relative_path']; _relative(path); order.append(path)
        states[path] = Codec(blobs,result_version='1.9' if data['schema_version']==MIXED_RESOURCE_VERSION else '1.8' if data['schema_version'] == GROUP_DIRECTORY_VERSION else '1.7' if data['schema_version'] == LINKED_DIRECTORY_VERSION else '1.6' if data['schema_version'] == ABSENT_DIRECTORY_VERSION else '1.5').decode(entry['tree'])
        from ._directory_tree import DirectoryManifest
        if type(states[path]) is not DirectoryManifest and not (states[path] is None and data['schema_version'] in (ABSENT_DIRECTORY_VERSION, LINKED_DIRECTORY_VERSION, GROUP_DIRECTORY_VERSION, MIXED_RESOURCE_VERSION)):
            raise TypeError('Expected a complete directory state manifest or explicit absence')
        if blobs is not None:
            for desc in _tree_files(path,states[path]).values():blobs.get_path(*descriptor(desc))
    if order != sorted(set(order)):raise ValueError('Mutable directory states must be sorted and unique')
    return states_for(snapshot.resources,states)


def _decode(data, blobs):
    from ._checkpoint_directory_outputs import unwrap, decode as decode_output_trees
    extended=data; data=unwrap(data)
    mutable = data.get('schema_version') in (MUTABLE_DIRECTORY_VERSION, ABSENT_DIRECTORY_VERSION, LINKED_DIRECTORY_VERSION, GROUP_DIRECTORY_VERSION, MIXED_RESOURCE_VERSION)
    exact(data, ('kind','schema_version','snapshot','execution_sha256','native_inputs',
                 'native_state','worker_state','trace','outputs','blobs') + (('directory_states',) if mutable else ()))
    if data['kind'] != KIND or data['schema_version'] not in (VERSION, DIRECTORY_VERSION, MUTABLE_DIRECTORY_VERSION, ABSENT_DIRECTORY_VERSION, LINKED_DIRECTORY_VERSION, GROUP_DIRECTORY_VERSION, MIXED_RESOURCE_VERSION):
        raise ValueError('Unsupported complete checkpoint format')
    snapshot = Codec(blobs, result_version='1.9' if data['schema_version']==MIXED_RESOURCE_VERSION else '1.8' if data['schema_version'] == GROUP_DIRECTORY_VERSION else '1.7' if data['schema_version'] == LINKED_DIRECTORY_VERSION else '1.6' if data['schema_version'] == ABSENT_DIRECTORY_VERSION else '1.5' if mutable else '1.4' if data['schema_version'] == DIRECTORY_VERSION else '1.1').decode(data['snapshot'])
    if type(snapshot) is not RunSnapshot: raise TypeError('Expected checkpoint snapshot')
    if not snapshot.backend.available or not snapshot.backend.sha256:
        raise ValueError('Checkpoint snapshot lacks an available identified engine')
    for desc in _snapshot_files(snapshot).values(): blobs.get_path(*descriptor(desc))
    _decode_directory_states(data, snapshot, blobs)
    if type(data['native_inputs']) is not list: raise TypeError('Expected native input inventory')
    identities = []
    for item in data['native_inputs']:
        exact(item, ('identity','blob'))
        key = item['identity']
        if type(key) is not str or not INPUT_KEY.fullmatch(key): raise ValueError('Invalid native input identity')
        identities.append(key); blobs.get_path(*descriptor(item['blob']))
    if identities != sorted(set(identities)): raise ValueError('Native inputs must be sorted and unique')
    binding = execution_digest(snapshot, data['native_inputs'])
    if data['execution_sha256'] != binding: raise ValueError('Checkpoint execution digest mismatch')
    native = blobs.read(data['native_state'], blobs.limits.native_bytes)
    custom, seconds = _native_header(native, snapshot, binding)
    if type(data['outputs']) is not list: raise TypeError('Expected native output inventory')
    for index, item in enumerate(data['outputs']):
        exact(item, ('index','role','text','blob'))
        if (type(item['index']) is not int or item['index'] != index or
                type(item['role']) is not int or not 0 <= item['role'] <= 5 or type(item['text']) is not bool):
            raise ValueError('Invalid native output descriptor')
        blobs.get_path(*descriptor(item['blob']))
    worker = None
    if custom:
        worker = blobs.read(data['worker_state'], blobs.limits.worker_bytes)
        state = JsonDocument.from_bytes(worker).data
        exact(state, ('kind','version','binding','state','trace'))
        if state['kind'] != 'easysewer:checkpoint:flexible-worker' or type(state['version']) is not int or state['version'] != 1:
            raise ValueError('Unsupported worker checkpoint format')
        if canonical(state['trace']) != canonical(data['trace']):
            raise ValueError('Worker checkpoint trace descriptor mismatch')
        if (type(state['state']) is not dict or type(state['state'].get('time')) not in (int,float)
                or state['state']['time'] != seconds):
            raise ValueError('Native and worker checkpoint clocks disagree')
        if type(state['binding']) is not dict: raise ValueError('Invalid worker binding')
        expected = {k:getattr(snapshot.backend,k) for k in ('sha256','engine_version','platform','architecture','abi')}
        if canonical(state['binding'].get('engine')) != canonical(expected):
            raise ValueError('Worker checkpoint engine differs from snapshot')
        if snapshot.backend_settings is None: raise ValueError('Worker checkpoint configuration is missing')
        parameters = JsonDocument.from_bytes(snapshot.backend_settings).data
        parameters['trace'] = parameters['trace'] is not None
        if canonical(state['binding'].get('parameters')) != canonical(parameters):
            raise ValueError('Worker checkpoint parameters differ from snapshot')
    elif data['worker_state'] is not None or data['trace'] is not None:
        raise ValueError('Standard checkpoint cannot contain custom worker state')
    if data['trace'] is not None: blobs.get_path(*descriptor(data['trace']))
    decode_output_trees(extended,snapshot,blobs)
    if blobs.used != set(blobs.inventory): raise ValueError('Unreferenced checkpoint blobs')
    return snapshot, native, worker, seconds


def execution_layout(snapshot, root, *, schema=None, directory_states=None, _declarations=None):
    """Validate all executed file consumers before opening or creating files."""
    from ..model import Model
    from ..io.inp import InpDocument
    from ._preparation import inventory
    claims = tuple(item for item in checkpointed(snapshot.resources)
                   if item.kind == 'directory' and item.role != 'swmm:temporary_directory')
    if _declarations is None:
        from ._execution_model import execution_model
        model = execution_model(snapshot,schema=schema,_defer_declarations=True)
        plans = inventory(model, input_directory=root, working_directory=root,
                          _captured_directories=claims)
    else:
        if schema is not None:raise ValueError('Use explicit schema or verified declarations, not both')
        from ._checkpoint_declarations import validate
        from ._preparation import ResourcePlan
        plans=tuple(ResourcePlan(use=u,original=root/_relative(u.file.path)) for u in validate(snapshot,_declarations))
    from ._directory_graph import consumer_key
    graph_records={consumer_key(r.owner,r.field):r for r in snapshot.resources if r.directory_group is not None}
    for plan in plans:
        use=plan.use;key=consumer_key(use.owner,use.path)
        if key in graph_records:
            record=graph_records.pop(key)
            if any(getattr(record,n)!=getattr(use,n) for n in ('role','format','kind','access','active','required')) or use.file.path!=record.relative_path or use.file.base_directory is not None:
                raise ValueError('Executed resource graph declaration differs from snapshot')
    if graph_records:raise ValueError('Executed input lacks a declared resource graph consumer')
    inputs = _snapshot_files(snapshot)
    directories = set()
    input_names = set(inputs)
    from ._directory_state import states_for
    input_trees = _snapshot_trees(snapshot)
    from ._mutable_outputs import startup_states, checkpoint_owners
    owned_outputs=checkpoint_owners(snapshot)
    current_trees = startup_states(snapshot,states_for(snapshot.resources, directory_states))
    input_trees.update(current_trees)
    for name, manifest in checkpointed(current_trees.items()):
        inputs.update(_tree_files(name, manifest))
    input_names = set(inputs)
    output_roots = set()
    scratch_roots = set()
    absent_directories = set()
    output_names = {'model.rpt','model.out'}
    for name, manifest in checkpointed(input_trees.items()):
        if manifest is None:
            absent_directories.add(name); directories.add(_relative(name).parent)
        else:
            directories.add(_relative(name))
            directories.update(_relative(name+'/'+entry.path) for entry in checkpointed(manifest.entries) if entry.kind == 'directory')
    from ._directory_graph import resource_groups, view_manifest
    graph_views={}
    for group in resource_groups(snapshot.resources).values():
        tree=current_trees[group.relative_path] if group.initial_relative_path is not None else group.tree
        for view in group.state.layout.views:graph_views[group.relative_path+'/'+view.path]=view_manifest(tree,view.path,view.kind)
    for plan in checkpointed(plans):
        use = plan.use
        relative = _relative(use.file.path)
        if use.file.base_directory is not None or plan.original is not None and plan.original != root/relative:
            raise ValueError('Executed input contains an external resource location')
        if use.kind == 'directory':
            if use.file.path in graph_views:
                if graph_views[use.file.path] is None:absent_directories.add(use.file.path)
                else:directories.add(relative)
            elif use.role == 'swmm:temporary_directory' or use.file.path in input_trees and input_trees[use.file.path] is not None or (use.active and use.access == 'write'):
                directories.add(relative)
            else:
                # Missing optional and inactive directories must remain absent.
                absent_directories.add(use.file.path)
                directories.add(relative.parent)
            if use.role == 'swmm:temporary_directory':
                scratch_roots.add(use.file.path)
            elif use.active and use.access == 'write':
                if use.file.path in output_roots:
                    raise ValueError('Duplicate checkpoint output directory')
                output_roots.add(use.file.path)
        else:
            if use.file.path in graph_views:
                # The aggregate forest owns parent creation and current absence.
                continue
            directories.add(relative.parent)
            if use.active and use.access == 'write':
                if use.file.path in output_names: raise ValueError('Duplicate checkpoint output path')
                output_names.add(use.file.path)
            elif use.active and use.required and use.file.path not in inputs:
                raise ValueError('Executed input resource is absent from the checkpoint')
    if snapshot.backend_settings is not None:
        settings = JsonDocument.from_bytes(snapshot.backend_settings).data
        trace = settings.get('trace')
        if trace is not None:
            relative = _relative(trace)
            if trace in output_names: raise ValueError('Checkpoint trace aliases an output')
            output_names.add(trace); directories.add(relative.parent)
    if input_names & output_names or 'model.inp' in input_names | output_names:
        raise ValueError('Checkpoint input/output ownership conflict')
    # A checkpoint can move between case-sensitive and case-insensitive
    # filesystems. Reject aliases before creating anything, including an
    # output that would overwrite an input only on the destination host.
    names = input_names | output_names | {'model.inp'}
    folded = [name.casefold() for name in checkpointed(names)]
    if len(set(folded)) != len(folded):
        raise ValueError('Checkpoint filenames alias on a portable filesystem')
    file_paths = {_relative(p) for p in checkpointed(folded)}
    folded_directories = {Path(p.as_posix().casefold()) for p in checkpointed(directories)}
    folded_directories.update(parent for p in checkpointed(tuple(folded_directories)) for parent in p.parents)
    for path in checkpointed(file_paths):
        if any(parent in file_paths for parent in path.parents) or path in folded_directories:
            raise ValueError('Checkpoint file/directory ownership conflict')
    if claims:
        # Include implicit parents and retain spelling while comparing portable
        # identities. Files inside a declared output tree are legitimate output
        # members, but no output may be inside a captured input tree.
        namespace = {}
        def claim(path, kind):
            name = path.as_posix()
            previous = namespace.setdefault(name.casefold(), (name, kind))
            if previous != (name, kind):
                raise ValueError('Checkpoint paths alias or have a file/directory ownership conflict')
        for name in checkpointed(names):
            path = _relative(name); claim(path, 'file')
            for parent in path.parents:
                if parent != Path('.'):claim(parent, 'directory')
        for path in checkpointed(directories):
            if path != Path('.'):claim(path, 'directory')
            for parent in path.parents:
                if parent != Path('.'):claim(parent, 'directory')
        def beneath(path, folder):
            p, d = path.casefold(), folder.casefold()
            return p == d or p.startswith(d+'/')
        for folder in checkpointed(absent_directories):
            if any(beneath(name, folder) for name in checkpointed(names)) or any(
                    beneath(path.as_posix(), folder) for path in checkpointed(directories)):
                raise ValueError('Checkpoint reconstruction would create an absent directory resource')
        for folder in checkpointed(input_trees):
            if any((beneath(name, folder) or beneath(folder, name)) and owned_outputs.get(name)!=folder for name in checkpointed(output_names)):
                raise ValueError('Checkpoint output overlaps captured input directory')
        for folder in checkpointed(output_roots):
            if folder in owned_outputs:continue
            if any(beneath(name, folder) or beneath(folder, name) for name in input_names | {'model.inp'} | set(input_trees)):
                raise ValueError('Checkpoint output directory overlaps an input')
            # Nested explicitly declared writers share an outer output tree.
            # Portable spelling and file/directory conflicts were checked above.
        for folder in checkpointed(scratch_roots):
            if any(beneath(name, folder) or beneath(folder, name) for name in names | set(input_trees) | output_roots):
                raise ValueError('Checkpoint scratch directory overlaps a resource')
    if output_roots:
        # Path sets on Windows discard case-distinct spellings. Validate the
        # original string declarations before any reconstruction creates files.
        from ._checkpoint_directory_outputs import roots as output_forest_roots
        output_forest_roots(snapshot)
    from ._checkpoint_declarations import validate
    validate(snapshot,tuple(plan.use for plan in plans))
    return inputs, directories, output_names


@dataclass(frozen=True)
class Checkpoint:
    directory: Path
    manifest: bytes
    snapshot: RunSnapshot
    native_state: bytes
    worker_state: bytes | None
    simulation_seconds: float

    @property
    def data(self):
        return JsonDocument.from_bytes(self.manifest).data

    @property
    def binding(self):
        return bytes.fromhex(self.data['execution_sha256'])

    def copy_blob(self, desc, destination, *, checkpoint=lambda: None, on_create=None):
        """Create an independent verified copy; never return a writable blob."""
        sha, size = descriptor(desc)
        listed = {item['sha256']:item['size'] for item in self.data['blobs']}
        if listed.get(sha) != size: raise ValueError('Unlisted checkpoint content')
        destination = Path(destination)
        identity = None
        try:
            with destination.open('xb') as out:
                value = os.fstat(out.fileno())
                identity = (value.st_dev, value.st_ino)
                if on_create is not None:on_create(identity)
                observed = _stream(self.directory/'blobs'/sha, size, write=out, checkpoint=checkpoint)
                out.flush(); os.fsync(out.fileno())
            if observed != sha: raise ValueError('Checkpoint content changed after load')
        except BaseException:
            if identity is not None and destination.exists() and not destination.is_symlink():
                if fingerprint(destination)[:2] == identity: destination.unlink()
            raise
        return destination

    def materialize(self, directory, *, schema=None, checkpoint=lambda: None):
        """Rebuild a new workspace with cooperative checks during layout as well as IO."""
        with checkpoint_scope(checkpoint):
            checkpoint()
            return self._materialize(directory, schema=schema, checkpoint=checkpoint)

    def _materialize(self, directory, *, schema=None, checkpoint=lambda: None):
        """Reconstruct declared inputs in a new workspace, without loading code.

        Parse the exact executed INP and check every file consumer before any
        native open can follow. Unsupported opaque consumers require a codec;
        they are never permitted to escape reconstruction through raw text.
        """
        root = Path(directory).absolute()
        from ._mutable_outputs import startup_states
        states = startup_states(self.snapshot,_decode_directory_states(self.data, self.snapshot, None))
        inputs, directories, _ = execution_layout(self.snapshot, root, schema=schema, directory_states=states)
        root.mkdir()
        identity = fingerprint(root)[:2]
        try:
            for path in sorted(directories, key=lambda p:(len(p.parts), p.as_posix())):
                checkpoint()
                (root/path).mkdir(parents=True, exist_ok=True)
            trees = _snapshot_trees(self.snapshot); trees.update(states)
            links = {name+'/'+e.path:(name,e) for name,tree in trees.items() if tree is not None
                     for e in checkpointed(tree.entries) if e.hardlink_to is not None}
            created_files = {}
            for relative, desc in inputs.items():
                target = root/_relative(relative)
                target.parent.mkdir(parents=True, exist_ok=True)
                if relative in links:
                    from ._directory_tree import link_member
                    name,entry = links[relative]
                    link_member(root/_relative(name),entry,expected_identity=created_files[name+'/'+entry.hardlink_to],checkpoint=checkpoint)
                elif links:
                    created = []; self.copy_blob(desc,target,checkpoint=checkpoint,on_create=created.append)
                    created_files[relative] = created[0]
                else:
                    self.copy_blob(desc, target, checkpoint=checkpoint)
            _verify_snapshot_trees(replace(self.snapshot, execution_directory=str(root)), checkpoint)
            _verify_directory_states(root, states, checkpoint)
            checkpoint()
            with (root/'model.inp').open('xb') as stream:
                if stream.write(self.snapshot.input_bytes) != len(self.snapshot.input_bytes):
                    raise OSError('Short checkpoint input write')
                stream.flush(); os.fsync(stream.fileno())
        except BaseException as error:
            try: remove_owned_tree(root, parent=root.parent, identity=identity)
            except BaseException as cleanup:
                error.checkpoint_cleanup_error = f'{type(cleanup).__name__}: {cleanup}'
            raise
        return replace(self.snapshot, execution_directory=str(root))


def load(directory, *, limits=Limits(), checkpoint=lambda: None):
    if type(limits) is not Limits: raise TypeError('Expected checkpoint Limits')
    root = Path(directory).absolute()
    if root.is_symlink() or not root.is_dir() or (root/'blobs').is_symlink() or not (root/'blobs').is_dir():
        raise ValueError('Expected a checkpoint directory and owned blob directory')
    if {p.name for p in root.iterdir()} != {'blobs','checkpoint.json','checkpoint.commit'}:
        raise ValueError('Checkpoint is incomplete or contains unexpected files')
    manifest = root/'checkpoint.json'; commit = root/'checkpoint.commit'
    if _regular(manifest).st_size > limits.manifest_bytes or _regular(commit).st_size != 65:
        raise ValueError('Invalid checkpoint manifest or commit size')
    with manifest.open('rb') as stream: raw = stream.read(limits.manifest_bytes+1)
    with commit.open('rb') as stream: marker = stream.read(66)
    if len(raw) > limits.manifest_bytes or marker != hashlib.sha256(raw).hexdigest().encode()+b'\n':
        raise ValueError('Checkpoint manifest commit mismatch')
    data = JsonDocument.from_bytes(raw).data
    if type(data) is not dict or type(data.get('blobs')) is not list: raise ValueError('Invalid checkpoint manifest')
    inventory = {}; total = 0
    for item in data['blobs']:
        sha, size = descriptor(item)
        if sha in inventory: raise ValueError('Duplicate checkpoint blob')
        total += size
        if total > limits.total_bytes: raise ValueError('Checkpoint exceeds total byte limit')
        inventory[sha] = size
    if {p.name for p in (root/'blobs').iterdir()} != set(inventory):
        raise ValueError('Checkpoint file inventory mismatch')
    for sha, size in inventory.items():
        if _stream(root/'blobs'/sha, size, checkpoint=checkpoint) != sha:
            raise ValueError('Checkpoint blob digest mismatch')
    blobs = _Blobs(root/'blobs', limits, checkpoint, inventory)
    snapshot, native, worker, seconds = _decode(data, blobs)
    return Checkpoint(root, raw, snapshot, native, worker, seconds)


class Builder:
    """Capture inputs before obtaining binding, then capture native/worker state.

    Callers hold the solver serialization lock for this entire context. A new
    directory remains uncommitted until finish succeeds. No arbitrary target
    is removed on errors: cleanup is restricted to this created directory.
    """
    def __init__(self, directory, snapshot, *, limits=Limits(), checkpoint=lambda: None):
        if type(snapshot) is not RunSnapshot or type(limits) is not Limits:
            raise TypeError('Expected RunSnapshot and checkpoint Limits')
        self.root = Path(directory).absolute()
        self.snapshot, self.limits, self.checkpoint = snapshot, limits, checkpoint
        self.identity = None; self.done = False; self.sealed = False
        self.inputs = {}

    def __enter__(self):
        self.root.mkdir()
        self.identity = fingerprint(self.root)[:2]
        try:
            (self.root/'blobs').mkdir()
            self.blobs = _Blobs(self.root/'blobs', self.limits, self.checkpoint)
            self.encoded_snapshot = Codec(self.blobs, result_version=snapshot_codec_version(self.snapshot)).encode(self.snapshot)
            _verify_snapshot_trees(self.snapshot, self.checkpoint)
            workspace = Path(self.snapshot.execution_directory).resolve()
            for relative, desc in _snapshot_files(self.snapshot).items():
                path = workspace/_relative(relative)
                if not path.resolve().is_relative_to(workspace): raise ValueError('Snapshot input escaped workspace')
                self.blobs.put_file(path, desc)
            _verify_snapshot_trees(self.snapshot, self.checkpoint)
            from ._directory_state import mutable_groups
            from ._directory_tree import DirectoryLimits
            from ._directory_state import observe
            self.directory_states = {}
            from ._directory_graph import DirectoryGroupSnapshot, inspect_graph
            for relative,record in checkpointed(mutable_groups(self.snapshot.resources).items()):
                path = workspace/_relative(relative)
                if type(record) is DirectoryGroupSnapshot:
                    tree=inspect_graph(path,record.state.layout,checkpoint=self.checkpoint).tree
                else:tree = observe(path,allow_absent=True,limits=DirectoryLimits(total_bytes=self.limits.total_bytes),checkpoint=self.checkpoint)
                self.directory_states[relative] = tree
                for name, desc in _tree_files(relative,tree).items():
                    self.blobs.put_file(workspace/_relative(name),desc)
            _verify_directory_states(workspace,self.directory_states,self.checkpoint)
            from ._checkpoint_directory_outputs import capture
            self.output_directory_states=capture(self.snapshot,self.blobs,self.checkpoint)
            return self
        except BaseException:
            self.__exit__(*sys.exc_info())
            raise

    def add_input(self, identity, path, *, expected=None):
        if self.sealed or self.done: raise ValueError('Checkpoint input inventory is sealed')
        if type(identity) is not str or not INPUT_KEY.fullmatch(identity) or identity in self.inputs:
            raise ValueError('Invalid or duplicate native input identity')
        self.inputs[identity] = self.blobs.put_file(path, expected)

    @property
    def binding(self):
        self.sealed = True
        inputs = [dict(identity=k,blob=v) for k,v in sorted(self.inputs.items())]
        return bytes.fromhex(execution_digest(self.snapshot, inputs))

    def finish(self, native_state, outputs, *, worker_state=None, trace=None, output_locations=None):
        if self.done: raise ValueError('Checkpoint already published')
        if type(native_state) is not bytes or len(native_state) > self.limits.native_bytes:
            raise ValueError('Invalid or oversized native state')
        if worker_state is not None and (type(worker_state) is not bytes or len(worker_state) > self.limits.worker_bytes):
            raise ValueError('Invalid or oversized worker state')
        _verify_snapshot_trees(self.snapshot,self.checkpoint)
        _verify_directory_states(self.snapshot.execution_directory,self.directory_states,self.checkpoint)
        outputs=tuple(outputs)
        result = []
        for item in outputs:
            result.append(dict(index=item.index, role=item.role, text=item.text,
                               blob=self.blobs.put_file(item.path, expected_size=item.size)))
        mixed = snapshot_codec_version(self.snapshot)=='1.9'
        grouped = mixed or snapshot_codec_version(self.snapshot)=='1.8'
        absent = snapshot_codec_version(self.snapshot) == '1.6' or any(tree is None for tree in self.directory_states.values())
        linked = snapshot_codec_version(self.snapshot) == '1.7' or any(tree is not None and tree.has_hardlinks for tree in self.directory_states.values())
        encoded_snapshot = Codec(self.blobs,result_version='1.7').encode(self.snapshot) if linked and not grouped else self.encoded_snapshot
        data = dict(kind=KIND,schema_version=MIXED_RESOURCE_VERSION if mixed else GROUP_DIRECTORY_VERSION if grouped else LINKED_DIRECTORY_VERSION if linked else ABSENT_DIRECTORY_VERSION if absent else MUTABLE_DIRECTORY_VERSION if snapshot_codec_version(self.snapshot) == '1.5' else DIRECTORY_VERSION if snapshot_codec_version(self.snapshot) == '1.4' else VERSION,snapshot=encoded_snapshot,
                    execution_sha256=self.binding.hex(),
                    native_inputs=[dict(identity=k,blob=v) for k,v in sorted(self.inputs.items())],
                    native_state=self.blobs.put_bytes(native_state),
                    worker_state=self.blobs.put_bytes(worker_state) if worker_state is not None else None,
                    trace=self.blobs.put_file(trace) if trace is not None else None,outputs=result,
                    blobs=[dict(sha256=k,size=v) for k,v in sorted(self.blobs.inventory.items())])
        # Outputs and trace can take time to copy. Recheck complete directory
        # evidence after all source copying, before publishing the commit.
        _verify_snapshot_trees(self.snapshot,self.checkpoint)
        _verify_directory_states(self.snapshot.execution_directory,self.directory_states,self.checkpoint)
        if data['schema_version'] in (MUTABLE_DIRECTORY_VERSION, ABSENT_DIRECTORY_VERSION, LINKED_DIRECTORY_VERSION, GROUP_DIRECTORY_VERSION, MIXED_RESOURCE_VERSION):
            data['directory_states'] = [dict(relative_path=path,tree=Codec(self.blobs,result_version='1.9' if mixed else '1.8' if grouped else '1.7' if linked else '1.6' if absent else '1.5').encode(tree))
                for path,tree in sorted(self.directory_states.items())]
        from ._checkpoint_directory_outputs import extend
        data=extend(data,self.snapshot,self.output_directory_states,output_locations,outputs,trace,self.blobs)
        _verify_directory_states(self.snapshot.execution_directory,self.output_directory_states,self.checkpoint)
        raw = JsonDocument.from_data(data).to_bytes()
        if len(raw) > self.limits.manifest_bytes: raise ValueError('Checkpoint manifest exceeds byte limit')
        self.blobs.used.clear()
        _decode(data, self.blobs)
        for name, content in (('checkpoint.json',raw),('checkpoint.commit',hashlib.sha256(raw).hexdigest().encode()+b'\n')):
            self.checkpoint()
            with (self.root/name).open('xb') as stream:
                if stream.write(content) != len(content): raise OSError('Short checkpoint manifest write')
                stream.flush(); os.fsync(stream.fileno())
        value = load(self.root, limits=self.limits, checkpoint=self.checkpoint)
        self.done = True
        return value

    def __exit__(self, kind, error, traceback):
        if not self.done and self.identity is not None:
            try:
                remove_owned_tree(self.root, parent=self.root.parent, identity=self.identity)
            except BaseException as cleanup:
                if error is None: raise
                error.checkpoint_cleanup_error = f'{type(cleanup).__name__}: {cleanup}'
