# Checkpoint native ABI and build integration (2.0 development)

The native owners below form checkpoint ABI 2. Complete continuation also requires
verified input/output containers, Python policy state and the Session/Runner
contracts in [the runtime guide](../../docs/checkpoint-runner.md). HOTSTART remains
partial; a RunResult archive stores results rather than a live solver boundary.

## Reproducible test-only OUT/writes fault profile

The tool's optional `--profile lid-report-faults` selects a separate current-source
LID report fault/control contract, documented in [the LID guide](../lid_report/README.md).
Omitting `--profile` retains the checkpoint profile described below; the two
profiles' manifests, artifact names, test selections, and markers are distinct.

`tools/build_checkpoint_test_library.py` builds the bounded
`easysewer:test-only:checkpoint-output-writes:1` profile on Linux with an explicit
GCC-compatible compiler. It accepts **unmodified upstream source trees**, not old
prepared checkpoint trees. Supply the commits and normalized file bytes pinned
by `native/standard/source.json` and `native/flexible_ponding/source.json` through
an authorized source checkout. The tool does not download code, install a compiler,
change credentials, or copy private source into this repository.

Each build verifies every pinned upstream file, runs the current family preparation
(standard16 or custom native-I/O14), verifies all prepared bytes and recorded shared
recipes, then instruments a separate copy. Both the original checkout and current
production recipes remain unchanged. The output directory must be new and outside
the repository and upstream tree, including their resolved symlink locations.
An existing destination, including a dangling symlink, is rejected.

```sh
python -B tools/build_checkpoint_test_library.py \
  --family standard --source /path/to/pinned-epa-source \
  --compiler /usr/bin/gcc --destination /tmp/checkpoint-test-standard
python -B tools/build_checkpoint_test_library.py \
  --family custom --source /path/to/authorized-pinned-custom-source \
  --compiler /usr/bin/gcc --destination /tmp/checkpoint-test-custom

python -B tools/qualify_checkpoint_test_libraries.py \
  --standard-build /tmp/checkpoint-test-standard \
  --custom-build /tmp/checkpoint-test-custom \
  --destination /tmp/checkpoint-output-writes-results
```

Do not run either tool with `python -O`/`-OO` or `PYTHONOPTIMIZE`: fixture and
fresh-process checks use assertions, so both entry points fail closed under
optimized Python. Qualification fixes `OMP_NUM_THREADS=1`, requires
`/proc/self/fd` for descriptor-leak checks, and supplies absolute test-library
paths to child processes. Both independently built families are required; one
family must never be passed under the other's name to bypass a skip.

Build directories retain `verified-baseline/`, `instrumented-source/`, compiler
logs, `checkpoint-test-build.json`, and explicitly named
`checkpoint-test-standard.so` or `checkpoint-test-custom.so`. The instrumented
tree has **only** `checkpoint-test-source.json`, never a production
`prepared-source.json`. Evidence records the upstream identity, normalized source
hashes, every preparation recipe and test fragment, generated source hashes,
compiler command/version/binary hash, and final library hash. Inputs are checked
again after compilation; concurrent changes invalidate the build even when GCC
returns zero. The Linux linker rejects undefined hooks. Reproduction requires
the same toolchain and source bytes; cross-toolchain/platform bit identity is
not promised.

The profile appends the existing historical owner/bundle fragments required by
the OUT/writes Python Engine inheritance chain. Its only additional copied-source
changes are private staging fault hooks and ODE call counters:

- Controls allocation and table allocation/open/seek/tell/close failure hooks
- Output-prefix allocation/open/flush/seek/close and file-identity failure hooks
- Destructive numeric/scratch probes and ordered owner save/restore test exports

The controls/table hooks target the checkpoint owner blocks only, leaving
ordinary solver allocation and numerical expressions intact. The output identity
hook models failed file metadata retrieval. Fault wrappers call the unwrapped
operations when disarmed and do not recursively intercept themselves. No helper
is added to packaged libraries or advertised as a supported backend API.

Qualification runs the 8 OUT tests, 8 output-write tests, and the controls/table
fault-activation tests. It rejects skips, expected failures, unexpected successes,
or a changed 18-test inventory. This
includes corrupt snapshots rejected before resource callbacks, precommit rollback,
postcommit cleanup errors, malformed/digest-mismatched/aliased prefixes, preserved
retired output files, complete future outputs and fresh-process owner continuation.
`qualification.json` retains per-case evidence, counts, both library identities,
test/runtime input hashes, and the controlled environment; `tests.log` retains the
full unittest results. Build identities and all recorded inputs are checked again
after tests before success is reported.

This is an opt-in source-development qualification, not the default package suite
or a release gate requiring access to custom sources. Compiler-free tool integrity
regressions run with:

```sh
PYTHONPATH=src:tests python -B -m unittest test_checkpoint_test_build_tool
```

These historical owner probes intentionally retain their original path-bound
encoding (`logical_resources=0`). They do not certify every earlier owner suite,
the public ABI2 container/session contract, Windows, all coupled physics, or
release compatibility. Do not install, publish, register, or copy these destructive
test libraries into `src/easysewer/libs`.

`patch.py` is the shared exact-source transformation used by formal standard13
and custom11 preparation. It adds native owners and the six corrections below,
rejects repeated application, and records all recipe hashes. Both formal build
recipes verify this checkpoint profile before compiling. Built artifacts require
qualification before replacing installed libraries; version symbols alone do
not establish their provenance or backend capabilities.

