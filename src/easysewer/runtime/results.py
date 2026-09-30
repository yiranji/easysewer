"""Immutable run ownership, snapshots and operational results (no native IO)."""

from dataclasses import dataclass
from datetime import datetime
import hashlib
from pathlib import Path

from .backend import BackendInfo, EngineObjects, MassBalance, NativeFailure
from ._directory_tree import DirectoryLimits, DirectoryManifest
from ._directory_graph import DirectoryGroupSnapshot, DirectoryGraphState
from ..model.identity import Ref
from ..model.units import UnitContext
from ..schema.option_profile import ResolvedOptions
from ..schema.profiles import SwmmProfile
from ..validation import ValidationReport
from ..results.applicability import ResultApplicability, ResultContext
from .cache_reuse import CacheEvidence, CacheReuse
from ..validation._cooperative import _active, checkpoint_scope, checkpoint as work_checkpoint, checkpointed


@dataclass(frozen=True, kw_only=True)
class ResourceSnapshot:
    owner: Ref
    field: tuple[str | int, ...]
    role: str
    format: str
    kind: str
    access: str
    active: bool
    required: bool
    original_path: str | None
    relative_path: str | None
    sha256: str | None
    size: int | None
    tree: DirectoryManifest | None = None
    initial_relative_path: str | None = None
    directory_group: DirectoryGroupSnapshot | None = None

    def __post_init__(self):
        from ._result_contract import resource
        resource(self)


@dataclass(frozen=True, kw_only=True)
class RunSnapshot:
    run_id: str
    created_at: datetime
    model_json: bytes
    config_json: bytes
    model_sha256: str
    input_bytes: bytes
    input_sha256: str
    profile: SwmmProfile
    units: UnitContext
    options: ResolvedOptions
    backend: BackendInfo
    resources: tuple[ResourceSnapshot, ...]
    execution_directory: str
    contract: str = 'easysewer:run-snapshot:1'
    backend_settings: bytes | None = None

    def __post_init__(self):
        from ._result_contract import snapshot
        snapshot(self)

    def model(self, *, schema=None):
        from ..model import Model
        from ..io.json import JsonDocument
        return Model.from_json_document(JsonDocument.from_bytes(self.model_json), schema=schema, profile=self.profile, strict=True)


@dataclass(frozen=True, kw_only=True)
class FileArtifact:
    role: str
    path: str
    sha256: str
    size: int
    complete: bool
    owner: Ref | None = None
    field: tuple[str | int, ...] = ()
    declared_path: str | None = None

    def __post_init__(self):
        from ._result_contract import artifact
        artifact(self)

    def read_bytes(self, *, checkpoint=None):
        with checkpoint_scope(checkpoint):
            if _active.get() is None:
                data = Path(self.path).read_bytes()
                digest = hashlib.sha256(data).hexdigest()
            else:
                chunks = []; state = hashlib.sha256()
                with Path(self.path).open('rb') as stream:
                    while True:
                        work_checkpoint();chunk = stream.read(64*1024)
                        if not chunk:break
                        state.update(chunk);chunks.append(chunk)
                work_checkpoint();data = b''.join(chunks);digest = state.hexdigest()
            if len(data) != self.size or digest != self.sha256:
                raise ValueError(f'Artifact bytes changed since this run: {self.path}')
            return data


