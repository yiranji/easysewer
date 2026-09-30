"""Local result invariants. Construction performs no filesystem/native IO."""

from dataclasses import fields, is_dataclass
from datetime import datetime
from functools import lru_cache
import hashlib
from typing import get_type_hints

from ..io.json import JsonDocument
from ..model.identity import Ref, namespace_key
from ..results.series import digest


@lru_cache(maxsize=None)
def _hints(kind):
    return get_type_hints(kind)


def typed_tree(value):
    """Reject nested mutable/wrongly typed data even in legacy value classes."""
    from ._result_codec import matches
    visited = set()

    def visit(item):
        if id(item) in visited:
            return
        visited.add(id(item))
        if type(item) in (tuple, frozenset):
            for child in item:
                visit(child)
        elif is_dataclass(item):
            hints = _hints(type(item))
            for field in fields(item):
                child = getattr(item, field.name)
                if not matches(child, hints[field.name]):
                    raise TypeError('Invalid nested result field '+type(item).__name__+'.'+field.name)
                visit(child)
    visit(value)


def text(value, *, optional=False):
    if optional and value is None:
        return
    if type(value) is not str or not value or '\0' in value:
        raise ValueError('Expected a nonempty string without NUL')


def size(value, *, optional=False):
    if optional and value is None:
        return
    if type(value) is not int or value < 0:
        raise ValueError('Expected a nonnegative integer byte count')


def identity(value):
    if value.owner is not None and type(value.owner) is not Ref:
        raise TypeError('Expected an artifact/resource owner Ref')
    if type(value.field) is not tuple or any(type(v) not in (str, int) or
            isinstance(v, int) and v < 0 for v in value.field):
        raise TypeError('Expected an immutable field path')


def resource(value):
    identity(value)
    if value.owner is None:
        raise TypeError('A resource requires an owner')
    namespace_key(value.role)
    for name in ('format', 'kind', 'access'):
        text(getattr(value, name))
    for name in ('active', 'required'):
        if type(getattr(value, name)) is not bool:
            raise TypeError('Resource flags must be booleans')
    for name in ('original_path', 'relative_path'):
        text(getattr(value, name), optional=True)
    from ._directory_graph import validate_member
    validate_member(value)
    digest(value.sha256); size(value.size, optional=True)
    if (value.sha256 is None) != (value.size is None):
        raise ValueError('Resource size and hash must be present together')
    if value.initial_relative_path is not None:
        from ._directory_tree import relative_path
        if value.directory_group is None and (value.kind != 'directory' or not value.active or value.access not in ('read','read_write') or
                value.sha256 is not None or value.tree is None and value.required):
            raise ValueError('Independent initial path requires captured directory or optional absence evidence')
        relative_path(value.initial_relative_path)
        relative_path(value.relative_path)
        a, b = value.initial_relative_path.casefold(), value.relative_path.casefold()
        if a == b or a.startswith(b+'/') or b.startswith(a+'/'):
            raise ValueError('Initial directory evidence must be independent of its execution path')
    if value.tree is not None:
        from ._directory_tree import DirectoryManifest
        if (type(value.tree) is not DirectoryManifest or value.kind != 'directory' or
                value.access not in ('read','read_write') or not value.active or value.relative_path is None or value.sha256 is not None):
            raise ValueError('Directory evidence requires an active immutable captured input tree')


def artifact(value):
    namespace_key(value.role); text(value.path); text(value.declared_path, optional=True)
    identity(value); digest(value.sha256); size(value.size)
    if value.sha256 is None or type(value.complete) is not bool:
        raise ValueError('An artifact requires a hash and a boolean completion flag')