`prepare.py` remains an explicit historical development route. Recipe 26 accepts
only independently qualified standard12/custom10 prepared trees, verifies every
file, then calls the shared transformation and writes `checkpoint-source.json`.
It does not create a production `prepared-source.json`. Factoring recipe 25 into
the shared transformation preserves every generated C/header byte for those old
baselines. Formal preparation starts from the pinned upstream trees and records
its new revision markers separately.

Earlier recipe 20 added LID state, 21 balance/statistics, 22 OUT and custom ponding
state, 23 output-prefix transactions, 24 the native coordinator, and 25 relocation
identities (ABI 2). Owner-level sections below describe their original isolated
test scope. Subsequent container, worker RPC, public Session and Runner tests are
separate evidence; no isolated owner test proves every complete model combination.

## Native coordinator and resource identities

`api.h` declares capture, validation, restoration and resource inventory calls.
`coordinator.inc` sequences every native owner, validates all state before any
provider callback, prepares replacements, applies the state, then independently
reports cleanup errors. Restore holds a private copy of caller bytes. Capture
copies into the caller's buffer only after the full write succeeds. A serialized
entry guard rejects checkpoint reentry; trusted callbacks must not call other
solver operations or mutate the model. Providers exchange paths, never `FILE*`
pointers across Windows C runtimes.

ABI 2 uses stable names for five input-stream roles and for ordered curve,
time-series and private climate table consumers. Rain-source bindings use the
ordered gage identity. Actual filesystem paths are not serialized in those
bindings. Model order, settings, engine identity and immutable input contents
must still be verified by the outer 32-byte execution binding: logical resource
names alone provide no content integrity. Several consumers may refer to copies
of the same file. Inventory paths describe the initially reconstructed inputs;
the outer coordinator retains the verified resource mapping after restoration.

The initial ABI 1 candidate retained absolute paths in climate, time-series and
routing bindings. New-process tests that removed the original directory exposed
six relocation failures per environment. ABI 2 rejects ABI 1 payloads rather
than silently treating different identity schemes as compatible. Standalone
historical owner probes explicitly keep their original path-bound encoding;
the versioned coordinator selects logical resources on every walk, including
the table staging pass.

`runtime/_checkpoint_native.py` adapts the new ABI, limits state allocation and
retains callback failures as structured diagnostics. Interrupts are reraised
only after native cleanup, with the commit result attached. This is still an
internal adapter: complete container integrity, worker RPC and public
Session/Runner save/resume are not implemented by it. The owner sections below
describe their original module scope; the native coordinator now combines them.

The private Python owner in `runtime/_checkpoint_worker.py` captures the real
FlexiblePonding policy's previous node values, per-node removal ledger and active
step counts, pollutant quantities, clock and step count. It binds the engine and
execution configuration, and stages a content-verified independent UTF-8 trace
prefix. Native and Python owners must all validate and stage before either is
committed. Successful worker boundaries are marked only after native result
finalization and all Python trace rows complete; failed steps, end and cleanup
cannot be captured. Staging cleanup retains the primary cause, and retired
trace close errors occur after commit. The coordinator owns private path cleanup.

This owner does not itself provide RPC or public orchestration. The current
checkpoint RPC distinguishes recoverable rejection from fatal or committed
failure. The verified container binds native/Python state and output prefixes;
Session and Runner reconstruct resource identities and coordinate publication.
Historical private owner tests exercise the actual policy with the native test
bundle, including new-process continuation. Public integration has separate
tests and still requires the declared full checkpoint acceptance matrix.

Climate reconstruction binds `Temp.fileStartDate` only when a climate file is
enabled. The baseline initializes this field only for `TEMPERATURE FILE`; an
inactive value can belong to a previous project. Its inactive wire binding is
therefore canonical zero. Fresh-process tests first run a climate-file project
in the capture process, then checkpoint a project without that file to check
that restoration does not depend on process history.

## LID numeric and report-cache owner

The new baseline has 13 LID-related structure types with 100 fields and 33 private
globals. Its numerical owner retains layer depths/moisture, previous fluxes,
drain flows, dry time, clogging volume/regeneration day, group flows and the seven
water-balance totals. Green-Ampt fields other than `Ks` are initialized only when
`Ks > 0`; inactive fields must not be read. Design parameters, ordered deployments,
drain targets and pollutant-removal identities bind reconstruction. The legacy
unit `botWidth` member is uninitialized and unused, so it is never inspected or
serialized. Swale geometry instead uses a local variable in `lidproc.c`. The
initial candidate incorrectly bound this member, causing intermittent capture
failure and fresh-process mismatch; those first-run failures are retained.

Detailed report `wasDry` and the initialized prefix of `results` persist across
steps. The block saves only the bounded initialized string prefix and restores
its terminator. Serializing the entire 256-byte array would capture its unused tail. The
dirty queue must already be drained at a successful public boundary; latched
report errors cannot be represented as a successful checkpoint. Stream handles,
diagnostic paths and linked-list addresses must be reconstructed, with report
prefixes/positions staged by the future output transaction. A logical resource
identity must permit relocation without binding the old physical directory.

`HasWetLids` already belongs to the runoff block; catchment step volumes have
subcatchment ownership. Tests poison LID per-call flux/volume variables, inactive
Green-Ampt fields and the unused unit member between steps, then compare full
OUT, normalized RPT and exact detailed reports. Separate tests destroy numeric
and report-cache state, restore every step, reject corrupted blocks before
opening resources, inject staging faults and reconstruct only this owner in a
fresh process. These are module tests: complete output/worker continuation and
whole-solver new-process resume remain unimplemented. Output streams and their
existing prefixes are not recreated by this block.