@dataclass(frozen=True, kw_only=True)
class DirectoryArtifact:
    """Complete member evidence; loaded archives address files through blobs.

    `path` is the live tree, or None after archive relocation. `original_path`
    remains provenance. Files can always be queried by relative member name;
    materialize explicitly reconstructs a new tree, including empty directories.
    """
    role: str
    path: str | None
    original_path: str
    manifest: DirectoryManifest | None
    files: tuple[tuple[str, FileArtifact], ...]
    complete: bool
    owner: Ref
    field: tuple[str | int, ...]
    declared_path: str | None = None

    def __post_init__(self):
        from ._result_contract import directory_artifact
        directory_artifact(self)

    @classmethod
    def from_path(cls, path, *, role, owner, field, complete, declared_path=None,
                  limits=DirectoryLimits(), checkpoint=None, allow_absent=False):
        from ._directory_state import observe
        with checkpoint_scope(checkpoint):
            root = Path(path).absolute()
            manifest = observe(root, limits=limits, allow_absent=allow_absent)
            files = tuple((entry.path, FileArtifact(role=role, path=str(root/entry.path),
                sha256=entry.sha256, size=entry.size, complete=complete, owner=owner,
                field=field, declared_path=declared_path))
                for entry in checkpointed(manifest.entries if manifest is not None else ()) if entry.kind == 'file')
            return cls(role=role, path=str(root), original_path=str(root), manifest=manifest,
                files=files, complete=complete, owner=owner, field=field, declared_path=declared_path)

    def file(self, relative):
        from ._directory_tree import relative_path
        relative_path(relative)
        for name, artifact in checkpointed(self.files):
            if name == relative:
                return artifact
        raise KeyError('Directory member is not a captured file: '+relative)

    def _limits(self):
        return DirectoryLimits(total_bytes=max(1, self.manifest.total_bytes),
            entries=max(1, len(self.manifest.entries)),
            depth=max((len(e.path.split('/')) for e in self.manifest.entries), default=1))

    def verify(self, *, checkpoint=None):
        from ._directory_tree import verify_tree, _node, _stamp, _file_digest
        from ._directory_state import verify_absent
        with checkpoint_scope(checkpoint):
            if self.manifest is None:
                work_checkpoint()
                if self.path is not None:verify_absent(self.path)
                return self
            if self.path is not None:
                verify_tree(self.path, self.manifest, limits=self._limits())
            for name, artifact in checkpointed(self.files):
                path = Path(artifact.path); _, info = _node(path, 'file')
                if _file_digest(path, _stamp(info)) != (artifact.sha256, artifact.size):
                    raise ValueError('Directory member changed since capture: '+name)
            return self

    def materialize(self, directory, *, checkpoint=None):
        """Reconstruct a new tree; failed new captures remain at the given path.

        Never merge with or replace an existing target. The caller retains a
        partial new tree after interruption and chooses its retention/cleanup.
        """
        from dataclasses import replace
        from ._directory_tree import _node, verify_tree, link_member
        from ._workspace import copy_input
        with checkpoint_scope(checkpoint):
            target = Path(directory).absolute(); _node(target.parent, 'directory')
            if self.manifest is None:
                from ._directory_state import verify_absent
                self.verify(); work_checkpoint(); verify_absent(target)
                return replace(self,path=str(target))
            if self.path is not None:
                source = Path(self.path).resolve(); resolved = target.resolve()
                if source.is_relative_to(resolved) or resolved.is_relative_to(source):
                    raise ValueError('Directory materialization overlaps the live tree')
            self.verify(); work_checkpoint(); target.mkdir()
            identities = {'': (_node(target, 'directory')[1].st_dev, _node(target, 'directory')[1].st_ino)}
            files = []; by_name = dict(self.files); created_files = {}
            for entry in checkpointed(self.manifest.entries, interval=1):
                parts = entry.path.split('/')
                for count in range(len(parts)):
                    name = '/'.join(parts[:count]); parent = target.joinpath(*parts[:count]); _, info = _node(parent, 'directory')
                    if (info.st_dev, info.st_ino) != identities[name]:
                        raise ValueError('Directory materialization parent changed')
                destination = target/entry.path
                if entry.kind == 'directory':
                    destination.mkdir(); _, info = _node(destination, 'directory')
                    identities[entry.path] = info.st_dev, info.st_ino
                else:
                    artifact = by_name[entry.path]
                    if entry.hardlink_to is not None:
                        link_member(target, entry, expected_identity=created_files[entry.hardlink_to])
                    else:
                        created = []
                        if copy_input(Path(artifact.path), destination, checkpoint=work_checkpoint, on_create=created.append) != (entry.sha256, entry.size):
                            raise ValueError('Directory member changed while materializing')
                        created_files[entry.path] = created[0]
                    files.append((entry.path, replace(artifact, path=str(destination))))
            verify_tree(target, self.manifest, limits=self._limits())
            return replace(self, path=str(target), files=tuple(files))


@dataclass(frozen=True, kw_only=True)
class DirectoryGroupArtifact:
    group: DirectoryGroupSnapshot
    artifact: DirectoryArtifact
    current: bool

    def __post_init__(self):
        if type(self.group) is not DirectoryGroupSnapshot or type(self.artifact) is not DirectoryArtifact or type(self.current) is not bool:
            raise TypeError('Expected explicit directory group artifact')
        expected='run:resource_group_state' if self.current else 'run:resource_group'
        if (self.artifact.owner!=Ref(collection='run:directory-groups',key=self.group.key) or
                self.artifact.field!=() or self.artifact.role!=expected or self.artifact.manifest is None):
            raise ValueError('Directory group artifact identity differs')
        DirectoryGraphState(layout=self.group.state.layout,tree=self.artifact.manifest)
        if self.current:
            if self.group.initial_relative_path is None:raise ValueError('Readonly graph has no separate current artifact')
        elif self.artifact.manifest!=self.group.tree:raise ValueError('Initial group artifact differs from snapshot')

    @property
    def complete(self):return self.artifact.complete

    @classmethod
    def from_path(cls,group,path,*,current=False,complete=False,checkpoint=None):
        role='run:resource_group_state' if current else 'run:resource_group'
        artifact=DirectoryArtifact.from_path(path,role=role,owner=Ref(collection='run:directory-groups',key=group.key),field=(),complete=complete,limits=group.state.layout.limits,checkpoint=checkpoint)
        return cls(group=group,artifact=artifact,current=current)

    def materialize(self,directory,*,checkpoint=None):
        from dataclasses import replace
        return replace(self,artifact=self.artifact.materialize(directory,checkpoint=checkpoint))

    def view_path(self,owner,field):
        from ._directory_graph import consumer_key
        if self.artifact.path is None:raise ValueError('Materialize the complete directory group before accessing consumer paths')
        return Path(self.artifact.path)/self.group.state.layout.view(consumer_key(owner,field)).path


