"""Explicit backend protocol and immutable native execution metadata.

Importing this module performs no discovery, native loading or process creation.
"""

from dataclasses import dataclass, replace
import math
from typing import Protocol, runtime_checkable

from ..model.identity import Ref, canonical_key
from ..io.json import JsonDocument
from ..validation import Diagnostic
from ..results.applicability import ResultContext


@dataclass(frozen=True, kw_only=True)
class BackendInfo:
    key: str
    available: bool
    reason: str | None
    library: str | None
    sha256: str | None
    engine_version: int | None
    platform: str
    architecture: str
    abi: str
    profiles: tuple[str, ...]
    capabilities: tuple[str, ...]
    isolation: str
    origin: str = 'user-library'
    implementation: str = 'easysewer:process-session:1'
    numerical_policy: str = 'epa-swmm:5.2.4'
    output_semantics: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, kw_only=True)
class BackendArtifact:
    """A backend-owned output below the Runner's unique asset directory."""
    role: str
    relative_path: str


@dataclass(frozen=True, kw_only=True)
class BackendRunPlan:
    """JSON execution parameters and declared products of a prepared backend."""
    parameters: JsonDocument
    artifacts: tuple[BackendArtifact, ...] = ()
    diagnostics: tuple[Diagnostic, ...] = ()


@dataclass(frozen=True, kw_only=True)
class NativeFailure:
    stage: str
    code: int | None
    message: str


class SessionError(RuntimeError):
    """The primary failure survives subsequent cleanup errors."""
    def __init__(self, failure, *, cleanup=(), stderr='', returncode=None):
        self.failure = failure
        self.cleanup = tuple(cleanup)
        self.stderr = stderr
        self.returncode = returncode
        super().__init__(f'{failure.stage}: {failure.message}' + (f' (native {failure.code})' if failure.code is not None else ''))


class SessionTimeout(SessionError):
    pass


class SessionCancelled(SessionError):
    pass


class CheckpointRejected(SessionError):
    """No restoration committed; the running checkpoint session remains usable."""
    checkpoint_committed = False


class SessionStateError(RuntimeError):
    pass


@dataclass(frozen=True, kw_only=True)
class EngineObjects:
    """Names queried from this loaded engine, never inferred Model positions."""
    groups: tuple[tuple[str, tuple[str, ...]], ...]

    def __post_init__(self):
        if type(self.groups) is not tuple:
            raise TypeError('Engine groups must be immutable tuples')
        collections = set()
        for group in self.groups:
            if type(group) is not tuple or len(group) != 2:
                raise TypeError('Expected (collection, names) tuples')
            collection, names = group
            if collection in collections or type(names) is not tuple:
                raise ValueError('Invalid native identity groups')
            collections.add(collection)
            if any(not isinstance(name, str) for name in names):
                raise ValueError('Native identities require string names')
            if len({canonical_key(name) for name in names}) != len(names):
                raise ValueError('Duplicate native identities')

    def names(self, collection):
        try:
            return dict(self.groups)[collection]
        except KeyError:
            raise ValueError(f'The native name API does not expose {collection}') from None

    def index(self, target: Ref):
        key = canonical_key(target.key)
        for index, name in enumerate(self.names(target.collection)):
            if canonical_key(name) == key:
                return index
        raise KeyError(target)

    def verify(self, expected):
        """Check identities as sets; native order is authoritative."""
        for collection, names in expected:
            names = tuple(names)
            keys = {canonical_key(name) for name in names}
            if len(keys) != len(names) or keys != {canonical_key(name) for name in self.names(collection)}:
                raise ValueError(f'Loaded native identities do not match the snapshot: {collection}')


@dataclass(frozen=True, kw_only=True)
class StepResult:
    elapsed_days: float
    finished: bool
    steps: int = 1


@dataclass(frozen=True, kw_only=True)
class MassBalance:
    runoff_percent: float | None
    flow_percent: float | None
    quality_percent: float | None
    raw_percentages: tuple[float, ...] = ()
    result_context: ResultContext = ResultContext()

    def __post_init__(self):
        if not isinstance(self.result_context,ResultContext):raise TypeError('Expected execution result context')
        values=(self.runoff_percent,self.flow_percent,self.quality_percent)
        if any(v is not None and (type(v) not in (int,float) or not math.isfinite(v)) for v in values):
            raise ValueError('Invalid continuity value')
        if type(self.raw_percentages) is not tuple or (self.raw_percentages and
            (len(self.raw_percentages)!=3 or any(type(v) not in (int,float) or not math.isfinite(v) for v in self.raw_percentages))):
            raise ValueError('Raw native continuity requires three finite numbers')

    def applicability(self, key):
        return self.result_context.balance(key)

    def with_context(self, context):
        raw=self.raw_percentages or (self.runoff_percent,self.flow_percent,self.quality_percent)
        values={key+'_percent':None if context.balance(key).unavailable else value
                for key,value in zip(('runoff','flow','quality'),raw)}
        return replace(self,**values,raw_percentages=raw,result_context=context)

    def to_data(self):
        return dict(runoff_percent=self.runoff_percent,flow_percent=self.flow_percent,
            quality_percent=self.quality_percent,raw_percentages=list(self.raw_percentages),
            result_context=self.result_context.to_data())

    @classmethod
    def from_data(cls, data):
        legacy={'runoff_percent','flow_percent','quality_percent'}
        if type(data) is not dict or set(data) not in (legacy, legacy | {'raw_percentages','result_context'}):
            raise ValueError('Invalid continuity result fields')
        if set(data)==legacy:return cls(**data)
        if type(data['raw_percentages']) is not list:raise TypeError('Expected raw continuity array')
        return cls(**{k:data[k] for k in legacy},raw_percentages=tuple(data['raw_percentages']),
                   result_context=ResultContext.from_data(data['result_context']))


@runtime_checkable
class Session(Protocol):
    info: BackendInfo
    state: str
    objects: EngineObjects | None

    def open(self, input, report, output, *, expected=(), overwrite=False): ...
    def start(self, *, save_results=True): ...
    def step(self, *, max_steps=1) -> StepResult: ...
    def end(self) -> MassBalance: ...
    def report(self): ...
    def close(self): ...


@runtime_checkable
class Backend(Protocol):
    key: str

    def probe(self, *, call_timeout=30, cancel_event=None, poll_interval=.05) -> BackendInfo: ...
    def session(self, *, working_directory, **options) -> Session: ...


@runtime_checkable
class CheckpointSession(Session, Protocol):
    """Optional session contract; ordinary third-party sessions need not implement it.

    open_checkpoint owns open/configure/start together from an execution snapshot.
    After a restore, checkpoint_outputs identifies the current files to collect
    after end/report/close. No output publication is performed by this protocol.
    """
    checkpoint_outputs: tuple

    def open_checkpoint(self, snapshot, *, expected=()) -> EngineObjects: ...
    def save_checkpoint(self, directory): ...
    def restore_checkpoint(self, checkpoint): ...