## Mass balance and global statistics

`massbal.inc` preserves runoff/groundwater totals, loading and routing mass,
current/old step rate histories, per-node inflow/outflow histories and the four
public continuity-error values. It binds the model's ordered identities, units,
pollutant conversion factors, process options and reconstructed total area.
Loading `finalLoad` and quality `finalStorage` already accumulate before end and
must survive. Other final storage/error fields are computed during finalization;
unused step storage and quality evaporation fields are not inspected.

The baseline's `massbal_getSysFlows` helper is uncalled. Supported output reads
current step rates directly, with the custom engine adding its separate ponding
contribution. This owner does not restore custom ponding state. Capturing after
end is prohibited: final reporting can accumulate final mass and convert units.

`stats.inc` preserves subcatchment, node, link, storage, outfall and pump
statistics, including nested pollutant load arrays, peak dates, flow turns,
step counts and system maxima. Subtype indexes and allocation presence are bound
to normal project reconstruction. Groundwater's nested statistics remain owned
by `groundwater.inc`. Step histogram intervals are reconstructed bindings; its
unused count slot zero is not read. Ranking arrays are rebuilt before reporting,
and the per-call system outfall flow is scratch.

Tests destroy both owners at every step and compare complete OUT and reports,
poison derived/scratch/unused fields, reject corrupt payloads before staging,
and demonstrate that omitting accumulated totals or statistics changes actual
outputs. Fresh-process probes reconstruct only these two owners; output-prefix
transactions, worker state and whole-solver continuation remain incomplete.

## OUT numeric and finalized custom ponding owners

`output.inc` binds the reconstructed binary header layout, units, report clock,
pollutant order and the complete object/report-index mapping. It preserves
`Nperiods`, `Nsteps` and the node/link average arrays, including the special
last-setting accumulator for non-conduit capacity. Each binary32 accumulator is
widened exactly to binary64 on the wire; non-finite, out-of-range or inexactly
representable input is rejected before application. This includes averages
captured in the middle of a reporting interval.

The three object result arrays and `SysResults` are scratch. Writers recompute
them, and saved-value queries reread the requested records. Public
`swmm_getSavedValue` is available only after end; it is not a live-step query.
`OutReady`, `OutFailed` and `OutEnded` constrain the valid boundary rather than
being restorable state. OUT stream ownership, bytes and positions are still
pending: numeric restoration alone must not advertise resumability.

`ponding.inc` preserves mode and current/previous discrete removal rates only
at a finalized custom split-step boundary. Pending adjustment cannot be
captured. The completed step's duration and deferred-statistics scratch are
replaced before next use. Node baselines/counters and pollutant removal totals
remain in the network owner; continuity and statistics remain in their own
owners. Python policy history and trace output are separate pending owners.

Whole-checkpoint qualification remains in progress. Tests for these owners do
not establish output-prefix transactions or fresh-process solver continuation.

## Wire and restoration rules

`writes.c` stages the six native output roles: main RPT, binary OUT, SAVE RUNOFF,
SAVE OUTFLOWS, each LID detailed report, and SAVE HOTSTART. HOTSTART opens and
writes a header during start, even though its body is emitted on close. Output
identities are stable roles and ordered LID deployment identities, not physical
paths. The writer inventory is reconstructed without changing any live owner.

Capture flushes and checks each regular file, then requires the physical byte
extent to equal the current writer position. All supported writers are at EOF
at the successful boundary. Text streams keep Windows text mode; binary streams
keep update mode for OUT seeks and the runoff count rewritten during close.

The trusted resource provider must verify immutable captured content and create
an independent private prefix file for each inventory index. Native preparation
opens it without creation/truncation, checks exact physical length and rejects
file-identity aliases of live outputs or other staged outputs (including hard
links). The provider owns temporary path cleanup, including files created before
it returns an error. Whole-container digests and production resource publication
remain pending; the length field alone is not an integrity checksum.

After every owner has validated and every resource has staged, application does
no I/O: handles and filenames are exchanged together. This is necessary because
runoff failure cleanup removes its recorded filename. LID names are allocated
before commit and retired names are freed afterward. Pre-commit failure retains
all live owners; post-commit close failure is reported separately. Tests use
private resource providers; this is not yet a public save/resume facility.

`codec.c` encodes fixed-width little-endian integers and IEEE binary64 numbers.
It never copies C structs, native padding, addresses, `FILE`, or `fpos_t` values.
The supported native builds require 8-bit bytes, 32-bit `int`, and binary64
`double`. All serialized doubles must be finite; signed zero is preserved.
Lengths and cursor arithmetic are checked. The first error is retained.

A future whole-checkpoint coordinator must:

1. Verify the complete container, digests, module inventory, and exact captured
   model/input/profile/resource/backend identities before opening output files.
2. Own immutable copies of the bytes it validates. Hold the reconstructed model
   fixed throughout validation and application; do not permit concurrent steps.
3. Walk **all** modules in `ES_CK_VALIDATE` mode and check complete consumption of
   every bounded module chunk. Validation must not change live native state.
4. Stage all module allocations and opened resources. Discard all staged owners
   if any stage fails, preserving the original session and output files.
5. Apply only after all validation and staging succeeds. `ES_CK_APPLY` is an
   internal commit pass over the exact same immutable bytes and unchanged
   layout. It is not a standalone hostile-input reader or rollback mechanism.