def directory_artifact(value):
    from .results import FileArtifact
    from ._directory_tree import DirectoryManifest
    identity(value); namespace_key(value.role)
    text(value.path, optional=True); text(value.original_path); text(value.declared_path, optional=True)
    if value.owner is None or type(value.complete) is not bool or (value.manifest is not None and type(value.manifest) is not DirectoryManifest):
        raise TypeError('Directory artifact requires owner, completion flag and manifest')
    if type(value.files) is not tuple:
        raise TypeError('Directory artifact files must be immutable')
    expected = tuple(e for e in value.manifest.entries if e.kind == 'file') if value.manifest is not None else ()
    if len(value.files) != len(expected):
        raise ValueError('Directory artifact member count differs from its manifest')
    for pair, entry in zip(value.files, expected):
        if type(pair) is not tuple or len(pair) != 2 or pair[0] != entry.path or type(pair[1]) is not FileArtifact:
            raise ValueError('Directory artifact file identity/order differs from its manifest')
        member = pair[1]
        if (member.role != value.role or member.owner != value.owner or member.field != value.field or
                member.complete != value.complete or member.declared_path != value.declared_path or
                (member.sha256, member.size) != (entry.sha256, entry.size)):
            raise ValueError('Directory artifact member evidence differs from its manifest')
        if value.path is not None:
            from pathlib import Path
            if Path(member.path) != Path(value.path)/entry.path:
                raise ValueError('Directory artifact file path differs from its live root')


def snapshot(value):
    from .backend import BackendInfo
    from .results import ResourceSnapshot
    from ..schema.profiles import SwmmProfile
    from ..schema.option_profile import ResolvedOptions
    from ..model.units import UnitContext
    typed_tree(value)
    text(value.run_id); text(value.execution_directory)
    if value.contract != 'easysewer:run-snapshot:1':
        raise ValueError('Unknown run snapshot contract')
    if type(value.created_at) is not datetime:
        raise TypeError('Snapshot creation time must be a datetime')
    for name, kind in (('profile', SwmmProfile), ('units', UnitContext),
                       ('options', ResolvedOptions), ('backend', BackendInfo)):
        if type(getattr(value, name)) is not kind:
            raise TypeError('Invalid snapshot '+name)
    for name, hashed in (('model_json', 'model_sha256'), ('input_bytes', 'input_sha256')):
        raw = getattr(value, name)
        if type(raw) is not bytes or hashlib.sha256(raw).hexdigest() != getattr(value, hashed):
            raise ValueError('Snapshot bytes differ from '+hashed)
    for name in ('model_json', 'config_json', 'backend_settings'):
        raw = getattr(value, name)
        if raw is None and name == 'backend_settings':
            continue
        if type(raw) is not bytes:
            raise TypeError('Snapshot JSON must be immutable bytes')
        JsonDocument.from_bytes(raw)
    if type(value.resources) is not tuple or any(type(v) is not ResourceSnapshot for v in value.resources):
        raise TypeError('Snapshot resources must be immutable resource records')
    from ._directory_state import mutable_groups
    mutable_groups(value.resources)
    keys = [(v.owner.canonical, v.field) for v in value.resources]
    if len(set(keys)) != len(keys):
        raise ValueError('Duplicate captured resource')


def failure(value):
    from .backend import NativeFailure
    typed_tree(value)
    text(value.stage); text(value.exception_type)
    if type(value.message) is not str or type(value.stderr) is not str:
        raise TypeError('Failure messages and stderr must be strings')
    if value.native is not None and type(value.native) is not NativeFailure:
        raise TypeError('Expected a native failure')
    if type(value.cleanup) is not tuple or any(type(v) is not NativeFailure for v in value.cleanup):
        raise TypeError('Expected immutable native cleanup failures')
    if value.worker_returncode is not None and type(value.worker_returncode) is not int:
        raise TypeError('Expected an integer worker return code')


