"""Private Runner context envelope around the qualified Session checkpoint.

The low-level state directory remains format 1.0. This outer format commits
its digest together with Runner diagnostics, cache evidence and resume history.
No stored path selects executable code and loading never creates a solver.
"""
from dataclasses import dataclass, fields
import hashlib
import os
from pathlib import Path

from .backend import BackendArtifact
from .cache_reuse import CacheContext
from .checkpoint import Checkpoint, CheckpointLimits
from .results import CacheConsumption, RunContinuation
from ._checkpoint_container import _Blobs, _regular, _relative, _stream
from ._result_codec import Codec, exact
from ._result_contract import continuation_chain, typed_tree
from ._workspace import fingerprint, remove_owned_tree
from ..io.json import JsonDocument
from ..model.identity import namespace_key
from ..validation import ValidationReport


@dataclass(frozen=True, kw_only=True)
class RunnerContext:
    asset_directory: str
    diagnostics: ValidationReport
    consumed_caches: tuple[CacheConsumption, ...] = ()
    cache_contexts: tuple[CacheContext, ...] = ()
    backend_artifacts: tuple[BackendArtifact, ...] = ()
    steps: int = 0
    continuations: tuple[RunContinuation, ...] = ()

    def __post_init__(self):
        typed_tree(self)
        if len(_relative(self.asset_directory).parts)!=1:
            raise ValueError('Runner assets require one portable directory name')
        if self.steps<0:raise ValueError('Runner step count cannot be negative')
        if not self.diagnostics.is_valid:raise ValueError('Cannot checkpoint a rejected Runner context')
        for values in (self.cache_contexts,self.consumed_caches):
            if len({v.kind for v in values})!=len(values):raise ValueError('Duplicate Runner cache kind')
        paths=set();roles=set()
        for item in self.backend_artifacts:
            namespace_key(item.role)
            path=_relative(item.relative_path)
            if len(path.parts)<2 or path.parts[0]!=self.asset_directory:
                raise ValueError('Backend artifact escaped its Runner asset directory')
            key=path.as_posix().casefold()
            if key in paths or item.role in roles:raise ValueError('Duplicate backend artifact identity')
            paths.add(key);roles.add(item.role)


def _validate(context, state):
    if type(context) is not RunnerContext or type(state) is not Checkpoint:
        raise TypeError('Expected RunnerContext and Checkpoint')
    snapshot=state.snapshot
    continuation_chain(context.continuations,snapshot)
    for previous in context.continuations:
        if previous.execution_sha256!=state._archive.binding.hex():
            raise ValueError('Runner continuation binds a different native execution')
    if context.continuations and (context.continuations[-1].simulation_seconds>state.simulation_seconds or
                                 context.continuations[-1].steps>context.steps):
        raise ValueError('Runner progress precedes its restoration boundary')
    active={r.role.rsplit('.',1)[1].upper() for r in snapshot.resources
            if r.active and r.role.startswith('swmm:interface.')}
    consumed={r.role.rsplit('.',1)[1].upper():r for r in snapshot.resources
              if r.active and r.access!='write' and r.role.startswith('swmm:interface.')}
    if {v.kind for v in context.cache_contexts}!=active or {v.kind for v in context.consumed_caches}!=set(consumed):
        raise ValueError('Runner cache evidence is incomplete for its snapshot')
    contexts={v.kind:v for v in context.cache_contexts}
    for item in context.cache_contexts:
        if item.input_sha256!=snapshot.input_sha256 or item.engine_sha256!=snapshot.backend.sha256:
            raise ValueError('Runner cache context differs from its snapshot')
    for item in context.consumed_caches:
        if (item.consumer_model_sha256!=snapshot.model_sha256 or item.sha256!=consumed[item.kind].sha256 or
            item.reuse.consumer_context_sha256!=contexts[item.kind].sha256 or not item.reuse.allowed):
            raise ValueError('Runner cache consumption differs from its captured conditions')
    existing={r.relative_path.casefold() for r in snapshot.resources if r.relative_path is not None}
    for item in context.backend_artifacts:
        if item.relative_path.casefold() in existing:
            raise ValueError('Backend product aliases a captured resource')
    # The built-in custom worker's trace is part of the state transaction and
    # must also be a declared Runner product; otherwise publication loses it.
    if snapshot.backend_settings is not None:
        trace=JsonDocument.from_bytes(snapshot.backend_settings).data.get('trace')
        if trace is not None and trace not in {v.relative_path for v in context.backend_artifacts}:
            raise ValueError('Runner context omitted its backend trace product')


@dataclass(frozen=True)
class RunnerCheckpoint:
    directory: Path
    manifest: bytes
    state: Checkpoint
    context: RunnerContext

    @classmethod
    def load(cls, directory, *, limits=CheckpointLimits(), checkpoint=lambda: None):
        return load(directory,limits=limits,checkpoint=checkpoint)

    @property
    def config(self):
        from .config import RunConfig
        return RunConfig.from_json_document(JsonDocument.from_bytes(self.snapshot.config_json))

    @property
    def sha256(self):return hashlib.sha256(self.manifest).hexdigest()

    @property
    def snapshot(self):return self.state.snapshot

    @property
    def simulation_seconds(self):return self.state.simulation_seconds

    def materialize(self, directory, *, schema=None, limits=CheckpointLimits(), checkpoint=lambda: None):
        verified=load(self.directory,limits=limits,checkpoint=checkpoint)
        if verified!=self:raise ValueError('Runner checkpoint changed after loading')
        return verified.state.materialize(directory,schema=schema,checkpoint=checkpoint)