The blocks do not yet provide whole-container checks, a public session capture
protocol, worker state, production resource publication, or a public resume
operation. The private output-prefix transaction still requires whole-checkpoint
qualification. In particular, local rule/action binding is additional
defense, not a replacement for verifying the complete input program.

## RDII and routing input frames

`rdii.inc` captures the published binary32 node flows (losslessly widened to
binary64), current interval, previous frame date and logical EOF. Header type,
step, node order/identity and text units must match reconstructed input. Values
that cannot round-trip through binary32 are rejected. Binary RDII units are
already cfs; its unused text-unit global is deliberately not inspected.

`iface.inc` captures both old and new routing frame matrices, their date
bracket, previous frame date, interpolation fraction and logical EOF. Header
names and order, unknown node/pollutant columns, model mappings/units and file
size are bound to reconstruction. Unknown columns remain part of the frame.
Input arrays must already exist after successful project start. These blocks do
not restore allocations into arbitrary uninitialized projects.

The stream block separately restores physical offsets. Both numeric blocks and
the stream block must validate before staging resources or applying any state.
Pending frame buffers are scratch: every value is overwritten before publishing
a complete frame. RDII unit-hydrograph convolution, its history allocation and
generation totals are consumed before start returns; the workspace is freed and
generated immutable input bytes are owned by the resource container. Capturing
in the middle of that generation is outside the allowed boundary. Routing output
line accounting is still owned by the pending output-prefix implementation.

Tests poison published frames, dates and EOF, close input streams, restore, then
compare complete runs. Separate fresh-process tests compare future RDII and
routing flow/quality queries, including EOF, unknown columns and changing
frames; omitting the numeric blocks must change those queries. These prove the
two input owners only, not complete solver process continuation.

## Groundwater objects and statistics

`groundwater.inc` binds aquifer parameters, monthly evaporation pattern values,
subcatchment/node associations, local overrides and the ordered compiled lateral
and deep-flow expressions. It captures moisture, saturated depth, old/new flow,
evaporation loss, available infiltration volume and all nine nested statistics.
Arrays and expression lists must be reconstructed by normal project startup.
No native pointers or expression-node layouts are copied into the payload.

When a subcatchment has no pervious area, native initialization can produce a
non-finite, unused infiltration capacity. Capture binds that zero fraction and
omits only this unused value. The fixed model prevents the area from becoming
pervious between validation and apply. All serialized numeric values remain
finite. Groundwater's per-call ODE/flux work variables are independently tested
as scratch, including the aquifer copy and expression pointers.

The development recipe also fixes `getUpperPerc` leaving `HydCon` from a previous
ODE evaluation/subcatchment/project when percolation is zero. The documented
`K` variable is unsaturated conductivity, distinct from percolation `Fu`.
Compute it from the current moisture before the percolation early return;
preserve the existing percolation equation and threshold. An independent
analytical probe and explicit `KS * EXP((THETA-PHI)*slope)` expression oracle
check the correction, alongside dry-after-wet project runs. This is a fifth
candidate correction and is not yet installed in the production libraries.

Fresh-process groundwater tests reconstruct only this owner and its statistics.
Outputs, custom ponding and worker restoration still have to be completed before
a full solver checkpoint can be exposed. LID and global mass balance/statistics
now have separate internal owners described above.

## Snowpack histories and seasonal coefficients

`snow.inc` captures all seven snowpack history arrays and the daily `dhm` melt
coefficients. Snowmelt parameter sets, removal destinations, surface fractions,
subcatchment area and LID area are fixed bindings. Restoring climate's day cursor
alone would skip recomputing today's coefficients, so they must survive too.
The new-snow depletion history (`sba`, `awe`, `sbws`) is retained even between
snowfall and thaw. The climate block owns removed snow volume and season; the
subcatchment block owns old/new snow depth. No state is owned twice.

The development recipe initializes `sba`, `sbws` and `imelt` on every snow
surface. It also clears every `imelt` slot before each plowing calculation,
including surfaces with zero area. Previously those slots could retain
uninitialized memory, enter net-precipitation arithmetic and contaminate the
combined impervious precipitation: multiplying NaN by zero does not remove it.
The independent baseline probe injects an explicit allocation sentinel; it
demonstrates this read path rather than claiming every ordinary run fails.

The snow tests check that native initialization overwrites the sentinel and
that per-step immediate-melt poisoning leaves complete results unchanged for
zero, partial and fully impervious/plowable areas. This sixth candidate fix is
not installed. Combined tests destroy all implemented owners before restoring;
separate omission tests remove water, thermal, depletion-history or seasonal
state and require a change in future complete results. New-process tests restore
only the snow owner. Full solver continuation remains unimplemented.

## Controls owner

The block walks both THEN and ELSE action lists in their native order. It
preserves mutable curve/time-series values, PID values and both previous errors,
including inactive or lower-priority actions. Numeric action values, PID
coefficients, rule IDs/priorities and action identities are checked against the
reconstructed configuration. Non-PID coefficient/error fields are uninitialized
in SWMM and are deliberately never read.

`ControlValue` and `SetPoint` must survive: time EQ/NE comparisons and MISSING
premises can leave their earlier values intact. The most recent evaluation date
and time are also retained. Global elapsed time and the routing rule clock are
handled by the engine/routing blocks below; link settings and table/rain histories
still need their separate state modules.

