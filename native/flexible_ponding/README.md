# FlexiblePonding native candidate

Native revision 14 adds shared `../standard/horton_outfall.py`: Modified Horton
constant-rate/zero-decay state handling and the outfall gate reader with a
route-to token. The build records `horton_outfall` revision 1 and exports
`swmm_getEasySewerHortonState()` / `swmm_getEasySewerOutfallGate()`, both returning
1. The numerical identity ends with `incremental-volume:2:native:14`; accounting
ABI 201 and checkpoint layout 2 remain unchanged. The packaged runtime uses native revision 14.
See [current limits](../../docs/2.0-known-issues.md).

Native revision 13 adds the shared `../standard/horton.py` correction:
Modified Horton excess infiltration state uses `MIN(Fe,Fmax)` at its finite
upper bound. Build records include `horton_capacity` revision 1 and its recipe
digest; `swmm_getEasySewerHortonCapacity()` returns 1. The accounting ABI 201,
ponding removal policy 2 and checkpoint ABI 2 remain unchanged. The complete
new backend numerical identity ends with `incremental-volume:2:native:13`.

Native I/O revision 12 applies the [shared path I/O recipe](../path_io/README.md),
with checked 4095-byte paths, pool-owned names, physical INP records and explicit
scratch stream ownership. Climate consumers close before the name pool is freed.
The custom accounting ABI remains 201 and checkpoint ABI remains 2. Build
preparation pins every helper digest and refuses stale path profiles.

The source identity is recorded in `source.json`: EasySewerSWMM commit
`378560b3accb0e7159e97f66351cfbc67b7026bc` (SWMM engine 5.2.4).
The manifest checks all 80 solver source/header files after CRLF-to-LF
normalization. Repository/commit labels alone are not used as proof of bytes.
The original checkout is read-only during preparation.

`prepare.py` verifies those bytes, then writes a separate source tree with:

* Complete native checkpoint ABI 2 and the six shared numerical corrections,
  applied through `../checkpoint/patch.py`. Checkpoint recipe hashes and all
  corrected source hashes are part of formal preparation; a stale checkpoint
  profile is rejected by build. The Python policy/trace state is coordinated by
  Session and Runner, not serialized by the C library alone.
* An actual routing clock independent of control-rule evaluation intervals.
* Explicit `swmm_getFlexiblePondingAbi() == 201` identification.
* A bounds-checked absolute input path. On Linux, `realpath` allocates its result
  before it is copied to SWMM's smaller fixed buffer. Windows checks the required
  size returned by `GetFullPathNameA`.
* Discrete external water/quality removal and post-adjustment node statistics,
  applied by `accounting.py` and the added `accounting.c` implementation.
* Checked route/finalize sequencing, pollutant identities and concentrations,
  actual routing intervals and node-statistics accessors.
* Checked HOTSTART scalar reads/writes and close errors, read-only input,
  idempotent close, climate/scratch handle ownership and replaced TREATMENT
  expression release. Shared exact-source patches in `../standard` explicitly
  verify the custom source's already-corrected buildup write count.
* `swmm_getEasySewerNativeIOFixes() == 13`, separate from the unchanged ABI 201
  and discrete water/quality accounting policy.
* Shared RAIN header/payload, runtime read and SAVE write checks, read-only USE,
  deterministic station padding, and bounded historical rainfall text parsing.
  Read/write failures report 322/324 and preserve a previous primary error.
  [RAIN boundary details](../../docs/2.0-native-io.md) apply to both families.
* Shared [RUNOFF revision 3](../../docs/2.0-native-io.md) validates every frame,
  buffers runtime state updates, opens USE read-only and checks SAVE/backpatch
  and close failures. Partial startup stops before later HOTSTART outputs open.

The prepared tree contains the 80 checked source/header files plus the added
`easysewer_ponding.c` plus four checkpoint source/header files. Patch anchors are checked before replacement. Accounting
remains active when binary output or report tables are disabled. Ordinary
overflow uses the standard integration factor; no external-flow clipping is used.

The destination must be empty and outside the source checkout. A mismatch aborts
before source files are copied. `prepared-source.json` records every patched hash
and the hashes of the local and shared patch recipes.

```powershell
python -B native/flexible_ponding/prepare.py `
  --source D:/CodeProjects/CProjects/2025/EasySewerSWMM `
  --destination D:/tmp/flexible-build/source
```

`build.py` uses an explicitly selected GCC-compatible compiler. All artifacts
stay beside the requested output. It validates the prepared hashes, compiles in
sorted order without fast-math or floating-point contraction, and records the
full command, compiler version, source/recipe hashes, library hash, ABI and I/O
revision. Windows uses MinGW with static runtime linking, no PE timestamp and a
fixed preferred image base (`0x180000000`); ASLR remains enabled. Linux uses PIC
and OpenMP. Independent output directories reproduce each platform's bytes
with the qualified toolchain.
No compiler is installed and no system configuration is changed by these scripts.

