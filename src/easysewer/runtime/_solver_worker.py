"""Private versioned JSON-lines worker; no pickle or dynamic command execution."""

from dataclasses import asdict
import hashlib
import json
import os
import queue
import sys
import threading
from pathlib import Path

MAX_FRAME = 16 * 1024 * 1024


def _configure_error_mode():
    """Keep loader failures in the worker protocol instead of Windows dialogs."""
    if os.name != 'nt':
        return
    import ctypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.GetErrorMode.argtypes = []
    kernel.GetErrorMode.restype = ctypes.c_uint
    kernel.SetErrorMode.argtypes = [ctypes.c_uint]
    kernel.SetErrorMode.restype = ctypes.c_uint
    # This process belongs to EasySewer. Preserve inherited flags and apply
    # before starting any threads; the parent application's policy is untouched.
    kernel.SetErrorMode(kernel.GetErrorMode() | 0x0001)  # SEM_FAILCRITICALERRORS


def main():
    _configure_error_mode()
    # Native stdout goes to stderr. Keep a dedicated duplicate solely for RPC.
    output = os.fdopen(os.dup(sys.stdout.fileno()),'wb',buffering=0)
    os.dup2(sys.stderr.fileno(),sys.stdout.fileno())
    commands = queue.Queue(maxsize=2)
    stopping = threading.Event()

    def receive():
        try:
            pending = bytearray()
            while True:
                # Do not hold a BufferedReader lock in a daemon thread during
                # interpreter shutdown; use the raw OS pipe instead.
                chunk = os.read(sys.stdin.fileno(), 8192)
                if not chunk:
                    # A parent crash/exit must also reap a worker currently
                    # stuck inside a native call (ctypes releases the GIL).
                    if not stopping.is_set(): os._exit(74)
                    return
                pending.extend(chunk)
                while b'\n' in pending:
                    end = pending.index(b'\n')+1
                    if end>MAX_FRAME:
                        os._exit(75)
                    line = bytes(pending[:end])
                    del pending[:end]
                    commands.put(json.loads(line))
                if len(pending)>=MAX_FRAME:
                    os._exit(75)
        except BaseException:
            os._exit(76)

    def send(value):
        payload=json.dumps(value,ensure_ascii=True,allow_nan=False,separators=(',',':')).encode('ascii')+b'\n'
        if len(payload)>MAX_FRAME:
            raise ValueError('Worker response exceeds the explicit frame limit')
        pending=memoryview(payload)
        while pending:
            written=output.write(pending)
            if not written: raise BrokenPipeError('Worker response pipe closed')
            pending=pending[written:]

    threading.Thread(target=receive,daemon=True).start()
    from ._native_solver import NativeSolver, NativeCallFailure
    solver = None
    admission = None
    lifecycle = None
    state = 'NEW'
    try:
        while True:
            request = commands.get()
            request_id = request.get('id')
            stage = request.get('command')
            checkpoint_committed = False
            def cleanup_error(issue):
                send({'id':request_id,'event':'cleanup_failure','failure':asdict(issue)})
            try:
                if request.get('version') != 1 or type(request_id) is not int:
                    raise ValueError('Unsupported worker request')
                args=request.get('args',{})
                if stage == 'load' and state=='NEW':
                    guard=args.get('execution_guard')
                    if guard is not None:
                        from ._admission import enter
                        admission=enter(guard)
                    kind=args.get('solver_kind','standard')
                    if kind=='standard':solver_type=NativeSolver
                    elif kind=='flexible-ponding':
                        from ._native_flexible import NativeFlexibleSolver
                        solver_type=NativeFlexibleSolver
                    else:raise ValueError('Unsupported native solver kind')
                    solver=solver_type(args['library'], expected_sha256=args.get('expected_sha256')); state='LOADED'
                    from ._worker_identity import capture
                    value=dict(solver.metadata,worker_identity=capture(),
                               execution_guard=guard['token'] if guard is not None else None)
                elif stage == 'open' and state=='LOADED':
                    value=solver.open(args['paths']); state='OPEN'
                elif stage == 'checkpoint_open_start' and state=='LOADED':
                    from ._checkpoint_context import read
                    from ._checkpoint_lifecycle import CheckpointLifecycle
                    from ._result_codec import exact
                    exact(args,('directory','sha256'))
                    snapshot,declarations=read(args['directory'],args['sha256'],_with_declarations=True)
                    if Path(snapshot.execution_directory).resolve()!=Path.cwd().resolve():
                        raise ValueError('Checkpoint context belongs to another working directory')
                    lifecycle=CheckpointLifecycle(solver,snapshot,_declarations=declarations)
                    value=lifecycle.open_start(cleanup_on_error=False);state='STARTED'
                    from ._checkpoint_context import output_inventory, write_outputs
                    mapping=output_inventory(snapshot,lifecycle.api.outputs(),
                        solver.trace.name if lifecycle.custom and solver.trace else None)
                    value['output_inventory_sha256']=write_outputs(args['directory'],mapping)
                elif stage == 'checkpoint_capture' and state=='STARTED' and lifecycle is not None:
                    from ._result_codec import exact
                    exact(args,('directory',))
                    archive=lifecycle.capture(args['directory'])
                    value=dict(directory=str(archive.directory),sha256=hashlib.sha256(archive.manifest).hexdigest(),
                               simulation_seconds=archive.simulation_seconds)
                elif stage == 'checkpoint_restore' and state=='STARTED' and lifecycle is not None:
                    from ._checkpoint_container import load
                    from ._result_codec import exact
                    exact(args,('directory','sha256') if 'sha256' in args else ('directory',))
                    archive=load(args['directory'])
                    if 'sha256' in args and hashlib.sha256(archive.manifest).hexdigest()!=args['sha256']:
                        raise ValueError('Checkpoint changed since the caller loaded it')
                    outcome=lifecycle.restore(archive)
                    checkpoint_committed=outcome.native.committed
                    if not lifecycle.can_continue():
                        raise NativeCallFailure(stage,outcome.native.error,'Solver is unusable after checkpoint restoration')
                    value=asdict(outcome)
                    value['simulation_seconds']=archive.simulation_seconds
                    value['output_directory']=str(lifecycle.owned[-1][0])
                elif stage == 'start' and state=='OPEN':
                    value=solver.start(args['save_results']); state='STARTED'
                elif stage == 'configure' and state=='OPEN':
                    value=solver.configure(args['parameters'])
                elif stage == 'step' and state=='STARTED':
                    count=args.get('max_steps',1)
                    if type(count) is not int or not 1 <= count <= 100000:
                        raise ValueError('Invalid step batch size')
                    value=lifecycle.step(count) if lifecycle else solver.step(count)
                    if value['finished']: state='FINISHED'
                elif stage == 'end' and state in ('STARTED','FINISHED'):
                    value=lifecycle.end() if lifecycle else solver.end(); state='ENDED'
                elif stage == 'report' and state=='ENDED':
                    value=lifecycle.report() if lifecycle else solver.report(); state='REPORTED'
                elif stage == 'execution_results' and state in ('ENDED','REPORTED'):
                    value=solver.execution_results()
                elif stage == 'close':
                    issues=(lifecycle.cleanup(on_error=cleanup_error) if lifecycle else solver.cleanup(on_error=cleanup_error)) if solver else []
                    stopping.set()
                    send({'id':request_id,'ok':True,'value':{'cleanup':[asdict(issue) for issue in issues]}})
                    break
                else:
                    raise ValueError(f'Invalid {stage} in worker state {state}')
                send({'id':request_id,'ok':True,'value':value})
            except BaseException as error:
                from .backend import NativeFailure
                failure=error.failure if isinstance(error,NativeCallFailure) else NativeFailure(stage=str(stage),code=None,message=f'{type(error).__name__}: {error}')
                retained_cleanup=list(getattr(error,'checkpoint_cleanup',()))
                if getattr(error,'checkpoint_cleanup_error',None):
                    retained_cleanup.append(NativeFailure(stage='checkpoint_workspace_cleanup',code=None,
                                                         message=error.checkpoint_cleanup_error))
                if lifecycle is not None and stage in ('checkpoint_capture','checkpoint_restore'):
                    from ._checkpoint_native import CheckpointError
                    from ._checkpoint_lifecycle import RestoreRejected
                    if isinstance(error,RestoreRejected):
                        detail='; '.join(f'{v.stage}: {v.message}' for v in error.result.provider_errors)
                        failure=NativeFailure(stage=stage,code=error.result.error,message=str(error)+((': '+detail) if detail else ''))
                        if error.result.cleanup_error:
                            retained_cleanup.append(NativeFailure(stage='checkpoint_native_cleanup',code=error.result.cleanup_error,
                                                                 message='Failed to close a staged native checkpoint resource'))
                    checkpoint_committed=bool(getattr(error,'checkpoint_committed',checkpoint_committed))
                    if (not checkpoint_committed and isinstance(error,(ValueError,TypeError,OSError,CheckpointError,RestoreRejected))
                            and lifecycle.can_continue()):
                        send(dict(id=request_id,ok=False,rejected=True,state='STARTED',committed=False,
                                  failure=asdict(failure),cleanup=[asdict(v) for v in retained_cleanup]))
                        continue
                # Send the primary cause before cleanup, which can itself
                # crash or block inside a native function.
                event={'id':request_id,'event':'failure','failure':asdict(failure)}
                if stage in ('checkpoint_capture','checkpoint_restore'):event['checkpoint_committed']=checkpoint_committed
                send(event)
                cleanup=retained_cleanup
                for issue in cleanup:cleanup_error(issue)
                cleanup.extend(solver.cleanup(on_error=cleanup_error) if solver else [])
                stopping.set()
                send({'id':request_id,'ok':False,'failure':asdict(failure),'cleanup':[asdict(issue) for issue in cleanup]})
                break
    finally:
        try:
            if solver: solver.cleanup()
        finally:
            try:
                stopping.set()
                output.close()
            finally:
                if admission is not None:admission.close()


if __name__ == '__main__':
    main()