The action list has no pending work after a successful complete evaluation, but
its **allocation count** affects subsequent distinct-link execution and report
order. It is encoded with a bound of the number of links. Restoration stages a
replacement list of that size with null action pointers before changing live
state. The next evaluation clears/reuses it exactly as a continuous run would.
Repeated restoration transfers ownership once; failed staging frees only the
new list. Parser cursors, symbolic bindings and expression trees are rebuilt
from the identical captured input.

## Engine and routing clocks

`engine.inc` owns capture-boundary checks and serializes the independent runoff,
routing, report and elapsed clocks. It also stores routing/statistics counters,
warnings, and both `RouteStep` and `CourantFactor` (the public routing-step setter
changes both). Counters use a nonnegative 64-bit wire value with explicit
destination `LONG_MAX` checks; the Windows/Linux difference cannot truncate one.
Calendar, duration, output mode and effective step configuration are bound to the
reconstructed project. Output frame count (`Nperiods`) is reserved for the output
module, where it must be bound to actual file layout and position.

The capture gate rejects closed, unstarted, failed or exception-recovered
sessions, an unfinished stride, and a custom `PondingPending` step. The caller
must still serialize native calls: this is not a concurrent snapshot mechanism.
Successful custom capture occurs after external adjustments and `saveResults`.
Runoff can be ahead of routing. Custom `ElapsedTime` may still contain the last
control-evaluation time; it is not recomputed from routing on restore. Initial
`OldRunoffTime` can contain an unused previous-owner value, so its ordering check
applies after runoff advances. The candidate resets `OldRoutingTime` at start
and advances it with routing disabled as well.

`routing.inc` binds reconstructed sorted link order and sorted/overlap-trimmed
events, including the terminal event sentinel. It preserves `NextEvent`,
`BetweenEvents` and `NewRuleTime`. An inactive routing process has no routing
state payload and does not inspect old allocations. Restoring a future rule time,
an invalid event index, counter overflow or a malformed later controls block is
rejected before any clock is applied or any live control list is replaced.

The development recipe also fixes an independently reproduced scheduling defect
in standard11/custom9: while between events, an early `return fixedStep` bypasses
the common `RULE_STEP` cap. A nondivisible step can miss a rule boundary and later
produce a negative step (error 107). The candidate chooses `routingStep = fixedStep`
and continues through that existing cap. This correction is still in development
builds; it must be incorporated and qualified in production recipes before release.

A second independently reproduced defect affects inactive routing after a prior
project. The old routing clock is neither initialized nor advanced in that path.
When a report lands on the stale value, output interpolation divides by zero and
the checked output writer rejects nonfinite results with error 309. The candidate
initializes the old clock and advances both clock ends in the inactive branch.
This fix also remains outside the installed standard11/custom9 libraries and
requires production qualification before release.

## Dynamic-wave owner

`dynwave.inc` preserves `VariableStep` and each node's `Xnode.oldSurfArea` and
`Xnode.dYdT`. The first distinguishes the initial minimum-step sentinel from an
ongoing variable-step run. The latter two carry the last non-ponded surface area
into the near-crown surcharge calculation and the last depth rate into selection
of the next stable step. All are finite and nonnegative. `VariableStep` may exceed
a newly changed `RouteStep`; the module must not reject that valid history.

The block binds the active process, routing method, ordered node identities/types
and crown elevations, and effective dynamic-wave parameters. Inactive routing,
steady flow and kinematic wave have no Xnode payload and never dereference that
allocation. The reconstructed solver owns its allocation; no pointers or native
struct layout enter the wire representation.

`Steps`, `Omega`, and the extended node's convergence flag, current surface area
and current derivative sum are scratch. They are overwritten before the next
solve consumes them. The tests poison these independently and require unchanged
subsequent traces, output and reports. Statistics already computed from the
completed iteration require their own future checkpoint owner.

This owner does not capture `Node`, `Link`, `Conduit`, storage exfiltration,
regulator state, mass balance, statistics, hydrology or output resources. Its
tests keep those alive. Reconstructing a complete hydraulic solution in a new
process remains a separate requirement.

## Numeric network owner

`network.inc` walks the initialized Node/Link objects, conduit history,
storage residence/loss values and both bottom/bank Green-Ampt states, mutable
outfall stage/type and routed volume/quality, and orifice/weir setting-derived
coefficients. It preserves old/new quality arrays and link total loads, ordered
by bound pollutant identities and units. Custom builds also retain node ponding
values and cumulative removed volume/mass. Other native/Python ponding owners
still require separate modules.

Numeric objects and quality arrays are calloc-backed. Green-Ampt allocations
receive all parameter/state assignments before capture. Negative signed flows,
Green-Ampt recovery `F` and countdown `T` are valid and are not clamped. All
stored doubles must be finite; flags/enums are bounded. Existing reconstructed
arrays are used without replacing pointers or allocating during application.

Outfall type and fixed stage are mutable through the public setter, so they are
stored, not configuration-bound. For FILLED_CIRCULAR, geometry helpers add and
subtract `yBot`/`aBot` in place. The saved positive `yFull`/`aFull` values preserve
their exact floating representation; reconstructing the original geometry need
not undo floating addition/subtraction bit for bit. Other geometry and subtype
configuration is rebuilt from identical captured input, with local bindings as
additional checks rather than a substitute for the future complete container.

The development recipe adds PARABOLOID (input keyword PARABOLIC) to the analytical
storage exfiltration initialization branch. The baseline omits this shape,
leaving four malloc-backed geometry fields uninitialized. A diagnostic using
defined sentinels before `exfil_initState` proves the original function leaves
all four untouched; the candidate initializes bottom area and minimum bank depth
to zero and bank maxima to BIG. This correction still needs integration and
qualification in the production recipes.