```sh
python3 -B native/flexible_ponding/build.py \
  --source /mnt/d/tmp/flexible-build/source \
  --compiler /usr/bin/gcc --target linux \
  --output /mnt/d/tmp/flexible-build/linux/flexible_ponding.so

python3 -B native/flexible_ponding/build.py \
  --source /mnt/d/tmp/flexible-build/source \
  --compiler /path/to/x86_64-w64-mingw32-gcc-posix --target windows \
  --output /mnt/d/tmp/flexible-build/windows/flexible_ponding.dll
```

Inspect the build log and run the custom backend tests before packaging a build.
The source distribution includes these scripts and manifest; the wheel contains
both custom libraries and adjacent build JSON. Its library digest is pinned by
the default backend. An explicitly selected external library is checked for ABI
and engine version and records its actual digest; callers may also require a
specific SHA-256. These records are provenance evidence, not signatures or a
claim of bit-for-bit reproduction across compilers/platforms.

The candidate Linux build was exercised on Ubuntu 24.04 under WSL x86_64. Its
imports require `libm.so.6`, `libc.so.6` (including `GLIBC_2.29`) and `libgomp.so.1`
(including `GOMP_4.0`/`OMP_1.0`). The Windows x86_64 build imports `KERNEL32.dll`
and `msvcrt.dll`. These observations do not replace the remaining supported
platform and older-runtime release matrix.

The standard SWMM build is documented separately in [../standard](../standard/README.md).
The shared I/O/lifecycle changes are qualified against each library family.
Only known custom binary hashes advertise `easysewer:native-io-fixes:10`; an
unknown user library's ABI number does not establish that patch capability.
Remaining native I/O and calendar audits are separate work. The
independent closed-node water and
pollutant balances, ordinary-overflow comparison, actual-step accounting and
post-adjustment statistics are described in
[the candidate backend guide](../../docs/2.0-flexible-ponding.md). That scope does
not certify every combination of SWMM processes or every distribution platform.

The known builds also provide `easysewer:runoff-physics:1`, independently of
ABI 201, ponding policy 2 and native I/O revision 3. The shared RUNOFF correction
and its numerical/partial-state limits are described in
[replay semantics](../../docs/2.0-runoff-replay.md). The build record pins
`runoff_physics: 1`; an unknown external library receives no such capability.

The shared `runoff_rain.c` fragment separately provides
`swmm_getEasySewerRunoffRainClock() == 1`, build-record `runoff_rain_clock: 1`,
and known-hash capability `easysewer:runoff-rain-clock:1`. Replay advances
rain reports and hourly rain controls by the consumer clock. This does not
change ABI 201, native I/O revision 3 or the ponding policy. See
[replay semantics](../../docs/2.0-runoff-replay.md) for the exact behavior.

Native I/O revision 4 adds the shared RDII boundary without changing ABI 201
or ponding policy 2. Build records include `rdii_io: 1`, and known hashes
advertise `easysewer:rdii-io:1`. It checks complete binary/text frames and
production arithmetic, propagates read/write/close errors, and requires an
explicit gage when generating used hydrographs. See the
[RDII boundary guide](../../docs/2.0-native-io.md).

Native I/O revision 5 adds the shared routing boundary, full matrix
initialization and file-alias protection while retaining ABI 201 and policy 2.
It records `routing_io: 1` and exports `swmm_getEasySewerRoutingIO() == 1`.
Qualified hashes advertise `easysewer:routing-io:1`; see the
[routing boundary guide](../../docs/2.0-native-io.md).

Native I/O revision 6 adds shared checked solver OUT writes, report/saved-value
reads and completion ownership, retaining ABI 201 and policy 2. Build records
include `solver_output_io: 1`; the export is
`swmm_getEasySewerSolverOutputIO() == 1`. Qualified hashes advertise
`easysewer:solver-output-io:1`; see the
[solver OUT guide](../../docs/2.0-native-io.md).

Native I/O revision 7 adds shared climate stream and calendar corrections,
retaining ABI 201 and ponding policy 2. It exports
`swmm_getEasySewerClimateIO() == 1`, records `climate_io: 1` and grants
`easysewer:climate-io:1` only for qualified hashes. See the
[climate boundary guide](../../docs/2.0-native-io.md) for record formats,
time-series traversal, failure handling and remaining scope.

Native I/O revision 8 adds shared external time-series records and independent
sequential/interpolation cursors, retaining ABI 201 and ponding policy 2. It
exports `swmm_getEasySewerTimeSeriesIO() == 1` and records `timeseries_io: 1`.
Qualified hashes advertise `easysewer:timeseries-io:1`; see the
[time-series I/O guide](../../docs/2.0-native-io.md).

Native I/O revision 9 adds checked main RPT writes/flush/close, error 306, and early-failure
project ownership cleanup. It exports `swmm_getEasySewerReportIO() == 1`, records
`report_io: 1`, and advertises `easysewer:report-io:1` only for qualified binary
hashes. See [report I/O](../../docs/2.0-native-io.md).

Native I/O revision 10 adds checked detailed LID report formatting, writes, flush and close,
plus safe project teardown after failed LID initialization. The shared patch
exports `swmm_getEasySewerLidReportIO() == 1`, records `lid_report_io: 1`, and
advertises `easysewer:lid-report-io:1` only for qualified binary hashes.
See [LID report I/O](../../docs/2.0-native-io.md).