def _state_size(state):
    return sum(row['size'] for row in state._archive.data['blobs'])


def _context_data(context, blobs):
    codec=Codec(blobs)
    return {f.name:codec.encode(getattr(context,f.name)) for f in fields(RunnerContext)}


def capture(session, context, directory, *, checkpoint=lambda: None):
    """Save a new combined Runner checkpoint; never replace a previous one.

    Session capture keeps its own qualified default bounds. The outer envelope
    additionally checks the combined blob budget before committing metadata.
    A failed context write does not roll back or delete an earlier checkpoint.
    """
    if type(context) is not RunnerContext:raise TypeError('Expected RunnerContext')
    checkpoint();root=Path(directory).absolute();root.mkdir()
    identity=fingerprint(root)[:2]
    try:
        (root/'blobs').mkdir()
        state=session.save_checkpoint(root/'state')
        _validate(context,state);checkpoint()
        limits=CheckpointLimits()
        blobs=_Blobs(root/'blobs',limits,checkpoint)
        data=dict(kind='easysewer:runner-checkpoint',schema_version='1.0',codec_version='1.2',
                  state_sha256=state.sha256,context=_context_data(context,blobs))
        data['blobs']=[dict(sha256=k,size=v) for k,v in sorted(blobs.inventory.items())]
        if _state_size(state)+sum(blobs.inventory.values())>limits.total_bytes:
            raise ValueError('Combined Runner checkpoint exceeds byte limit')
        raw=JsonDocument.from_data(data).to_bytes()
        if len(raw)>limits.manifest_bytes:raise ValueError('Runner checkpoint manifest exceeds byte limit')
        for name,content in (('runner.json',raw),('runner.commit',hashlib.sha256(raw).hexdigest().encode()+b'\n')):
            checkpoint()
            with (root/name).open('xb') as stream:
                if stream.write(content)!=len(content):raise OSError('Short Runner checkpoint metadata write')
                stream.flush();os.fsync(stream.fileno())
        return load(root,checkpoint=checkpoint)
    except BaseException as error:
        try:remove_owned_tree(root,parent=root.parent,identity=identity)
        except BaseException as cleanup:error.runner_checkpoint_cleanup=f'{type(cleanup).__name__}: {cleanup}'
        raise


def load(directory, *, limits=CheckpointLimits(), checkpoint=lambda: None):
    """Verify a complete envelope without executing code or opening old paths."""
    if type(limits) is not CheckpointLimits:raise TypeError('Expected CheckpointLimits')
    checkpoint();root=Path(directory).absolute()
    if root.is_symlink() or not root.is_dir() or any((root/name).is_symlink() for name in ('blobs','state')):
        raise ValueError('Invalid Runner checkpoint directory')
    if {p.name for p in root.iterdir()}!={'blobs','state','runner.json','runner.commit'}:
        raise ValueError('Incomplete or unexpected Runner checkpoint content')
    path=root/'runner.json'
    if _regular(path).st_size>limits.manifest_bytes or _regular(root/'runner.commit').st_size!=65:
        raise ValueError('Invalid Runner checkpoint manifest/commit size')
    with path.open('rb') as stream:raw=stream.read(limits.manifest_bytes+1)
    with (root/'runner.commit').open('rb') as stream:marker=stream.read(66)
    if len(raw)>limits.manifest_bytes or marker!=hashlib.sha256(raw).hexdigest().encode()+b'\n':
        raise ValueError('Runner checkpoint commit mismatch')
    data=JsonDocument.from_bytes(raw).data
    exact(data,('kind','schema_version','codec_version','state_sha256','context','blobs'))
    if (data['kind']!='easysewer:runner-checkpoint' or data['schema_version']!='1.0' or data['codec_version'] not in ('1.1','1.2')):
        raise ValueError('Unsupported Runner checkpoint contract')
    state=Checkpoint.load(root/'state',limits=limits,checkpoint=checkpoint)
    if state.sha256!=data['state_sha256']:raise ValueError('Runner context identifies different solver state')
    if type(data['blobs']) is not list:raise ValueError('Invalid Runner context blob inventory')
    blobs=_Blobs(root/'blobs',limits,checkpoint)
    for row in data['blobs']:
        exact(row,('sha256','size'))
        if row['sha256'] in blobs.inventory:raise ValueError('Duplicate Runner context blob')
        blobs._reserve(row['sha256'],row['size'])
    if _state_size(state)+sum(blobs.inventory.values())>limits.total_bytes:
        raise ValueError('Combined Runner checkpoint exceeds byte limit')
    if {p.name for p in (root/'blobs').iterdir()}!=set(blobs.inventory):
        raise ValueError('Runner context blob inventory changed')
    for sha,size in blobs.inventory.items():
        if _stream(root/'blobs'/sha,size,checkpoint=checkpoint)!=sha:
            raise ValueError('Runner context blob content changed')
    exact(data['context'],tuple(f.name for f in fields(RunnerContext)))
    codec=Codec(blobs,result_version=data['codec_version'])
    context=RunnerContext(**{k:codec.decode(v) for k,v in data['context'].items()})
    if blobs.used!=set(blobs.inventory):raise ValueError('Unreferenced Runner context content')
    _validate(context,state)
    return RunnerCheckpoint(root,raw,state,context)