The network module does not capture street-inlet private state (covered by the
separate inlet owner below), infiltration module globals, other hydrology,
table/file cursors, mass balance/statistics/output,
private native ponding state or worker ledgers. Combined restore tests keep these
owners alive. Full new-process continuation is not implemented.

## Inlet owner and treatment scratch

`inlet.inc` retains each valid deployed inlet's current capture and backflow,
three period counters and four cumulative/peak statistics. The native
`initInletStats` initializes all of these even before the first routing step.
Counter sums are checked in 64-bit arithmetic; integer overflow, nonfinite
numbers and impossible statistics are rejected. Existing objects are restored
without replacing their pointers. The full bundle validates the last inlet
chunk before staging controls or applying any preceding module.

Reconstructed design and usage parameters, flow factors, backflow ratios,
ordered link/node/design identities and linked-list order are bound. Invalid
placements removed by native validation are absent from the saved list; an
empty list can coexist with `UsesInlets` being true. Design variants are
calloc-backed, including their unused fields. Full input and resource identity
still belongs to the future whole-container coordinator.

`InletFlow` is zeroed before each capture pass. The HEC-22 geometry globals,
including `xsect`, are scratch selected for the current conduit before use.
This classification requires the candidate's on-sag ordering correction:
the original computes `totalInlets` using the preceding `Nsides` before calling
`getConduitGeometry`. The candidate moves that multiplication after geometry
selection. This fourth native correction also requires production qualification;
it is not yet in the installed standard11/custom9 libraries.
`qualrout.c` has no mutable module globals. Treatment's `Cin` is set for every
pollutant before each node treatment; `ErrCode`, `J`, `Dt`, `Q`, `V` and every
`R` entry are reset by that treatment. They are excluded from the checkpoint.
Expression trees are rebuilt from input; persistent node/link quality and
storage HRT belong to the network owner, and mass balance remains a future owner.

The inlet tests poison scratch independently, including treatment state, then
compare complete output, normalized reports and traces. Clearing inlet history
must change the final street report even when hydraulics and OUT are unchanged.
Other process owners remain live: these tests do not prove new-process resume.

## Infiltration and subcatchment owners

`infiltration.inc` walks only the active member of each subcatchment's native
infiltration union. It binds the method, input parameters and derived constants,
then preserves Horton/modified-Horton time and cumulative infiltration,
Green-Ampt/modified-Green-Ampt deficit, cumulative/upper-zone water, recovery
timer and saturation flag, or all six Curve Number history values. Missing
infiltration has no payload. Signed recovery values remain valid. Native union
padding, inactive members and C pointers never enter the representation.

`subcatchment.inc` preserves three subareas' inflow, runoff and ponded depth,
catchment rainfall/loss/runon, old/new runoff and aggregate snow depth, old/new
quality, ponded mass and cumulative load. Each land-use factor retains pollutant
buildup and its last sweeping date. Geometry, area fractions, routing choices,
patterns, input loadings, constituent identities/units and resource-presence
bindings are reconstructed from the identical input. All arrays are restored in
place after validation; no new allocation or ownership transfer is needed.

The two infiltration globals (`Fumax`, `InfilFactor`), subcatchment's three
ODE/parameter scratch values and ten per-step volume globals are overwritten
before use at a complete engine boundary. Tests poison them independently and
as part of the combined restore. `surfqual.c` and `landuse.c` have no extra
mutable module globals; their persistent buildup/washoff data lives in the
subcatchment and mass-balance owners. The generic ODE workspace is audited
separately below.

The tests include all five infiltration methods, a mixed-method project,
subarea and catchment-to-catchment routing, month-boundary adjustments, US/SI,
all eight LID types, groundwater, snowmelt, buildup/washoff variants, storage
exfiltration, stride and custom split steps. Omission tests require observable
result changes for each infiltration method and for surface, quality, buildup
and sweeping history. Invalid later chunks cannot apply any earlier block.

These owners do not capture the rain/climate/table/file cursors, private runoff
step-selection state, groundwater/snow/LID objects, general statistics, mass
balance, output or worker state. Those remain live during these component tests.
Aggregate snow depth is not a replacement for the full snowpack state.

## Runoff scheduling, replay and ODE workspace

`runoff.inc` binds the active process, file mode, units and object counts. An
inactive process has no state payload and does not inspect old allocations.
It retains `HasRunoff`, `HasSnow` and `HasWetLids`: the previous runoff step's
flags choose the next wet/dry step before they are recalculated. `Nsteps` is
preserved for SAVE's final frame-count header and USE's EOF/first-groundwater-frame
semantics. USE additionally binds the reconstructed `MaxSteps` count. File layout,
`MaxStepsPos` and buffer capacity are checked against reconstructed configuration.

USE's independent rainfall consumer retains `ReplayRainTime` and every gage's
unadjusted `ReplayRawRain`. The active flag is reconstructed and bound; allocation
addresses are never serialized. The consumer time is bounded by the simulation
duration. These values cannot be reconstructed from current gage rainfall after
monthly factors (especially zero factors) or from a future runoff-cache frame.

