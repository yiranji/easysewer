"""Private serialized lifecycle for checkpoint-capable worker solvers.

Construct before open. All solver mutations must go through this owner; the
worker must not mix these calls with direct native calls. Public RPC integration
is separate. Input stamps detect changes between boundaries, while capture also
verifies complete content against the startup ledger.
"""
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
import tempfile
import threading

from ._checkpoint_container import Builder, Checkpoint, Limits, _regular, _snapshot_files, _stream, execution_digest, execution_layout, load
from ._checkpoint_native import NativeCheckpoint, RestoreResult
from . import _checkpoint_worker as worker
from ._native_flexible import NativeFlexibleSolver
from ._workspace import fingerprint, remove_owned_tree
from .backend import NativeFailure
from .results import RunSnapshot
from ..io.json import JsonDocument


@dataclass(frozen=True)
class Input:
    path: Path
    sha256: str
    size: int
    stamp: tuple

    @classmethod
    def read(cls, path, *, expected=None, checkpoint=lambda: None):
        path = Path(path).absolute()
        before = fingerprint(path)
        size = _regular(path).st_size
        digest = _stream(path, size, checkpoint=checkpoint)
        if fingerprint(path) != before:
            raise ValueError('Checkpoint input changed during startup verification')
        if expected is not None and expected != dict(sha256=digest, size=size):
            raise ValueError('Checkpoint input differs from the execution snapshot')
        return cls(path, digest, size, before)

    @property
    def descriptor(self):
        return dict(sha256=self.sha256, size=self.size)

    def check(self):
        _regular(self.path)
        if fingerprint(self.path) != self.stamp:
            raise ValueError('Checkpoint input changed since its verified boundary')


@dataclass(frozen=True)
class RestoreOutcome:
    native: RestoreResult
    cleanup: tuple[NativeFailure, ...]


class RestoreRejected(RuntimeError):
    def __init__(self, result):
        self.result = result
        super().__init__(f'Checkpoint restoration rejected ({result.error})')


