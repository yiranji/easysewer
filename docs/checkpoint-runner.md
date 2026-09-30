# Runner checkpoints (2.0 development)

`Runner.run(..., checkpoints=CheckpointSchedule(...))` captures a complete
execution at successful, unfinished step boundaries. `Runner.resume()` loads
that execution into an independent workspace and uses the ordinary result
inspection and transactional publication path. The packaged standard revision 16
and custom native I/O revision 14 libraries contain checkpoint ABI 2. These APIs
remain candidates until the 2.0 API freeze; final R02 acceptance and the complete
release matrix remain separate requirements.
The [checkpoint scope](checkpoint-support.md) lists state owners, resource and
output coverage, compatibility requirements and rejection boundaries.

```python
from datetime import timedelta
from easysewer.model import FileReference
from easysewer.runtime import (
    CheckpointSchedule, ResumeConfig, RunConfig, Runner,
)

runner = Runner()
saved = []
result = runner.run(
    model,
    RunConfig(output_directory=FileReference(path="first", direction="output"),
              step_batch_size=10),
    checkpoints=CheckpointSchedule(
        directory=FileReference(path="checkpoints", direction="output"),
        interval=timedelta(minutes=5),
        on_saved=saved.append,
    ),
)
if saved:
    resumed = runner.resume(
        saved[0],  # A moved checkpoint directory is also accepted.
        ResumeConfig(output_directory=FileReference(path="resumed", direction="output")),
    )
```

The interval measures simulated time. A save happens at the first unfinished
step-batch boundary at or beyond the next interval, so it can overshoot that
time. Intervals crossed within one batch produce one checkpoint. A simulation
that finishes in its first batch produces no checkpoints; reduce
`step_batch_size` when intermediate boundaries are needed. Restored schedules
start at the next interval beyond the saved clock. Each capture uses a new
directory named with the attempt ID and cumulative step count.

`on_saved` receives a committed `RunnerCheckpoint`, whose `directory`, `sha256`,
`simulation_seconds`, `snapshot` and original `config` can be inspected.
`RunnerCheckpoint.load(directory)` verifies the envelope without starting native
code. Passing a loaded value to resume does not bypass revalidation. The
Session-level `Checkpoint` and the Runner envelope are distinct: a low-level
Session archive alone lacks Runner cache, diagnostic and publication context.

`ResumeConfig` changes only operational options: the explicitly selected output
directory and artifact stem, overwrite/retention policies, progress and polling
intervals, deadlines, batch size and report-reading options. Report options
default to the original execution's settings. Simulation settings, input bytes,
immutable resources, backend parameters and original run ID remain bound to the
checkpoint. The caller's registered backend selects executable code; stored
library paths never do. Engine bytes and execution metadata must match. This is
not cross-platform native-state conversion.

Each attempt has a separate identity. `RunResult.continuations` retains the
checkpoint hashes, original execution ID, restored clock/step count and effective
attempt configuration, including for failed, rejected, cancelled and timed-out
attempts after archive verification. Cancellation before verification completes
cannot claim verified lineage. The published execution record is format 1.1 and
contains the same history; RunResult archive 1.3 can move and reload it while
retaining structured diagnostic locations (older 1.0/1.1 archives remain readable).

After native end/report/close, Runner collects the latest restored output
mapping. It refuses changed startup destinations before replacing them inside
its owned workspace, then verifies and transactionally publishes the finished
files. Main INP/RPT/OUT use the requested directory and stem. Resource outputs
and backend products remain under the original run's asset directory name in
that new directory. Resume does not write to original external resource-output
paths recorded in the snapshot. A failed attempt preserves old requested
outputs, and `keep_failed_artifacts` controls retained private files.

Saving, loading, materializing, native operations, collection and publication
participate in cancellation and the whole-attempt deadline. A save or notification
callback error stops the attempt. A callback error does not remove its already
committed checkpoint; it can be resumed in a new attempt. Fatal restoration
errors with `checkpoint_committed=True` are reported as
`run.checkpoint_committed` and are never retried on the same session. Cleanup
diagnostics remain separate from the primary failure. The lower-level Session
API offers explicit manual capture and recoverable-rejection handling.

See [Session contract](checkpoint-session.md), [envelope format](checkpoint-runner-context.md)
and [result archive](2.0-result-archive.md). Runtime integration tests do not
replace the remaining complete-domain, release-package and platform gates.

## Directory consumers

Checkpoints preserve initial captured inputs separately from mutable execution
trees and declared directory outputs. They retain shared/nested views, explicit
optional absence, empty directories and internal hardlinks. Resource byte and
identity checks still apply, including to native streams inside those trees.
For extension consumers, pass the same explicit registered schema to
`Runner.resume(..., schema=schema)` and register required `DirectoryAdapter`
instances on the Runner. Loading metadata does not install or execute a plugin.
Resume publishes under the requested new output directory; it does not rewrite
the original external resource-output locations. See
[directory resources](2.0-directory-resources.md) for formats and limits.
