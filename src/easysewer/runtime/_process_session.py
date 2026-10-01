"""Private subprocess transport and standard SWMM session implementation."""

import json
import math
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
import weakref
import uuid

from .backend import (CheckpointRejected, EngineObjects, MassBalance, NativeFailure, SessionCancelled,
                      SessionError, SessionStateError, SessionTimeout, StepResult)
from ..results.applicability import ResultContext, context_from_input

MAX_FRAME = 16 * 1024 * 1024
STDERR_LIMIT = 64 * 1024


# Retain the private spelling for existing internal integrations.
CheckpointRequestRejected = CheckpointRejected


def _worker_command():
    # -I ignores PYTHONPATH and the working directory. Explicitly use this
    # installation's package root, including when running from a src checkout.
    root = str(Path(__file__).resolve().parents[2])
    script = 'import sys; sys.path.insert(0, '+repr(root)+'); from easysewer.runtime._solver_worker import main; main()'
    return [sys.executable, '-I', '-B', '-u', '-c', script]


class _WorkerConnection:
    def __init__(self, directory):
        self._messages = queue.Queue(maxsize=2)
        self._writes = queue.Queue(maxsize=2)
        self._stopping = threading.Event()
        self._stderr = bytearray()
        self._stderr_lock = threading.Lock()
        self._sequence = 0
        self._disposed = False
        self.process = subprocess.Popen(_worker_command(), cwd=str(directory),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            bufsize=0, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self._threads = [threading.Thread(target=target, daemon=True, name='easysewer-worker-'+name)
                         for name, target in (('reader', self._read), ('writer', self._write), ('stderr', self._drain))]
        try:
            for thread in self._threads:
                thread.start()
        except BaseException:
            self.abort()
            raise

    def _put(self, value):
        while not self._stopping.is_set():
            try:
                self._messages.put(value, timeout=.05)
                return
            except queue.Full:
                pass

    def _read(self):
        try:
            while not self._stopping.is_set():
                line = self.process.stdout.readline(MAX_FRAME+1)
                if not line:
                    raise EOFError('Worker response pipe closed')
                if len(line)>MAX_FRAME or not line.endswith(b'\n'):
                    raise ValueError('Invalid or oversized worker response')
                self._put(json.loads(line))
        except BaseException as error:
            self._put(error)

    def _write(self):
        try:
            while not self._stopping.is_set():
                payload = self._writes.get()
                if payload is None:
                    return
                pending = memoryview(payload)
                while pending:
                    count = self.process.stdin.write(pending)
                    if not count:
                        raise BrokenPipeError('Worker request pipe closed')
                    pending = pending[count:]
        except BaseException as error:
            self._put(error)

    def _drain(self):
        try:
            while True:
                part = self.process.stderr.read(8192)
                if not part:
                    return
                with self._stderr_lock:
                    self._stderr.extend(part)
                    del self._stderr[:-STDERR_LIMIT]
        except (OSError, ValueError):
            pass

    @property
    def stderr(self):
        with self._stderr_lock:
            return bytes(self._stderr).decode('utf-8', errors='backslashreplace')

    def exchange(self, command, args, *, timeout, cancelled, poll_interval=.05):
        self._sequence += 1
        request_id = self._sequence
        payload = json.dumps(dict(version=1, id=request_id, command=command, args=args),
                             ensure_ascii=True, allow_nan=False, separators=(',', ':')).encode('ascii')+b'\n'
        if len(payload)>MAX_FRAME:
            raise ValueError('Worker request exceeds the explicit frame limit')
        deadline = time.monotonic()+timeout
        self._writes.put_nowait(payload)
        primary = None
        cleanup = []
        checkpoint_committed = None
        while True:
            try:
                if cancelled():
                    raise SessionCancelled(NativeFailure(stage=command, code=None, message='Session cancelled'))
                remaining = deadline-time.monotonic()
                if remaining<=0:
                    raise SessionTimeout(NativeFailure(stage=command, code=None, message='Native call timed out'))
                try:
                    response = self._messages.get(timeout=min(poll_interval, remaining))
                except queue.Empty:
                    continue
                if isinstance(response, BaseException):
                    raise response
                if not isinstance(response, dict) or response.get('id')!=request_id:
                    raise ValueError('Worker response does not match the request')
                if response.get('event') in ('failure', 'cleanup_failure'):
                    failure = NativeFailure(**response['failure'])
                    if response['event']=='failure':
                        if primary is not None:
                            raise ValueError('Duplicate worker primary failure')
                        primary = failure
                        if 'checkpoint_committed' in response:
                            if command not in ('checkpoint_capture','checkpoint_restore') or type(response['checkpoint_committed']) is not bool:
                                raise ValueError('Invalid checkpoint commit status')
                            checkpoint_committed=response['checkpoint_committed']
                        deadline = time.monotonic()+min(timeout, 5)
                    else:
                        cleanup.append(failure)
                    continue
                if type(response.get('ok')) is not bool:
                    raise ValueError('Invalid worker response status')
                if 'rejected' in response:
                    if (set(response)!={'id','ok','rejected','state','committed','failure','cleanup'} or
                        response['ok'] is not False or response['rejected'] is not True or response['committed'] is not False or
                        response['state']!='STARTED' or command not in ('checkpoint_capture','checkpoint_restore') or
                        primary is not None or cleanup or response['failure'].get('stage')!=command):
                        raise ValueError('Invalid recoverable checkpoint rejection')
                if primary is not None and (response['ok'] or NativeFailure(**response['failure'])!=primary):
                    raise ValueError('Worker final response contradicts its primary failure')
            except Exception as error:
                if primary or cleanup:
                    cause = primary or cleanup[0]
                    previous = cleanup if primary else cleanup[1:]
                    interrupted = NativeFailure(stage='cleanup', code=None,
                                                message=f'Worker cleanup interrupted: {type(error).__name__}: {error}')
                    failure=SessionError(cause, cleanup=(*previous, interrupted))
                    failure.checkpoint_committed=checkpoint_committed
                    raise failure from error
                raise
            if not response['ok']:
                if 'rejected' in response:
                    raise CheckpointRequestRejected(NativeFailure(**response['failure']),
                        cleanup=tuple(NativeFailure(**item) for item in response['cleanup']))
                failure=SessionError(NativeFailure(**response['failure']),
                                     cleanup=tuple(NativeFailure(**item) for item in response.get('cleanup', ())))
                failure.checkpoint_committed=checkpoint_committed
                raise failure
            return response['value']

    def finish(self, *, grace=1):
        if self._disposed:
            return self.process.returncode
        self._disposed = True
        try:
            self.process.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
        finally:
            self._stopping.set()
            try:
                self._writes.put_nowait(None)
            except queue.Full:
                pass
            for thread in self._threads:
                if thread.ident is not None:
                    thread.join(timeout=1)
            for pipe in (self.process.stdin, self.process.stdout, self.process.stderr):
                pipe.close()
        return self.process.returncode

    def abort(self):
        self.finish(grace=0)


class ProcessSession:
    """A single native project. All calls are serialized; cancel() is concurrent.

    Context management or explicit close is required. The finalizer is a last
    resort and never attempts to save/report a simulation. Files are caller-owned
    at this low-level boundary; no input or output is deleted by a session.
    """
    def __init__(self, backend, library, *, working_directory, call_timeout, cancel_event, poll_interval=.05,
                 _execution_guard=None):
        if isinstance(call_timeout, bool) or not isinstance(call_timeout, (int, float)) or not math.isfinite(call_timeout) or call_timeout<=0:
            raise ValueError('call_timeout must be finite positive seconds')
        if cancel_event is not None and not callable(getattr(cancel_event, 'is_set', None)):
            raise TypeError('cancel_event must provide is_set()')
        if isinstance(poll_interval, bool) or not isinstance(poll_interval, (int, float)) or not math.isfinite(poll_interval) or poll_interval<=0:
            raise ValueError('poll_interval must be finite positive seconds')
        directory = Path(working_directory)
        if not directory.is_absolute() or not directory.is_dir():
            raise ValueError('working_directory must be an existing absolute directory')
        self.working_directory = directory.resolve()
        self.call_timeout = float(call_timeout)
        self.poll_interval = float(poll_interval)
        self._cancel_event = cancel_event
        self._cancelled = threading.Event()
        self._lock = threading.RLock()
        self.state = 'NEW'
        self.failure = None
        self.cleanup_errors = ()
        self.objects = None
        self.warnings = None
        self.flow_units = None
        self.result_context = ResultContext()
        self.info = None
        self.worker_identity = None
        self._connection = None
        self._checkpoint_enabled = False
        self.checkpoint_outputs = ()
        self.checkpoint_cleanup = ()
        self._checkpoint_output_inventory = None
        self._checkpoint_snapshot = None
        try:
            if self._is_cancelled():
                raise SessionCancelled(NativeFailure(stage='launch', code=None, message='Session cancelled'))
            try:
                self._connection = _WorkerConnection(self.working_directory)
            except (OSError, RuntimeError) as error:
                raise SessionError(NativeFailure(stage='launch', code=None, message=str(error))) from error
            self._finalizer = weakref.finalize(self, self._connection.abort)
            metadata = self._rpc('load', {'library': library, 'expected_sha256': backend._selection()[1],
                                         'solver_kind': backend.worker_kind, 'execution_guard': _execution_guard})
            metadata = dict(metadata)
            admitted = metadata.pop('execution_guard', None)
            if _execution_guard is not None and admitted != _execution_guard['token']:
                raise SessionError(NativeFailure(stage='load',code=None,message='Worker did not confirm execution admission'))
            identity = metadata.pop('worker_identity', None)
            if identity is not None:
                from ._worker_identity import validate
                try:self.worker_identity = validate(identity, pid=self.pid)
                except ValueError as error:
                    raise SessionError(NativeFailure(stage='load',code=None,message=str(error))) from error
            self.info = backend._loaded(metadata)
            if not self.info.available:
                error = SessionError(NativeFailure(stage='load', code=None, message=self.info.reason))
                error.backend_metadata = metadata
                raise error
            self.state = 'LOADED'
        except BaseException:
            if self._connection:
                self._connection.abort()
                self._finalizer.detach()
            raise

    @property
    def pid(self):
        return self._connection.process.pid

    @property
    def returncode(self):
        return self._connection.process.poll()

    @property
    def stderr(self):
        return self._connection.stderr

    def _is_cancelled(self):
        return self._cancelled.is_set() or bool(self._cancel_event and self._cancel_event.is_set())

    def cancel(self):
        self._cancelled.set()

    def _rpc(self, command, args, *, closing=False):
        try:
            return self._connection.exchange(command, args,
                timeout=min(5, self.call_timeout) if closing else self.call_timeout,
                poll_interval=self.poll_interval,
                cancelled=(lambda: False) if closing else self._is_cancelled)
        except CheckpointRequestRejected as error:
            if self.state!='STARTED':
                self._connection.abort();self._finalizer.detach();self.state='FAILED'
                raise SessionError(NativeFailure(stage=command,code=None,message='Checkpoint rejection in invalid session state')) from error
            error.stderr=self.stderr
            error.returncode=self.returncode
            raise
        except BaseException as error:
            # Reap first: native leaks, blocked calls and partial start are
            # contained even when graceful cleanup cannot return.
            self._connection.finish(grace=.5 if isinstance(error, SessionError) and not isinstance(error, (SessionTimeout, SessionCancelled)) else 0)
            self._finalizer.detach()
            self.state = 'FAILED'
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            if not isinstance(error, SessionError):
                error = SessionError(NativeFailure(stage=command, code=None, message=f'{type(error).__name__}: {error}'))
            error.stderr = self.stderr
            error.returncode = self.returncode
            self.failure = error
            self.cleanup_errors = error.cleanup
            raise error

    def _require(self, *states):
        if self.state not in states:
            raise SessionStateError(f'Operation requires {states}, session is {self.state}')

    def _checkpoint_invalid_response(self, command, error, *, committed=None):
        """The child may have advanced; never leave a divergent parent reusable."""
        self._connection.abort();self._finalizer.detach();self.state='FAILED'
        failure=SessionError(NativeFailure(stage=command,code=None,
            message=f'{type(error).__name__}: {error}'),stderr=self.stderr,returncode=self.returncode)
        failure.checkpoint_committed=committed
        self.failure=failure
        raise failure from error

    def open_checkpoint(self, snapshot, *, expected=(), schema=None):
        """Open, configure and start an execution owned by the checkpoint lifecycle.

        The snapshot's inputs must already exist in working_directory. Use
        Checkpoint.materialize() when resuming elsewhere. Native calls remain
        isolated; missing checkpoint ABI is an explicit session failure.
        """
        from ._checkpoint_context import write, read_outputs
        from ._workspace import remove_owned_tree
        from .results import RunSnapshot
        with self._lock:
            self._require('LOADED')
            if type(snapshot) is not RunSnapshot:raise TypeError('Expected a RunSnapshot')
            expected=tuple((key,tuple(names)) for key,names in expected)
            if Path(snapshot.execution_directory).resolve()!=self.working_directory:
                raise ValueError('Checkpoint snapshot belongs to another working directory')
            directory=self.working_directory/('.checkpoint-context-'+uuid.uuid4().hex)
            digest,identity=write(directory,snapshot) if schema is None else write(directory,snapshot,schema=schema)
            primary=None
            try:
                value=self._rpc('checkpoint_open_start',{'directory':str(directory),'sha256':digest})
                try:
                    from ._result_codec import exact
                    exact(value,('groups','warnings','flow_units','output_inventory_sha256'))
                    self.objects=EngineObjects(groups=tuple((k,tuple(v)) for k,v in value['groups']))
                    self.objects.verify(expected)
                    if (type(value['warnings']) is not int or value['warnings']<0 or
                        type(value['flow_units']) is not int or value['flow_units']!=
                        ('CFS','GPM','MGD','CMS','LPS','MLD').index(snapshot.units.flow_units)):
                        raise ValueError('Checkpoint engine warnings or flow units differ from snapshot')
                    mapping=read_outputs(directory,value['output_inventory_sha256'],snapshot)
                    self._checkpoint_output_inventory=mapping
                    from ._checkpoint_directory_outputs import roots
                    self._checkpoint_output_roots=roots(snapshot)
                    self.checkpoint_outputs=self._checkpoint_output_paths()
                    self.warnings=value['warnings'];self.flow_units=value['flow_units']
                    self.result_context=context_from_input(snapshot.input_bytes,self.info)
                except Exception as error:
                    self._checkpoint_invalid_response('checkpoint_open_start',error)
                self._checkpoint_snapshot=snapshot
                self._checkpoint_enabled=True;self.state='STARTED'
                return self.objects
            except BaseException as error:
                primary=error
                raise
            finally:
                try:remove_owned_tree(directory,parent=self.working_directory,identity=identity)
                except BaseException as error:
                    if primary is None:self._checkpoint_invalid_response('checkpoint_context_cleanup',error)
                    primary.checkpoint_context_cleanup=f'{type(error).__name__}: {error}'

    def _open_checkpoint(self, snapshot):
        return self.open_checkpoint(snapshot)

    def _checkpoint_output_paths(self, folder=None):
        from .checkpoint import CheckpointOutput, OUTPUT_ROLES
        mapping=self._checkpoint_output_inventory
        def directory(name):
            if folder is None:return None
            from ._checkpoint_directory_outputs import owner
            root=owner(getattr(self,'_checkpoint_output_roots',()),name)
            return self.working_directory/root if root is not None else None
        result=[]
        for item in mapping['outputs']:
            target=self.working_directory/item['relative_path']
            result.append(CheckpointOutput(role=OUTPUT_ROLES[item['role']],destination=target,
                path=folder/('output-'+str(item['index'])) if folder else target,directory=directory(item['relative_path'])))
        if mapping['trace'] is not None:
            target=self.working_directory/mapping['trace']
            result.append(CheckpointOutput(role='easysewer:ponding-trace',destination=target,
                path=folder/'trace' if folder else target,directory=directory(mapping['trace'])))
        return tuple(result)

    def save_checkpoint(self, directory):
        """Save to a new directory; existing content is never replaced.

        CheckpointRejected leaves this session running. Other SessionErrors may
        indicate loss of the worker; no automatic retry is performed.
        """
        from .checkpoint import Checkpoint
        from ._result_codec import exact
        with self._lock:
            value=self._capture_checkpoint(directory)
            try:
                exact(value,('directory','sha256','simulation_seconds'))
                path=(self.working_directory/Path(directory)).absolute()
                if value['directory']!=str(path):raise ValueError('Checkpoint capture returned another directory')
                saved=Checkpoint.load(path)
                if (saved.sha256!=value['sha256'] or type(value['simulation_seconds']) not in (int,float) or
                    saved.simulation_seconds!=value['simulation_seconds']):
                    raise ValueError('Checkpoint capture returned inconsistent content')
                return saved
            except Exception as error:
                self._checkpoint_invalid_response('checkpoint_capture',error,committed=False)

    def restore_checkpoint(self, checkpoint):
        """Restore an unfinished boundary, returning current output locations.

        CheckpointRejected means no state committed and the session remains
        usable. Fatal failures can carry checkpoint_committed=True; they must
        not be retried on this session. Outputs stay in owned staging files.
        """
        from .checkpoint import Checkpoint, CheckpointRestore
        from ._result_codec import exact
        with self._lock:
            self._require('STARTED')
            if not self._checkpoint_enabled:raise SessionStateError('Checkpoint lifecycle was not enabled before open')
            archive=checkpoint if type(checkpoint) is Checkpoint else None
            directory=archive.directory if archive else checkpoint
            value=self._restore_checkpoint(directory,expected_sha256=archive.sha256 if archive else None)
            try:
                exact(value,('native','cleanup','simulation_seconds','output_directory'))
                native=value['native'];exact(native,('error','committed','cleanup_error','provider_errors'))
                if (native['committed'] is not True or type(native['error']) is not int or native['error']!=0 or
                    type(native['cleanup_error']) is not int or native['cleanup_error']<0 or native['provider_errors']!=[]):
                    raise ValueError('Invalid checkpoint commit result')
                seconds=value['simulation_seconds']
                if type(seconds) not in (int,float) or not math.isfinite(seconds) or seconds<0:
                    raise ValueError('Invalid restored checkpoint clock')
                if archive is not None and seconds!=archive.simulation_seconds:
                    raise ValueError('Restored checkpoint clock differs from the archive')
                folder=Path(value['output_directory'])
                if (not folder.is_absolute() or folder.parent!=self.working_directory or
                    not folder.name.startswith('.checkpoint-restore-') or folder.is_symlink() or
                    not folder.is_dir() or folder.resolve()!=folder):
                    raise ValueError('Restored output directory escaped its workspace')
                outputs=self._checkpoint_output_paths(folder)
                if any(v.path.is_symlink() or not v.path.is_file() for v in outputs):
                    raise ValueError('Restored output file is missing or not regular')
                from ._checkpoint_directory_inputs import _parents
                from ._directory_tree import _node
                for item in outputs:
                    if item.directory is not None:
                        _parents(self.working_directory,item.destination);_node(item.destination,'file')
                        if not item.path.samefile(item.destination):raise ValueError('Restored output directory binding is missing')
                if type(value['cleanup']) is not list:raise ValueError('Invalid checkpoint cleanup diagnostics')
                cleanup=tuple(NativeFailure(**v) for v in value['cleanup'])
                if native['cleanup_error']:
                    cleanup+=(NativeFailure(stage='checkpoint_native_cleanup',code=native['cleanup_error'],
                        message='Native restoration committed but retiring old resources failed'),)
                self.checkpoint_outputs=outputs
                self.checkpoint_cleanup+=cleanup
                return CheckpointRestore(simulation_seconds=seconds,outputs=outputs,cleanup=cleanup)
            except Exception as error:
                self._checkpoint_invalid_response('checkpoint_restore',error,committed=True)

    def _capture_checkpoint(self, directory):
        with self._lock:
            self._require('STARTED')
            if not self._checkpoint_enabled:raise SessionStateError('Checkpoint lifecycle was not enabled before open')
            return self._rpc('checkpoint_capture',{'directory':str((self.working_directory/Path(directory)).absolute())})

    def _restore_checkpoint(self, directory, *, expected_sha256=None):
        with self._lock:
            self._require('STARTED')
            if not self._checkpoint_enabled:raise SessionStateError('Checkpoint lifecycle was not enabled before open')
            args={'directory':str((self.working_directory/Path(directory)).absolute())}
            if expected_sha256 is not None:args['sha256']=expected_sha256
            return self._rpc('checkpoint_restore',args)

    def open(self, input, report, output, *, expected=(), overwrite=False):
        with self._lock:
            self._require('LOADED')
            if type(overwrite) is not bool:
                raise TypeError('overwrite must be bool')
            paths = [(self.working_directory/Path(value)).resolve() for value in (input, report, output)]
            if any('\x00' in str(path) for path in paths):
                raise ValueError('Paths cannot contain NUL')
            if not paths[0].is_file():
                raise FileNotFoundError(paths[0])
            for index, path in enumerate(paths):
                for other in paths[:index]:
                    if path==other or (path.exists() and other.exists() and os.path.samefile(path, other)):
                        raise ValueError('Input, report and output must be distinct files')
            for path in paths[1:]:
                if not path.parent.is_dir() or (path.exists() and not path.is_file()):
                    raise ValueError('Output requires an existing directory and a regular file path')
                if path.exists() and not overwrite:
                    raise FileExistsError(path)
            expected = tuple((collection, tuple(names)) for collection, names in expected)
            # Avoid passing UTF-8 absolute prefixes to the narrow fopen API.
            # Windows native full-path resolution still requires the working
            # directory to be representable by the process ANSI code page.
            native_paths = [str(path.relative_to(self.working_directory)) if path.is_relative_to(self.working_directory)
                            else str(path) for path in paths]
            # Context is evidence from these bytes, not a guess from native
            # output zeros. Oversized/opaque input remains explicitly unknown.
            with paths[0].open('rb') as stream:raw=stream.read(MAX_FRAME+1)
            if len(raw)>MAX_FRAME:raw=None
            value = self._rpc('open', {'paths': native_paths})
            self.state = 'OPEN'
            try:
                self.objects = EngineObjects(groups=tuple((key, tuple(names)) for key, names in value['groups']))
                self.objects.verify(expected)
                self.warnings = value['warnings']
                if raw is not None:
                    with paths[0].open('rb') as stream:observed=stream.read(MAX_FRAME+1)
                    if observed == raw:self.result_context = context_from_input(raw, self.info)
                self.flow_units = value['flow_units']
            except BaseException:
                # A mismatched snapshot may never reach start(). Preserve the
                # validation exception even if native close also fails.
                try:
                    self.close()
                except SessionError:
                    pass
                raise
            return self.objects

    def start(self, *, save_results=True):
        with self._lock:
            self._require('OPEN')
            if type(save_results) is not bool:
                raise TypeError('save_results must be bool')
            self._rpc('start', {'save_results': save_results})
            self.state = 'STARTED'

    def configure_execution(self, parameters):
        with self._lock:
            self._require('OPEN')
            self._rpc('configure', {'parameters': parameters})

    def execution_results(self):
        with self._lock:
            self._require('ENDED', 'REPORTED')
            return self._rpc('execution_results', {})

    def step(self, *, max_steps=1):
        with self._lock:
            self._require('STARTED')
            if type(max_steps) is not int or not 1<=max_steps<=100000:
                raise ValueError('max_steps must be an integer from 1 to 100000')
            result = StepResult(**self._rpc('step', {'max_steps': max_steps}))
            if result.finished:
                self.state = 'FINISHED'
            return result

    def end(self):
        with self._lock:
            self._require('STARTED', 'FINISHED')
            result = MassBalance(**self._rpc('end', {}))
            self.result_context = self.result_context.with_fact('swmm:completed', 'yes' if self.state=='FINISHED' else 'no')
            result = result.with_context(self.result_context)
            self.state = 'ENDED'
            return result

    def report(self):
        with self._lock:
            self._require('ENDED')
            self._rpc('report', {})
            self.state = 'REPORTED'

    def close(self):
        with self._lock:
            if self.state=='CLOSED':
                return
            if self.state=='FAILED':
                self.state = 'CLOSED'
                return
            try:
                value = self._rpc('close', {}, closing=True)
                self.cleanup_errors = tuple(NativeFailure(**item) for item in value['cleanup'])
                code = self._connection.finish()
                if code:
                    self.cleanup_errors += (NativeFailure(stage='exit', code=code, message='Worker did not exit normally'),)
                if self.cleanup_errors:
                    self.failure = SessionError(self.cleanup_errors[0], cleanup=self.cleanup_errors[1:],
                                                stderr=self.stderr, returncode=code)
                    raise self.failure
            finally:
                self._finalizer.detach()
                self.state = 'CLOSED'

    def __enter__(self):
        self._require('LOADED', 'OPEN', 'STARTED', 'FINISHED', 'ENDED', 'REPORTED')
        return self

    def __exit__(self, exc_type, exc, traceback):
        try:
            self.close()
        except SessionError:
            if exc is None:
                raise
        return False
