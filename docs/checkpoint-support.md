# Complete checkpoint scope (2.0 development)

A checkpoint continues the same execution with the same engine bytes, model,
configuration and immutable input resources. It restores the accumulated state
and output history at a successful, unfinished step boundary. It is distinct
from SWMM HOTSTART (a subset of solver state), an interface cache (reusable
process data), and a RunResult archive (completed or failed execution evidence).

The packaged standard revision 16 and FlexiblePonding native I/O revision 14
provide native checkpoint ABI 2. Use [Runner](checkpoint-runner.md) for complete preparation, scheduling,
result collection and publication; the [Session interface](checkpoint-session.md)
requires the caller to manage the verified snapshot and output mappings.

## State ownership

Static parameters and object identities are reconstructed from the verified INP
and bound to the execution identity. Persistent state is restored by the owners
below. Temporary work arrays are rebuilt or overwritten before their next use;
addresses, native file handles and uninitialized storage are not serialized.

| Owner | Persistent state carried into continuation |
| --- | --- |
| Controls and clocks | Control/PID histories, action settings, routing/report/runoff clocks, event and rule cursors |
| Dynamic wave and network | Hydraulic iteration history, node/link/subtype states, water quality, storage exfiltration and native removal counters |
| Inlets | Capture/backflow history and accumulated inlet statistics |
| Infiltration and subcatchments | Method-specific infiltration histories, surface storage, runoff/quality histories and catchment volumes |
| Rain gages and runoff | Rain intervals/accumulators, runoff scheduling, wet flags and cache replay state |
| Climate and tables | Weather/calendar/evaporation histories, seasonal values, curve/time-series consumer cursors and interpolation state |
| RDII and routing interfaces | Input frame windows, interpolation history and logical input positions |
| Groundwater | Aquifer/saturated/unsaturated numerical history and groundwater statistics |
| Snow | Snow-water/cold-content/temperature histories, depletion/coverage history and seasonal melt factors |
| LID | Layer states, flux histories, clogging/regeneration/dry-time state, water balances and detailed-report line/dry-period cache |
| Mass balance and statistics | Accumulated water/quality totals, step rate histories, object statistics, maxima and peak times |
| OUT and native ponding | Reporting periods/averaging accumulators and finalized custom split-step removal rates |
| Python ponding policy | Previous node values, removed-volume/pollutant ledgers, active-step counts, clock, step count and trace prefix |
| Runner context | Diagnostics, cache conditions/consumption evidence, backend products, observed steps and prior continuation history |

The [native recipe](../native/checkpoint/README.md) describes ownership and the
historical per-owner tests. The production coordinator validates and stages all
owners together before applying state. Isolated owner tests are supplemented by
complete native, worker, public Session and Runner continuation tests.

## Resources and outputs

Input streams use logical roles or ordered table-consumer identities. The outer
container binds their complete immutable contents and reconstructs independent
resource copies. Removing the original input/workspace directory is supported
after the checkpoint has committed. Several consumers can share one physical
input without sharing a mutable cursor.

The checkpoint includes verified prefixes for the main RPT, OUT, SAVE RUNOFF,
SAVE OUTFLOWS, SAVE HOTSTART and each LID detailed report, plus the optional
Python ponding trace. Numeric state, byte prefixes and file positions must agree.
Restoration writes to new owned files. Runner publishes from the latest restored
mapping after closing the solver; it does not write back to historical external
output paths. A Session caller must explicitly collect those mapped files.

## Compatibility and failure boundaries

- Native checkpoints require matching library bytes, platform/architecture,
  model/order, semantic configuration, policy and resource contents. They are
  not a cross-platform state-conversion format. Reproducible builds alone do not
  make checkpoints from older candidate libraries compatible.
- Runner resume may change operational settings listed in `ResumeConfig`,
  including output location and step batch size. It cannot change the modeled
  scenario. Original execution identity and each recovery attempt remain distinct.
- Capture is allowed only after successful startup or a complete unfinished
  step batch, before end/report finalization. A partially adjusted custom step,
  failed step or finalized solver is not a valid capture boundary.
- Corrupt, incompatible or incomplete archives are rejected. A
  `CheckpointRejected` means state did not commit and the session remains usable.
  Fatal errors, cancellation and timeouts close the worker. A failure marked
  `checkpoint_committed=True` must not be retried on that session.
- FlexiblePonding retains its active DYNWAVE and ALLOW_PONDING requirements.
  Disabled routing is supported by the standard backend; the custom backend
  rejects that configuration before capture.
- Loading, inspecting and materializing stored data can run without native
  libraries. Continuing a simulation requires a process-capable runtime with the
  matching engine. Host simulation of the pure profile does not certify a browser.

## Result equivalence

Continuation checks compare complete OUT bytes, report content after removing
volatile timestamps and relocated generated paths, mass balance, object identity,
interface products, LID reports, custom ledgers/traces and cache evidence.
Fresh-process tests remove the old workspace and reconstruct resources from the
archive. Omission tests and injected failures exercise state necessity and
transaction boundaries; a successful return alone is not the acceptance oracle.

This scope does not replace the complete 2.0 support matrix, API freeze, real
downstream migration, performance benchmarks or final platform release checks.