`IsRaining` is reset before the next step selection. `OutflowLoad` is zeroed before
each catchment's washoff. `RunoffFrame` is fully filled by each successful read or
save before consumption. These are scratch. The six ODE arrays `y`, `yscal`,
`yerr`, `ytemp`, `dydx` and `ak` are also overwritten before use in each integration;
`nmax` and their allocations are rebuilt by `odesolve_open(MAXODES)`. Adaptive
step sizes are local to each integration. The one-dimensional subarea derivative
and both groundwater derivatives assign every requested component on every path.
Test-only counters prove both integration dimensions execute while all arrays are
poisoned with NaNs. Production ODE source is unchanged.

Combined tests cover wet/dry transitions, cold snow cover, all eight LID types,
groundwater, zero-area and inactive runoff, disabled routing, SAVE and USE files,
independent rain replay across month boundaries, file rainfall, API overrides,
US/SI, stride and custom split steps. Missing each of the six persistent groups
must alter the subsequent solution, time-step history, rain-control actions or
saved cache. Scratch poisoning must preserve the full result. Corruption in this
last block and control-stage allocation failure must leave all earlier owners
unchanged.

The sequential runoff file cursor, EOF state and saved-output prefix remain the
future resource owner's responsibility. The tests preserve those live resources,
as well as other unimplemented hydrology/statistics/worker owners. Applying this
numerical block alone is not file restoration or new-process continuation.

## Climate numerical owner

`climate.inc` binds temperature/wind/evaporation sources, unit-dependent constants,
monthly schedules and adjustments, snowmelt parameters and the climate-file mode.
It preserves current public climate values, snow season/removal totals, the last
daily update, evaporation event date/rate and the independent last climate update.
File temperature additionally retains the prior maximum and all diurnal curve
parameters; reconstructing today's minimum/maximum does not recover that history.

Temperature-based evaporation saves the moving-average count/front, averages and
only the filled entries of its seven-day window. Unfilled slots may contain an
earlier project's data and must not be inspected. Calendar-file state includes
the current day/month cache, carried daily values, elapsed days, EOF/order markers
and the already parsed lookahead row for a future month. Calendar dates, month
lengths and the file-start/elapsed-day relationship are checked before application.
GHCND column layout and units/wind selection are bound to the reconstructed file
header. No address or native file handle is encoded.

The parser consumes `ClPending` during `clStart`; nonzero pending state is refused
at an engine capture boundary. `FileLine` is then scratch and is replaced before
the next parsed row. Scratch tests also poison unused moving-average slots.
`ClTempSeries` and `ClEvapSeries` are independently owned table cursors, not scratch.
Their reconstruction/staging and physical stream positions require the upcoming
table/resource owners; they remain alive in this module's tests. The climate-file
handle and position are also still live. This is not a new-process resume API.

## Table cursors and staged read streams

`tables.inc` covers project curves/time series and the active private temperature
and evaporation consumers exposed by `climate_tables.inc`. Entry lists are
reconstructed and bound by ordered values; mutable `thisEntry` is an ordinal,
never an address. Sequential date continuation/previous timestamps and independent
interpolation brackets, EOF flags and optional owned lookup cursors are retained.
Inactive private climate structs are not inspected: they can contain borrowed
pointers from an earlier project. TFile.state has no table consumer and is not
serialized.

Binary input positions are nonnegative 64-bit offsets with destination and file
extent checks. Capturing a position only calls tell; it does not seek or consume
the live stream. Validation parses every table without opening resources or
modifying live state. After whole-container validation, preparation allocates
replacement cursor nodes, opens independent streams and positions them. Failed
preparation leaves only staged owners for disposal. Applying the prepared block
swaps owners without allocation or I/O; all old streams/lookups become retired
owners, closed and freed by `es_ck_tables_discard`.

Each attempt uses a fresh zero-initialized `EsCkTablesStage`, immutable validated
bytes and a fixed reconstructed model. The resource provider is a required trust
boundary: it must resolve captured, content-verified immutable resources, not
merely reopen an unverified path. No production provider or complete checkpoint
container is supplied by this module. The Python test provider checks original
bytes against its captured resource inventory, then opens independent immutable
copies. A changed or missing resource refuses preparation.

Cleanup returns a separate error. Before commit it is secondary to the original
prepare failure; after commit it reports retirement failure, **not** a failed
restore with the original session intact. The coordinator must preserve this
distinction. Closing a read stream can fail, so it cannot be hidden inside an
allegedly infallible commit or used to promise rollback.

Tests destroy lookup allocations and close table streams before each restore,
then compare complete subsequent output/report/trace data. Separate tests rebuild
the table block after closing a project and in a fresh process, including EOF
and backward queries followed by independent sequential reads. These prove the
table block can reconstruct its owners; other numerical, climate-file, runoff,
statistics, output and worker owners are still outside that fresh-process test.
They do not establish whole-solver continuation. Allocation/open/seek/tell/close
faults exercise staged cleanup and committed-retirement reporting, with stable
handle counts and successful subsequent retries.

## Other post-start input streams

`streams.inc` covers climate calendar input, the binary rainfall interface,
runoff USE, RDII input and routing inflows. Activity is bound to the reconstructed
process and mode. Rainfall/RDII SCRATCH and SAVE files have finished generation
before a successful start boundary; their subsequent consumers only read.
Replacing those handles with independent binary read streams does not repeat
generation or overwrite the published SAVE file. Runoff SAVE and all other
output streams are excluded and require an output-prefix transaction.

The block preserves 64-bit physical positions and uses the same staged read
helpers as tables. It binds role/mode/activity and caller-file identity. Generated
scratch files use `swmm:rain-scratch` and `swmm:rdii-scratch` resource identities,
because their actual temporary names change on reconstruction. The verified
resource provider must bind those roles to captured content. The engine retains
its own current scratch filename for normal cleanup; retiring a handle does not
delete that path or a provider-owned resource copy.