class CheckpointLifecycle:
    def __init__(self, solver, snapshot, *, limits=Limits(), schema=None, _declarations=None):
        if type(snapshot) is not RunSnapshot or type(limits) is not Limits:
            raise TypeError('Checkpoint lifecycle requires RunSnapshot and Limits')
        if solver.open_attempted or solver.start_attempted or solver.closed:
            raise ValueError('Checkpoint lifecycle must own the solver before open')
        for key in ('sha256', 'engine_version', 'platform', 'architecture', 'abi'):
            if solver.metadata[key] != getattr(snapshot.backend, key):
                raise ValueError('Checkpoint snapshot differs from the actual engine')
        self.custom = isinstance(solver, NativeFlexibleSolver)
        if snapshot.backend.key != ('easysewer:flexible-ponding' if self.custom else 'swmm:standard'):
            raise ValueError('Checkpoint snapshot backend family mismatch')
        self.solver, self.snapshot, self.limits = solver, snapshot, limits
        self.root = Path(snapshot.execution_directory).resolve(strict=True)
        self.lock = threading.Lock()
        self.state = 'NEW'
        self.originals = {}; self.inputs = {}; self.owned = []
        self.api = None
        self.schema = schema
        self.declarations = _declarations
        self.directory_inputs = None
        self.output_locations = None

    @contextmanager
    def _operation(self, *states):
        if not self.lock.acquire(blocking=False):
            raise ValueError('Checkpoint solver operation is already in progress')
        try:
            if self.state not in states:
                raise ValueError(f'Checkpoint operation is invalid in {self.state}')
            if Path.cwd().resolve() != self.root:
                raise ValueError('Checkpoint solver working directory changed')
            yield
        finally:
            self.lock.release()

    def _check_inputs(self):
        if self.directory_inputs is not None:self.directory_inputs.check()
        for item in (*self.originals.values(), *self.inputs.values()):
            item.check()

    def open_start(self, *, save_results=True, checkpoint=lambda: None, cleanup_on_error=True):
        with self._operation('NEW'):
            if type(cleanup_on_error) is not bool: raise TypeError('cleanup_on_error must be bool')
            try:
                _, directories, outputs = execution_layout(self.snapshot, self.root, schema=self.schema, _declarations=self.declarations)
                for name in outputs:
                    path=self.root/name
                    if path.exists() or path.is_symlink():
                        raise FileExistsError('Checkpoint startup refuses existing output: '+str(path))
                for path in [*(self.root/name for name in outputs), *(self.root/name for name in directories)]:
                    if not path.resolve().is_relative_to(self.root):
                        raise ValueError('Checkpoint output escaped its execution directory')
                from ._checkpoint_directory_inputs import DirectoryInputs
                self.directory_inputs = DirectoryInputs.read(self.snapshot,checkpoint=checkpoint)
                declared = _snapshot_files(self.snapshot)
                declared['model.inp'] = dict(sha256=self.snapshot.input_sha256, size=len(self.snapshot.input_bytes))
                for name, desc in declared.items():
                    if self.directory_inputs is not None and name in self.directory_inputs.files:continue
                    path = self.root/name
                    if not path.resolve().is_relative_to(self.root):
                        raise ValueError('Checkpoint input escaped its execution directory')
                    self.originals[name] = Input.read(path, expected=desc, checkpoint=checkpoint)
                self.objects = self.solver.open(['model.inp', 'model.rpt', 'model.out'])
                if self.custom:
                    self.solver.configure(JsonDocument.from_bytes(self.snapshot.backend_settings).data)
                self.solver.start(save_results)
                self._check_inputs()
                probe = NativeCheckpoint(self.solver, b'\0'*32, max_state_bytes=self.limits.native_bytes)
                for item in probe.inputs():
                    if item.identity in self.inputs: raise ValueError('Duplicate native input identity')
                    self.inputs[item.identity] = Input.read(item.initial_path, checkpoint=checkpoint)
                rows = [dict(identity=k, blob=v.descriptor) for k,v in sorted(self.inputs.items())]
                self.binding = bytes.fromhex(execution_digest(self.snapshot, rows))
                self.api = NativeCheckpoint(self.solver, self.binding, max_state_bytes=self.limits.native_bytes)
                from ._checkpoint_directory_outputs import roots
                if roots(self.snapshot):
                    from ._checkpoint_context import output_inventory
                    self.output_locations=output_inventory(self.snapshot,self.api.outputs(),self.solver.trace.name if self.custom and self.solver.trace else None)
                self.state = 'STARTED'
                return self.objects
            except BaseException as error:
                self.state = 'FAILED'
                # The process worker must send the primary cause before any
                # cleanup that can block/crash. Standalone owners still clean
                # up here by default.
                if cleanup_on_error: error.checkpoint_cleanup = tuple(self.solver.cleanup())
                raise

    def step(self, max_steps=1):
        with self._operation('STARTED'):
            if type(max_steps) is not int or not 1 <= max_steps <= 100000:
                raise ValueError('Invalid checkpoint step batch size')
            try:
                self._check_inputs()
                result = self.solver.step(max_steps)
                self._check_inputs()
                if result['finished']: self.state = 'FINISHED'
                return result
            except BaseException:
                self.state = 'FAILED'
                raise

    def capture(self, destination, *, checkpoint=lambda: None):
        with self._operation('STARTED'):
            from ..validation._cooperative import checkpoint_scope
            with checkpoint_scope(checkpoint):
                checkpoint(); self._check_inputs()
            # Native capture is indivisible; check before and after that call,
            # and around worker/output flushes before creating archive files.
            checkpoint()
            native = self.api.capture()
            checkpoint()
            python = worker.capture(self.solver) if self.custom else None
            checkpoint()
            outputs = self.api.outputs()
            checkpoint()
            with Builder(destination, self.snapshot, limits=self.limits, checkpoint=checkpoint) as builder:
                for key, item in sorted(self.inputs.items()):
                    builder.add_input(key, item.path, expected=item.descriptor)
                if builder.binding != self.binding: raise ValueError('Checkpoint execution identity changed')
                self._check_inputs()
                return builder.finish(native, outputs, worker_state=python,
                    trace=self.solver.trace.name if self.custom and self.solver.trace else None,
                    output_locations=self.output_locations)

    def restore(self, archive, *, checkpoint=lambda: None):
        with self._operation('STARTED'):
            if type(archive) is not Checkpoint or archive.binding != self.binding:
                raise ValueError('Checkpoint does not belong to this execution')
            # The dataclass is an inspection value, not a validation token.
            # Revalidate persisted bindings and reject replaced in-memory
            # state fields before any native owner can commit.
            verified = load(archive.directory, limits=self.limits, checkpoint=checkpoint)
            if verified != archive:
                raise ValueError('Checkpoint value differs from its verified storage')
            self._check_inputs()
            self.api.validate(archive.native_state)
            data = archive.data
            from ._checkpoint_directory_outputs import decode, expected_bindings
            _,bindings=decode(data,self.snapshot)
            if self.output_locations is not None and bindings!=expected_bindings(self.snapshot,self.output_locations):
                raise ValueError('Checkpoint lacks matching output-directory stream bindings')
            if {item['identity'] for item in data['native_inputs']} != set(self.inputs):
                raise ValueError('Checkpoint input roles differ from startup')
            folder = Path(tempfile.mkdtemp(prefix='.checkpoint-restore-', dir=self.root))
            identity = fingerprint(folder)[:2]
            python = None; committed = False; cleanup = []; primary = None; directories = None
            try:
                inputs = {}
                for index, item in enumerate(data['native_inputs']):
                    path = archive.copy_blob(item['blob'], folder/('input-'+str(index)), checkpoint=checkpoint)
                    inputs[item['identity']] = Input.read(path, expected=item['blob'], checkpoint=checkpoint)
                current_outputs=self.api.outputs() if bindings is not None or self.custom else ()
                if bindings is not None and self.custom:worker.capture(self.solver)
                from ._checkpoint_directory_restore import DirectoryRestore
                directories=DirectoryRestore.prepare(self.snapshot,archive,folder,limits=self.limits,checkpoint=checkpoint)
                bound={item['index'] for item in bindings['outputs']} if bindings is not None else set()
                outputs={}
                for item in data['outputs']:
                    path=folder/('output-'+str(item['index']))
                    if item['index'] not in bound:archive.copy_blob(item['blob'],path,checkpoint=checkpoint)
                    outputs[item['index']]=path
                trace=folder/'trace' if data['trace'] else None
                if trace is not None and (bindings is None or bindings['trace'] is None):archive.copy_blob(data['trace'],trace,checkpoint=checkpoint)
                if bindings is not None:directories.bind_streams(bindings,outputs,trace,checkpoint=checkpoint)
                if self.custom:
                    python = worker.prepare(self.solver, archive.worker_state, trace_path=trace,
                        forbidden_files=[*outputs.values(), *(item.path for item in current_outputs)])
                def provide_input(key):
                    checkpoint(); inputs[key].check()
                    return inputs[key].path
                def provide_output(role, index, text, size):
                    checkpoint()
                    item = data['outputs'][index]
                    if (role, text, size) != (item['role'], item['text'], item['blob']['size']):
                        raise ValueError('Native output roles differ from checkpoint')
                    return outputs[index]
                self._check_inputs(); checkpoint()
                if directories:directories.check(checkpoint=checkpoint)
                result = self.api.restore(archive.native_state, input_provider=provide_input, output_provider=provide_output)
                committed = result.committed
                if not committed: raise RestoreRejected(result)
                # Native commit cannot be rolled back. Complete the prepared
                # Python commit even when retiring old native handles failed.
                self.inputs = inputs
                self.owned.append((folder, identity))
                if python:
                    python.apply()
                    # The retired Python trace can deny directory rename on
                    # Windows. Close it only after irreversible native commit.
                    if bindings is not None and bindings['trace'] is not None:
                        try:python.discard()
                        except BaseException as error:
                            cleanup.append(NativeFailure(stage='checkpoint_trace_cleanup',code=None,message=f'{type(error).__name__}: {error}'))
                if directories:
                    directories.apply()
                    cleanup.extend(directories.cleanup())
                if result.error: self.state = 'FAILED'
            except BaseException as error:
                primary = error
                error.checkpoint_committed = committed
                if committed: self.state = 'FAILED'
                raise
            finally:
                if python:
                    try: python.discard()
                    except BaseException as error:
                        cleanup.append(NativeFailure(stage='checkpoint_trace_cleanup', code=None, message=f'{type(error).__name__}: {error}'))
                if not committed:
                    try: remove_owned_tree(folder, parent=self.root, identity=identity)
                    except BaseException as error:
                        cleanup.append(NativeFailure(stage='checkpoint_workspace_cleanup', code=None, message=f'{type(error).__name__}: {error}'))
                if primary is not None: primary.checkpoint_cleanup = tuple(cleanup)
            return RestoreOutcome(result, tuple(cleanup))

    def end(self):
        with self._operation('STARTED', 'FINISHED'):
            self.state = 'FAILED'
            self._check_inputs()
            result = self.solver.end()
            self.state = 'ENDED'
            return result

    def report(self):
        with self._operation('ENDED'):
            self.state = 'FAILED'
            result = self.solver.report()
            self.state = 'REPORTED'
            return result

    def can_continue(self):
        """Conservative post-rejection check, called by the serialized worker."""
        if self.state != 'STARTED': return False
        try:
            self._check_inputs()
            self.api.inputs()  # Includes the native error/boundary guard.
            if self.custom: worker._boundary(self.solver)
            return True
        except Exception:
            self.state = 'FAILED'
            return False

    def cleanup(self, on_error=None):
        # Output paths remain until the owning Runner has copied/published
        # them. The Runner owns removal of the complete execution workspace.
        with self._operation('NEW', 'STARTED', 'FINISHED', 'ENDED', 'REPORTED', 'FAILED', 'CLOSED'):
            self.state = 'CLOSED'
            return self.solver.cleanup(on_error=on_error)