@dataclass(frozen=True, kw_only=True)
class RunProgress:
    run_id: str
    phase: str
    wall_seconds: float
    simulation_seconds: float
    fraction: float
    steps: int


@dataclass(frozen=True, kw_only=True)
class RunFailure:
    stage: str
    exception_type: str
    message: str
    native: NativeFailure | None = None
    cleanup: tuple[NativeFailure, ...] = ()
    stderr: str = ''
    worker_returncode: int | None = None

    def __post_init__(self):
        from ._result_contract import failure
        failure(self)


@dataclass(frozen=True, kw_only=True)
class ProducedCache:
    kind: str
    artifact: FileArtifact
    manifest: object
    producer: RunSnapshot
    applicability: ResultApplicability = ResultApplicability()
    reuse_evidence: CacheEvidence | None = None

    def __post_init__(self):
        from ._result_contract import produced
        produced(self)


@dataclass(frozen=True, kw_only=True)
class CacheConsumption:
    kind: str
    sha256: str
    producer_run_id: str | None
    producer_input_sha256: str | None
    producer_model_sha256: str | None
    producer_engine_sha256: str | None
    consumer_model_sha256: str
    verification: str
    reuse: CacheReuse = CacheReuse()

    def __post_init__(self):
        from ._result_contract import consumed
        consumed(self)


@dataclass(frozen=True, kw_only=True)
class RunContinuation:
    """One verified resume attempt, independent of the original execution ID."""
    attempt_id: str
    execution_run_id: str
    checkpoint_sha256: str
    state_sha256: str
    execution_sha256: str
    started_at: datetime
    simulation_seconds: float
    steps: int
    config_json: bytes

    def __post_init__(self):
        from ._result_contract import continuation
        continuation(self)