def produced(value):
    from .results import FileArtifact, RunSnapshot
    from .cache_reuse import CacheEvidence
    from ..results.applicability import ResultApplicability
    from ..io.hotstart_manifest import HotstartManifest
    from ..io.cache_manifest import CacheManifest
    if type(value.artifact) is not FileArtifact or type(value.producer) is not RunSnapshot:
        raise TypeError('Cache production requires artifact and snapshot records')
    if type(value.applicability) is not ResultApplicability:
        raise TypeError('Expected cache applicability')
    if type(value.manifest) not in (HotstartManifest, CacheManifest):
        raise TypeError('Unsupported produced cache manifest')
    kind = 'HOTSTART' if type(value.manifest) is HotstartManifest else value.manifest.kind
    if kind != value.kind or not value.artifact.complete:
        raise ValueError('Cache kind or artifact completion differs from its manifest')
    if (value.manifest.sha256 != value.artifact.sha256 or
        value.manifest.producer_input_sha256 != value.producer.input_sha256 or
        value.manifest.engine_sha256 != value.producer.backend.sha256):
        raise ValueError('Produced cache evidence differs from its artifact/snapshot')
    if value.reuse_evidence is not None:
        evidence = value.reuse_evidence
        if type(evidence) is not CacheEvidence:
            raise TypeError('Expected cache production-condition evidence')
        if (evidence.cache_sha256 != value.artifact.sha256 or evidence.context.kind != value.kind or
            evidence.context.input_sha256 != value.producer.input_sha256 or
            evidence.context.engine_sha256 != value.producer.backend.sha256):
            raise ValueError('Cache production conditions differ from its snapshot')


def consumed(value):
    from .cache_reuse import CacheReuse
    text(value.kind); text(value.verification); text(value.producer_run_id, optional=True)
    for name in ('sha256', 'producer_input_sha256', 'producer_model_sha256',
                 'producer_engine_sha256', 'consumer_model_sha256'):
        digest(getattr(value, name))
    if value.sha256 is None or value.consumer_model_sha256 is None or type(value.reuse) is not CacheReuse:
        raise ValueError('Cache consumption requires digests and typed reuse evidence')


def continuation(value):
    from .config import RunConfig
    typed_tree(value)
    text(value.attempt_id);text(value.execution_run_id)
    for name in ('checkpoint_sha256','state_sha256','execution_sha256'):
        hashed=getattr(value,name);digest(hashed)
        if hashed is None:raise ValueError('Continuation requires complete checkpoint identity')
    if (type(value.started_at) is not datetime or value.started_at.tzinfo is None or
        value.started_at.utcoffset() is None):
        raise ValueError('Continuation requires a timezone-aware start time')
    if value.simulation_seconds<0 or value.steps<0:
        raise ValueError('Continuation progress cannot be negative')
    RunConfig.from_json_document(JsonDocument.from_bytes(value.config_json))


def continuation_chain(values, snapshot=None):
    from .results import RunContinuation
    from .config import RunConfig
    if type(values) is not tuple or any(type(v) is not RunContinuation for v in values):
        raise TypeError('Continuation history requires immutable RunContinuation records')
    if len({v.attempt_id for v in values})!=len(values):raise ValueError('Duplicate resume attempt identity')
    if len({(v.execution_run_id,v.execution_sha256) for v in values})>1:
        raise ValueError('Continuation history mixes execution identities')
    if snapshot is None:
        if values:raise ValueError('Continuation history requires its original snapshot')
        return
    original=RunConfig.from_json_document(JsonDocument.from_bytes(snapshot.config_json)) if values else None
    # These settings govern this attempt; all model/backend execution settings
    # remain bound to the original snapshot and cannot silently change on resume.
    operational={'output_directory','input_directory','artifact_stem','overwrite','keep_failed_artifacts',
        'progress_interval','cancellation_poll_interval','wall_time_limit','native_call_timeout',
        'step_batch_size','file_inspection_limit','report_read','_source'}
    for value in values:
        if value.execution_run_id!=snapshot.run_id or value.simulation_seconds>snapshot.options.duration.total_seconds():
            raise ValueError('Continuation does not belong to this execution')
        settings=RunConfig.from_json_document(JsonDocument.from_bytes(value.config_json))
        if any(getattr(original,f.name)!=getattr(settings,f.name) for f in fields(RunConfig) if f.name not in operational):
            raise ValueError('Continuation changes simulation execution settings')


