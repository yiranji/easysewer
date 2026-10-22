# Standard SWMM native candidate

`source.json` pins EPA SWMM tag `v5.2.4`, commit
`7952ca837988b1c32f791812eccc9fd64547e093`, and the SHA-256 of 82 source/build
files after CRLF-to-LF normalization. The source is available from the
[EPA repository](https://github.com/USEPA/Stormwater-Management-Model/tree/v5.2.4).
The original checkout stays unchanged. This is a separately identified EasySewer
build of the standard equations, with the corrections below.

The 2.1.0 core native targets are Windows x86-64, Linux x86-64, Linux
aarch64 and macOS arm64, all with 64-bit Python. Existing compiled x86-64
libraries are preserved before dependency repair. ARM artifacts are built from
the same public pinned EPA source and existing revision-16 corrections; no new
solver equations or fast-math policy are introduced.

Final wheels are platform-specific and bundle non-system dependencies. Linux
x86-64 retains glibc 2.33 as its minimum and uses the verified PEP 600
`manylinux_2_33_x86_64` wheel tag; Linux aarch64 targets `manylinux_2_28_aarch64`. macOS arm64 targets macOS
14.0 and bundles source-built, pinned LLVM OpenMP. The raw legacy Linux x86-64
solver still imports `libgomp.so.1`, `libm.so.6` and `libc.so.6` (including
`GLIBC_2.33`); final repair bundles the compatible OpenMP runtime rather than
requiring users to install it separately. These 2.1.0 artifacts do not modify
the immutable PyPI `2.0.1` release. See
[installation and doctor](../../docs/2.0-installation.md) and
[build/qualification gates](../../docs/2.0-testing.md).

`prepare.py` checks every source digest and patch anchor before writing a separate
empty destination. The prepared manifest records the original identity, recipe
digests, patch `easysewer:standard:5.2.4:16`, and all resulting source digests.

Revision 16 adds `horton_outfall.py`: Modified Horton constant-rate/zero-decay
parameters use the existing state and capacity branches, and an outfall gate is
read when a route-to token follows it. The build records `horton_outfall` revision
1 and exports `swmm_getEasySewerHortonState()` and
`swmm_getEasySewerOutfallGate()`, both returning 1. Checkpoint layouts are unchanged.
The new numerical identity must be used for cache and checkpoint compatibility.
The packaged runtime uses standard revision 16. See [current limits](../../docs/2.0-known-issues.md).

Revision 15 adds the shared `horton.py` correction: Modified Horton excess
infiltration state uses `MIN(Fe,Fmax)` at its finite upper bound. The current
flux equation and checkpoint state layout are unchanged. The prepared record
and build record include `horton_capacity` revision 1 and its recipe digest.
`swmm_getEasySewerHortonCapacity()` returns 1. See the
[capacity review limits](../../docs/2.0-known-issues.md) for the independent
reference, measured behavior and remaining scope.

Revision 14 applies the [shared path I/O recipe](../path_io/README.md): checked
4095-byte paths, pool-owned input names, binary physical INP records, explicit
scratch directory/stream ownership, and climate close-before-pool-release.
Build preparation pins all shared helper digests and refuses stale profiles.

* Complete native checkpoint ABI 2 and its six numerical corrections share
  `../checkpoint/patch.py` with the historical development recipe: EVENTS rule
  boundaries, disabled-routing clocks, paraboloid exfiltration, inlet street
  sides, groundwater conductivity and snow initialization. The manifest records
  every shared recipe hash; build refuses an incomplete or stale checkpoint
  profile. See [checkpoint contract](../checkpoint/README.md).
* The HOTSTART buildup loop writes one double per pollutant, matching the reader.
* HOTSTART reads check complete headers and scalar reads, reject nonfinite values
  and trailing bytes, open input read-only, and close on every error path.
* HOTSTART writes check header/state counts and final close. Error 334 identifies
  write failure; an existing simulation error remains the primary error. The
  saved file can be incomplete on failure, so Runner does not publish it.
* RAIN revision 2 checks the complete station table and referenced payloads,
  signed 32-bit offsets, finite samples, native calendar bounds and chronology.
  Counts are bounded by actual bytes. Shared aligned spans are checked once;
  adjacent stations may restart their calendars. Case-sensitive duplicate IDs
  retain the first physical row. USE opens read-only; runtime sample reads and
  SAVE writes, seeks, tells, flush and close are checked. Errors 322/324 identify
  read/write failures without replacing an existing primary error. New failed
  caches are removed. Station padding is zero-filled for deterministic output.
* Historical rainfall text checks fixed-field bounds and numeric conversion
  ranges before native indexing or integer arithmetic. Valid reference rainfall
  arithmetic and finite negative cache sentinels are retained. See the
  [RAIN boundary guide](../../docs/2.0-native-io.md) for scope and limits.
* `swmm_close` ends a partially active simulation, closes the climate file and
  clears closed input/report/output pointers. A repeated close is safe.
* Replacing a TREATMENT expression deletes its previous tree after the new tree
  has parsed successfully.
* POSIX `mkstemp` reservations are closed before reopening; absolute input paths
  are bounded before copying, including Linux `realpath` results.
* `swmm_getEasySewerStandardFixes()` identifies patch revision 16. It is not a signature
  or proof that an arbitrary external binary matches the packaged source.
* RUNOFF revision 3 checks the complete header and all frames, finite data and
  advancing time steps. Runtime reads commit state only after a complete valid
  frame. SAVE checks payload, count backpatch, seek/tell, flush and close;
  write failures report 326. USE is read-only and exhausted declared frames
  report 327. A failed start stops later HOTSTART initialization. See the
  [RUNOFF boundary guide](../../docs/2.0-native-io.md).

```powershell
python -B native/standard/prepare.py `
  --source D:/tmp/swmm-v5.2.4 `
  --destination D:/tmp/standard-build/source
```

`build.py` verifies the prepared bytes and uses an explicitly selected
GCC-compatible compiler on Linux/Windows or Apple Clang on macOS. It does not
download/install tools. All outputs, compiler logs and
JSON build evidence stay beside the requested output. The record includes the
full command, compiler version, source/recipe hashes and binary hash.

```sh
python3 -B native/standard/build.py \
  --source /mnt/d/tmp/standard-build/source \
  --compiler /usr/bin/gcc --target linux \
  --output /mnt/d/tmp/standard-build/linux/swmm5.so

python3 -B native/standard/build.py \
  --source /mnt/d/tmp/standard-build/source \
  --compiler /path/to/x86_64-w64-mingw32-gcc-posix --target windows \
  --output /mnt/d/tmp/standard-build/windows/swmm5.dll
```

Native Linux aarch64 builds add `--architecture aarch64` on an actual aarch64
host; macOS builds use `--target darwin --architecture arm64`,
`--deployment-target 14.0` and `--openmp-prefix /path/to/pinned-libomp` on an actual
macOS arm64 host. The macOS source/build host can be newer than the deployment
target, but every bundled dependency must meet the same 14.0 floor. The pinned
OpenMP preparation/build is documented in [testing](../../docs/2.0-testing.md).

All targets use `-O2 -fno-fast-math -ffp-contract=off`. Linux/Windows add
`-fopenmp`; Apple Clang uses its OpenMP preprocessing option and the explicitly
selected pinned runtime. There is no architecture-native tuning that would
silently narrow the advertised CPU baseline. Windows also uses
static runtime linking, a zero PE timestamp and a fixed preferred image base
(relocation/ASLR remain enabled). The fixed base avoids MinGW deriving different
image addresses from different output directories. Bit-for-bit reproduction is
only claimed when verified with the same toolchain and source; it is not promised
across compiler versions or platforms.

Each native wheel includes its platform's `swmm5.build.json` and final runtime
manifest. Dependency repair runs before final hashes are sealed, so the manifest
pins the bytes actually installed, not the pre-repair compiler output.
`StandardBackend` reports `numerical_policy=easysewer:standard:5.2.4:16` only
for those verified known builds. Other explicit libraries report
`user-library:unverified` even if their version is 52004 and the caller supplied
their hash. This policy changes cache provenance without changing Model schemas.

In particular, this build uses the reference NWS float division by 100. The old
Windows binary produced the rounded reciprocal variant in measured cases;
archive conversion must select the appropriate explicit arithmetic policy.
Neither a matching engine version nor a similar printed report proves identical
binary cache state or all-platform floating-point results.

The regression suite includes direct C calls (bypassing Python file inspection),
multi-catchment/land-use/pollutant SAVE/USE, independent buildup mass/byte oracles,
damaged/readonly inputs, native descriptor counts, repeated close and climate
failure paths. Full platform, coupled-physics and other native input audits remain
part of the 2.0 release gates. These changes apply to the standard backend.
The separately sourced
FlexiblePonding backend is an experimental, scientifically unvalidated
replaceable-backend example with its own patch manifest; it is optional on ARM
and not a core native gate. A platform manifest or successful doctor probe does
not establish scientific validity or a completed native CI pass.

Revision 4 adds `swmm_getEasySewerRunoffPhysics() == 1` and build-record
`runoff_physics: 1`. It corrects cached evaporation dimensions, groundwater
volumetric-flow conversion, local groundwater elevation and frame interpolation,
including the initial HOTSTART groundwater boundary. See
[replay semantics](../../docs/2.0-runoff-replay.md). Normal hydrology and SAVE
remain unchanged; USE results affected by the old defects intentionally change.

Revision 5 adds `swmm_getEasySewerRunoffRainClock() == 1` and build-record
`runoff_rain_clock: 1`. The shared `runoff_rain.c` fragment advances replay
rain reports and 1–48 hour controls at consumer times, splitting rainfall
changes and month boundaries. It preserves normal non-cache gage behavior.
Known hashes advertise `easysewer:runoff-rain-clock:1`; see the replay document
for exact hour boundaries and differences from native hydrologic-step history.

Revision 6 adds checked binary/text RDII frames, complete preflight, bounded
indices and production arithmetic, explicit rain-gage initialization, and
read/write/close error propagation. It exports `swmm_getEasySewerRdiiIO() == 1`
and records `rdii_io: 1`. Known hashes advertise `easysewer:rdii-io:1`.
See the [RDII boundary guide](../../docs/2.0-native-io.md) for formats, errors,
qualification and file ownership.

Revision 7 adds shared complete routing frames, checked I/O, full matrix
initialization and file-alias protection. It exports
`swmm_getEasySewerRoutingIO() == 1` and records `routing_io: 1`.
Qualified hashes advertise `easysewer:routing-io:1`; see the
[routing boundary guide](../../docs/2.0-native-io.md).

Revision 8 adds shared checked solver OUT writes, saved-value/report reads and
completion ownership. It exports `swmm_getEasySewerSolverOutputIO() == 1` and
records `solver_output_io: 1`. Known hashes advertise
`easysewer:solver-output-io:1`; see the
[solver OUT guide](../../docs/2.0-native-io.md) for errors, qualification
and remaining scope.

Revision 9 adds shared bounded climate records, complete-file validation,
checked stream ownership, independent temperature/evaporation cursors and
calendar/event advancement. It exports `swmm_getEasySewerClimateIO() == 1`
and records `climate_io: 1`. Known hashes advertise `easysewer:climate-io:1`;
see the [climate boundary guide](../../docs/2.0-native-io.md).

Revision 10 adds checked external time-series records and independent sequential
and interpolation cursors, including calendar rewind and single-point boundaries.
It exports `swmm_getEasySewerTimeSeriesIO() == 1` and records `timeseries_io: 1`.
Qualified hashes advertise `easysewer:timeseries-io:1`; see the
[time-series I/O guide](../../docs/2.0-native-io.md).

Revision 11 adds checked main RPT writes/flush/close, error 306, and early-failure
project ownership cleanup. It exports `swmm_getEasySewerReportIO() == 1`, records
`report_io: 1`, and advertises `easysewer:report-io:1` only for qualified binary
hashes. See [report I/O](../../docs/2.0-native-io.md).

Revision 12 adds checked detailed LID report formatting, writes, flush and close,
plus safe project teardown after failed LID initialization. The shared patch
exports `swmm_getEasySewerLidReportIO() == 1`, records `lid_report_io: 1`, and
advertises `easysewer:lid-report-io:1` only for qualified binary hashes.
See [LID report I/O](../../docs/2.0-native-io.md).
