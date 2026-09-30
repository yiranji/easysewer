# Checkpoint session contract (2.0 development)

`easysewer.runtime.CheckpointSession` is an optional extension of `Session`.
The public names are candidates until the 2.0 API freeze. Packaged standard
revision 13 and custom native I/O revision 14 provide checkpoint ABI 2.
Earlier libraries do not gain that capability from a matching SWMM version.
Runner checkpoint scheduling,
resume, provenance and output publication are described in the
[Runner contract](checkpoint-runner.md); final integration qualification remains
separate from this Session contract.
See the [checkpoint scope](checkpoint-support.md) for state and resource coverage.

An ordinary `open()` / `start()` session cannot later enable checkpoints.
`open_checkpoint(snapshot, expected=...)` owns open, backend configuration and
start as one operation. The `RunSnapshot` must describe the actual staged INP,
immutable resources, engine and backend settings. It verifies executed resource
paths and refuses existing output files before native open. A changed engine or
invalid startup is fatal; the worker is reaped, including when parent metadata
validation or bootstrap cleanup fails after native startup.

At an unfinished step boundary, `save_checkpoint(new_directory)` returns a
`Checkpoint`. It refuses an existing destination. `Checkpoint.load(directory)`
verifies storage without loading native code. `Checkpoint.materialize(new_directory)`
revalidates the stored value and builds independent input files, returning a
relocated snapshot. It cannot silently merge into an existing directory. The
original model, configuration and execution identity are retained. Moving the
native library requires the same binary and supported execution platform.

Given an already prepared checkpoint, low-level restoration uses only public
interfaces:

```python
from easysewer.runtime import Checkpoint, StandardBackend

checkpoint = Checkpoint.load(saved_directory)
snapshot = checkpoint.materialize(new_workspace)
backend = StandardBackend()
with backend.session(working_directory=new_workspace) as session:
    session.open_checkpoint(snapshot)
    restored = session.restore_checkpoint(checkpoint)
    while not session.step(max_steps=3).finished:
        pass
    balance = session.end()
    session.report()

# The context manager has now closed all native files.
for output in restored.outputs:
    print(output.role, output.path, output.destination)
```

`restore_checkpoint()` accepts a loaded Checkpoint or a directory. For a loaded
value, the worker verifies its manifest digest before committing. Replacing that
directory with another valid checkpoint cannot silently restore different state.
The returned `CheckpointRestore` contains the simulation clock, cleanup
diagnostics and immutable output mappings. `session.checkpoint_outputs` always
holds the latest successful public restoration's mappings, including the custom
ponding trace. Repeated restoration supersedes previous mappings.

`path` is the actual file being written. `destination` is its original path in
the current execution workspace. Restoration keeps independent output prefixes;
it does not overwrite original files or publish a RunResult. After end/report/
close, callers must collect the mapped files. Reading only `model.out` or the
original LID/trace path after restoring would read stale startup output. The
caller owns the execution workspace and its eventual removal; close does not
delete the mapped files. Runner owns transactional publication when using its
complete checkpoint envelope.

`CheckpointRejected` guarantees no state committed and a healthy running session
at the rejection boundary. It may report cleanup diagnostics; the caller can
continue stepping or explicitly choose another checkpoint. Other `SessionError`s
are fatal. `checkpoint_committed=True` means a native state change occurred before
the failure; never automatically retry on that session. Cancellation and native
call timeouts reap the worker, as with ordinary sessions. This API does not yet
offer per-operation cancellation that preserves a live session.

The low-level API does not construct a complete execution snapshot from an
arbitrary Model. That is Runner's responsibility. The public Session tests use
qualified preparation fixtures; distinct Runner tests exercise full execution
preparation and result publication.