def result(value):
    from .results import FileArtifact, DirectoryArtifact, DirectoryGroupArtifact, RunSnapshot, RunFailure, ProducedCache, CacheConsumption
    from .backend import BackendInfo, EngineObjects, MassBalance
    from ..validation import ValidationReport
    from ..io.output_metadata import OutputMetadata
    from ..io.report_document import ReportDocument, ReportCapture, _matches_source
    from ..results.tables import ResultTable
    typed_tree(value)
    text(value.run_id); text(value.retained_directory, optional=True)
    if value.status not in ('succeeded', 'rejected', 'failed', 'cancelled', 'timed_out'):
        raise ValueError('Unknown run result status')
    if type(value.native_completed) is not bool or type(value.diagnostics) is not ValidationReport:
        raise TypeError('Invalid run completion flag or diagnostics')
    for name, kind in (('snapshot', RunSnapshot), ('backend', BackendInfo), ('engine_objects', EngineObjects),
                       ('mass_balance', MassBalance), ('failure', RunFailure), ('output_metadata', OutputMetadata),
                       ('report_document', ReportDocument), ('failure_report', ReportCapture), ('backend_results', JsonDocument)):
        child = getattr(value, name)
        if child is not None and type(child) is not kind:
            raise TypeError('Invalid run result '+name)
    for name, kind in (('artifacts', FileArtifact), ('directory_artifacts', DirectoryArtifact), ('directory_group_artifacts', DirectoryGroupArtifact), ('produced_caches', ProducedCache),
                       ('consumed_caches', CacheConsumption), ('report_tables', ResultTable)):
        children = getattr(value, name)
        if type(children) is not tuple or any(type(v) is not kind for v in children):
            raise TypeError('Invalid immutable result '+name)
    all_artifacts=(*value.artifacts,*value.directory_artifacts,*(a.artifact for a in value.directory_group_artifacts))
    keys = [(a.role, a.owner.canonical if a.owner else None, a.field) for a in all_artifacts]
    if len(set(keys)) != len(keys):
        raise ValueError('Duplicate result artifact identity')
    if len({t.key for t in value.report_tables}) != len(value.report_tables):
        raise ValueError('Duplicate requested report table')
    if value.snapshot is not None and (value.snapshot.run_id != value.run_id or value.snapshot.backend != value.backend):
        raise ValueError('Result snapshot identity/backend mismatch')
    from ._directory_graph import resource_groups
    groups=resource_groups(value.snapshot.resources) if value.snapshot else {}
    graph_artifacts={}
    for artifact in value.directory_group_artifacts:
        if groups.get(artifact.group.key)!=artifact.group:raise ValueError('Directory group artifact lacks matching snapshot evidence')
        key=(artifact.group.key,artifact.current)
        if key in graph_artifacts:raise ValueError('Duplicate directory group artifact')
        graph_artifacts[key]=artifact
    grouped={(r.owner.canonical,r.field) for r in value.snapshot.resources if r.directory_group is not None} if value.snapshot else set()
    if any(a.owner is not None and (a.owner.canonical,a.field) in grouped and a.role in ('run:resource','run:resource_state') for a in (*value.directory_artifacts,*value.artifacts)):
        raise ValueError('Grouped consumers require complete group artifacts to preserve shared views')
    states = {(r.owner.canonical,r.field) for r in value.snapshot.resources if r.initial_relative_path is not None} if value.snapshot else set()
    for artifact in value.directory_artifacts:
        if artifact.role == 'run:resource_state' and (artifact.owner.canonical,artifact.field) not in states:
            raise ValueError('Directory state artifact lacks a mutable resource declaration')
    continuation_chain(value.continuations,value.snapshot)
    documents = (value.report_document, value.failure_report.document if value.failure_report else None)
    for document in documents:
        if document is not None:
            if not _matches_source(document):
                raise ValueError('Captured report decoding differs from its raw bytes')
    if value.mass_balance is not None and value.mass_balance.raw_percentages:
        if value.mass_balance.with_context(value.result_context) != value.mass_balance:
            raise ValueError('Continuity values contradict their applicability/raw observations')
    for cache in value.produced_caches:
        if cache.producer != value.snapshot or cache.artifact not in value.artifacts:
            raise ValueError('Produced cache is not owned by this result')
    for cache in value.consumed_caches:
        if value.snapshot is None or cache.consumer_model_sha256 != value.snapshot.model_sha256:
            raise ValueError('Consumed cache identifies a different model')
    if value.succeeded:
        if (not value.native_completed or value.failure is not None or value.failure_report is not None or
            value.snapshot is None or value.backend is None or value.engine_objects is None or
            value.mass_balance is None or value.output_metadata is None or value.report_document is None or
            not value.diagnostics.is_valid or any(not a.complete for a in all_artifacts)):
            raise ValueError('Successful result lacks complete execution evidence')
        for role in ('run:input', 'run:report', 'run:output'):
            value.artifact(role)
        if (value.input.sha256 != value.snapshot.input_sha256 or value.input.size != len(value.snapshot.input_bytes) or
            value.report.sha256 != hashlib.sha256(value.report_document.raw).hexdigest() or
            value.report.size != len(value.report_document.raw)):
            raise ValueError('Captured input/report differs from its artifact')
        if value.output_metadata.result_context != value.result_context:
            raise ValueError('OUT context differs from the captured execution')
        metadata = value.output_metadata
        if (metadata.flow_units != value.snapshot.units.flow_units or metadata.engine_version != value.backend.engine_version or
            metadata.producer != value.backend.key or metadata.semantics != value.backend.output_semantics):
            raise ValueError('OUT metadata differs from the captured backend/units')
        for table in value.report_tables:
            source = table.source
            if (source.sha256 != value.report.sha256 or source.run_id != value.run_id or
                source.input_sha256 != value.snapshot.input_sha256 or source.backend_sha256 != value.backend.sha256):
                raise ValueError('Captured table source differs from this run')
        for group in groups.values():
            required={(group.key,False)} | ({(group.key,True)} if group.initial_relative_path is not None else set())
            if not required <= set(graph_artifacts):raise ValueError('Successful result lacks initial/current directory group artifacts')
        mutable_states = {}
        for resource in value.snapshot.resources:
            if resource.directory_group is not None:continue
            if resource.tree is not None or resource.initial_relative_path is not None:
                captured = [a for a in value.directory_artifacts if a.owner == resource.owner and
                            a.field == resource.field and a.role == 'run:resource']
                if len(captured) != 1 or captured[0].manifest != resource.tree:
                    raise ValueError('Captured directory differs from its resource snapshot')
                if resource.initial_relative_path is not None:
                    current = [a for a in value.directory_artifacts if a.owner == resource.owner and
                               a.field == resource.field and a.role == 'run:resource_state']
                    if len(current) != 1:
                        raise ValueError('Mutable directory lacks its final state artifact')
                    if mutable_states.setdefault(resource.relative_path, current[0].manifest) != current[0].manifest:
                        raise ValueError('Shared mutable directory final artifacts disagree')
            if resource.sha256 is None:
                continue
            captured = [a for a in value.artifacts if a.owner == resource.owner and a.field == resource.field
                        and a.role == ('run:resource' if resource.access != 'write' else resource.role)]
            if len(captured) != 1 or (captured[0].sha256, captured[0].size) != (resource.sha256, resource.size):
                raise ValueError('Captured resource differs from its artifact')
    else:
        if value.failure is None and value.diagnostics.is_valid:
            raise ValueError('Unsuccessful result requires a failure or error diagnostic')
        if (value.produced_caches or value.report_tables or value.report_document is not None or
            value.output_metadata is not None or any(a.complete for a in all_artifacts)):
            raise ValueError('Unsuccessful result cannot publish successful artifacts or tables')