All numeric/table/stream blocks validate before either resource stage is prepared.
Both stages must succeed before any numeric state or handle is committed. Shared
64-bit position/extent checks reject invalid offsets; validation itself does not
open or move streams. Climate/runoff numeric blocks require a live stream during
capture, while restoration permits the destination handle to be absent until
the corresponding staged stream is committed. Their layout and numeric checks
remain active. Close failures retain the separate cleanup semantics described
above.

FILE's implementation-specific EOF/buffering representation is not serialized.
Each consumer's logical EOF/date/frame state belongs to its numeric owner, and
binary seeking reconstructs the byte position. Climate and runoff numerical
owners already exist; RDII and routing-interface private frames/flags still need
their own blocks. Tests retain those latter states, destroy all implemented
table/input handles, and compare complete subsequent results. Separate fresh-
process tests rebuild only the stream block and verify captured-content digests,
positions and following bytes, including regenerated scratch names. This is not
whole-process continuation. The production container/provider, remaining process
owners, output prefixes and worker state remain pending.

## Qualification

### Rain gage owner

`gage.inc` preserves the current/next rain interval, cumulative reading, current,
next, API and reported rainfall, all 49 hourly-history bins and their partial
interval counter. The counter can equal 3600 at a normal runoff boundary; it is
not restricted to values strictly below one hour. Missing-date and API sentinels
are finite native values and retain their exact representation.

Gage identity, source kind, table binding, interval/type/units, snow factor,
conversion factor, shared-gage binding and usage are reconstructed configuration.
For archive rainfall, the source path/station/date range and checked interface
record extent are additionally bound. The current logical record offset is saved
as an unsigned 64-bit wire value with native `LONG_MAX`, extent and 12-byte
alignment checks. The qualified reader explicitly seeks to this offset for each
read. This block does not own or reopen the rain file, verify its bytes, or restore
a time-series cursor; those are separate resource/table responsibilities.

`isCurrent` is RDII scratch, reset for every RDII rainfall time step. It is not
serialized. `OneSecond` is constant; `gage.c` has no mutable module globals.
All gage objects, including inactive fields, originate from zeroed allocation.

The combined tests destroy and restore these values alongside all preceding
owners. They cover all three rain forms, cumulative resets, shared and unused
gages, late records and EOF, ignored rain, API overrides, US/SI, month changes,
file time series, historical rainfall files, runoff replay, all 48 history windows,
stride and custom split steps. Separate omission cases must change scientific
output or control actions. Truncation, binding/offset corruption, invalid counters,
nonfinite values and allocation failure must leave the whole bundle unchanged.
The remaining table, runoff, RDII and physical file owners stay alive in these
tests; this is still not complete new-process continuation.

`tests/test_native_v2_checkpoint_controls.py` requires separate instrumented
standard and custom libraries selected by `EASYSEWER_CHECKPOINT_STANDARD` and
`EASYSEWER_CHECKPOINT_CUSTOM`. `tests/native_checkpoint_controls.inc` is appended
only to these test builds; its exports are absent from production recipes.
Allocation failure instrumentation replaces exactly one stage allocation call;
linker wrapping alone does not reliably intercept MinGW's imported allocator.

The tests cover corrupt/truncated/extended blocks, mismatched counts and
identities, nonfinite numbers, signed zero, cursor overflow, staged allocation
failures, repeated restoration, both action branches, priority conflicts and
several RULE_STEP intervals. They also poison/reconstitute control state at many
boundaries within a real native run and compare every output byte, complete
sampled native history and complete RPT bytes except wall-clock timing lines.

`tests/test_native_v2_checkpoint_clocks.py` additionally combines clock and control
state. It covers hydrology leading routing, event transitions and the sentinel,
average/late-start reports, US/SI, inactive routing, live routing-step changes,
`stride`, custom split steps, lifecycle refusal and native counter width. All
module validation and control allocation staging precedes the combined commit.
The split-step test uses the engine's actual floating total duration, not a
rounded wall-clock duration, when deciding whether another step is allowed.

Those comparisons prove this component's behavior while other solver state
remains live. They do **not** prove full process restart, hydrology continuation,
cross-platform solver restart or FlexiblePonding worker/trace restoration.
Whole-checkpoint qualification must add those independently.

`tests/test_native_v2_checkpoint_dynwave.py` and the test-only
`native_checkpoint_dynwave.inc` / `native_checkpoint_hydraulics.inc` extend the
combined clock/control bundle. They test per-step restoration, variable and
fixed steps, EXTRAN/SLOT, ponding, event transitions, inactive/SF/KW routing,
stride and custom split-step boundaries. Corruption in the final dynamic-wave
block must leave earlier clocks and controls untouched, including after a
failure to stage controls. Omission tests must demonstrate that forgetting each
persistent dynamic-wave field changes the subsequent solution.

`test_native_v2_checkpoint_network.py` extends the bundle without changing the
smaller bundles' test interfaces. Complete-output comparisons cover the network
and storage variants, quality/treatment, return flows, control-driven regulators,
live inflow/outfall setters, stride and split-step operation. Actual discrete
ponding removal exercises nonzero custom mass/volume ledgers. Corrupt final
network blocks and failed control staging must leave the entire bundle unchanged.
Independent omission cases clear hydraulic, quality, exfiltration, conduit and
regulator histories to verify that the fixtures detect missing state.