@dataclass(frozen=True, kw_only=True)
class RunResult:
    run_id: str
    status: str
    diagnostics: ValidationReport
    artifacts: tuple[FileArtifact, ...] = ()
    snapshot: RunSnapshot | None = None
    backend: BackendInfo | None = None
    engine_objects: EngineObjects | None = None
    mass_balance: MassBalance | None = None
    failure: RunFailure | None = None
    native_completed: bool = False
    produced_caches: tuple[ProducedCache, ...] = ()
    consumed_caches: tuple[CacheConsumption, ...] = ()
    output_metadata: object | None = None
    report_document: object | None = None
    failure_report: object | None = None
    report_tables: tuple = ()
    retained_directory: str | None = None
    backend_results: object | None = None
    continuations: tuple[RunContinuation, ...] = ()
    directory_artifacts: tuple[DirectoryArtifact, ...] = ()
    directory_group_artifacts: tuple[DirectoryGroupArtifact, ...] = ()

    def __post_init__(self):
        from ._result_contract import result
        result(self)

    def save(self, directory, *, max_bytes=8*1024**3, max_manifest_bytes=64*1024**2):
        """Save all registered artifacts and typed evidence to a new directory."""
        from .archive import save_result
        return save_result(self, directory, max_bytes=max_bytes, max_manifest_bytes=max_manifest_bytes)

    @classmethod
    def load(cls, directory, *, max_bytes=8*1024**3, max_manifest_bytes=64*1024**2):
        """Verify a movable result archive without loading or running native code."""
        from .archive import load_result
        return load_result(directory, max_bytes=max_bytes, max_manifest_bytes=max_manifest_bytes)

    @property
    def result_context(self):
        return self.mass_balance.result_context if self.mass_balance else ResultContext()

    @property
    def succeeded(self):
        return self.status == 'succeeded'

    def artifact(self, role, *, owner=None, field=None):
        matches = [artifact for artifact in self.artifacts if artifact.role == role
                   and (owner is None or artifact.owner == owner) and (field is None or artifact.field == field)]
        if len(matches) != 1:
            raise KeyError(f'Expected one artifact for {role}, found {len(matches)}')
        return matches[0]

    def directory_artifact(self, role, *, owner=None, field=None):
        matches = [a for a in self.directory_artifacts if a.role == role
                   and (owner is None or a.owner == owner) and (field is None or a.field == field)]
        if len(matches) != 1:
            raise KeyError(f'Expected one directory artifact for {role}, found {len(matches)}')
        return matches[0]

    @property
    def input(self):
        return self.artifact('run:input')

    @property
    def report(self):
        return self.artifact('run:report')

    @property
    def output(self):
        return self.artifact('run:output')

    def raise_for_status(self):
        if not self.succeeded:
            raise RunError(self)
        return self

    def open_output(self, *, variables=None, max_buffer_bytes=1024*1024,
                    max_series_values=1_000_000, invalid_values='raise'):
        """Verify and open this run's OUT artifact; use as a context manager."""
        self.raise_for_status()
        if not self.output.complete or self.output_metadata is None:
            raise ValueError('This run has no verified complete OUT')
        from ..io.output import OutputReader
        return OutputReader(self.output.path,metadata=self.output_metadata,
            expected_sha256=self.output.sha256,expected_size=self.output.size,run_id=self.run_id,
            input_sha256=self.snapshot.input_sha256 if self.snapshot else None,
            backend_sha256=self.backend.sha256 if self.backend else None,variables=variables,
            max_buffer_bytes=max_buffer_bytes,max_series_values=max_series_values,invalid_values=invalid_values)

    def report_table(self, key):
        """Return a requested, captured table, including its explicit missing status."""
        self.raise_for_status()
        for table in self.report_tables:
            if table.key == key:return table
        raise KeyError('Report table was not requested: '+str(key))

    def report_reader(self, *, registry=None, on_error='preserve'):
        """Query the report bytes captured by this run; no path is reopened."""
        self.raise_for_status()
        if self.report_document is None or not self.report.complete:
            raise ValueError('No complete captured report')
        from ..io.report import ReportReader, ReportContext
        from ..io.inp import InpDocument
        from ..results import ResultSource
        if len(self.report_document.raw)!=self.report.size:raise ValueError('Captured report size differs from its artifact')
        context=ReportContext(producer=self.backend.key if self.backend else None,
            result_context=self.result_context,
            numerical_policy=self.backend.numerical_policy if self.backend else None,
            accounting=dict(self.backend.output_semantics).get('easysewer:ponding-accounting') if self.backend else None,
            report_start=self.snapshot.options.report_start if self.snapshot else None,
            averages=self.output_metadata.averages if self.output_metadata else None,
            flow_units=self.snapshot.units.flow_units if self.snapshot else None,
            rain_gage_sources=tuple((line.values[0],line.values[4].upper()) for line in InpDocument.from_bytes(self.snapshot.input_bytes).records('RAINGAGES')) if self.snapshot else None,
            pollutants=tuple(line.values[0] for line in InpDocument.from_bytes(self.snapshot.input_bytes).records('POLLUTANTS')) if self.snapshot else None)
        source=ResultSource(format='swmm:rpt',path=self.report.path,sha256=self.report.sha256,run_id=self.run_id,
            encoding=self.report_document.encoding,input_sha256=self.snapshot.input_sha256 if self.snapshot else None,
            backend_sha256=self.backend.sha256 if self.backend else None,engine_version=self.backend.engine_version if self.backend else None)
        return ReportReader(self.report_document,registry=registry,source=source,context=context,on_error=on_error)

    def read_lid_report(self, owner, *, schema=None, encoding=None, max_bytes=64*1024*1024,
                        on_decode_error='preserve', on_error='preserve'):
        """Verify an archived LID detail file against the captured deployment."""
        self.raise_for_status()
        if not isinstance(owner,Ref) or owner.collection!='swmm:lid_usage':
            raise TypeError('Expected a LID deployment Ref')
        artifact=self.artifact('swmm:lid-detail',owner=owner)
        if not artifact.complete or self.snapshot is None:raise ValueError('No complete captured LID output')
        use=self.snapshot.model(schema=schema).lid_usage[owner.key]
        from ..io.report_details import read_lid_report
        return read_lid_report(artifact.path,encoding=encoding,max_bytes=max_bytes,on_decode_error=on_decode_error,on_error=on_error,
            result_context=self.result_context,
            owner=artifact.owner,expected_subcatchment=use.subcatchment.key,expected_control=use.control.key,
            expected_sha256=artifact.sha256,expected_size=artifact.size,run_id=self.run_id,input_sha256=self.snapshot.input_sha256,
            backend_sha256=self.backend.sha256 if self.backend else None,engine_version=self.backend.engine_version if self.backend else None)


class RunError(RuntimeError):
    def __init__(self, result):
        self.result = result
        message = result.failure.message if result.failure else '; '.join(issue.message for issue in result.diagnostics.errors)
        super().__init__(f'Run {result.run_id} {result.status}: {message}')
