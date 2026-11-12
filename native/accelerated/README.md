# Accelerated SWMM native recipe

This optional backend applies ordered hydraulic acceleration to the unchanged
`easysewer:standard:5.2.4:16` preparation recipe. Its numerical policy is
`easysewer:accelerated:5.2.4:1`; the runtime backend key is `swmm:accelerated`.
It inherits the Standard corrections, checked I/O, and complete-checkpoint ABI.
The exported `swmm_getEasySewerAccelerationVersion()` returns `1` and distinguishes
the native library from Standard. A marker alone is not provenance verification.

## Source changes and numerical contract

`prepare.py` first runs the existing Standard preparation in an isolated Python
interpreter. That recipe verifies the pinned EPA SWMM 5.2.4 source manifest and
applies all Standard revision 16 corrections. The accelerated recipe then applies
`dynwave.patch` using exact hunk positions and context, verifies its complete
before/after source hashes after CRLF-to-LF normalization of `dynwave.c`, and
appends the identity export to `swmm5.c`. Normalization accounts for inherited
checkpoint fragments whose checkout line endings can vary between hosts.
No other prepared source file changes relative to Standard.

The hydraulic patch provides:

- Optional, immutable non-conduit and outfall link lists, retaining original
  link order. Dummy conduits, pumps, and regulators retain the original serial
  flow/update sequence.
- An adjacency list for each node, constructed in ascending original link order.
  One worker gathers each node's conduit contributions. The four local `double`
  accumulators retain the original arithmetic expressions, branches, and order.
  Zero additions, negative flows, losses, native `char` barrel promotion, and both
  endpoints of self-loops retain their original treatment.
- For pure true-conduit networks with valid adjacency, gathering occurs at the
  start of the existing node-depth loop, before the outfall skip. Other networks
  retain the separate gather stage. The last separate gather loop uses `nowait`;
  the parallel-region join supplies its required synchronization.
- A portable inline macro. GCC and Clang use `always_inline`; MSVC spelling is
  available in the C patch, and other compilers receive ordinary `inline`.
  The supplied build driver supports the shared GCC/Clang toolchain policy;
  this annotation does not add an MSVC build driver.

Cache allocation is optional. Count, allocation-size, adjacency-total, and
endpoint bounds are checked before use. Failure frees partial caches and selects
the original scan or serial aggregation algorithm without setting a new solver
error. Empty link lists require no allocation; zero-node adjacency remains
absent. The original Standard allocation semantics for essential solver state
remain unchanged. Closing releases every optional cache, and rebuilding resets
its validity before allocation. The caches contain topology, not simulation
state, and do not change checkpoint field layouts.

The final hot arithmetic and dispatch functions are inherited from the validated
fused-inline prototype with source SHA-256
`0718729000122cbc9cc8010cfea9842d2c251c514fcc5c3219b1d99998b6b4e4`.
Integration adds the portable annotation and allocation/endpoint guards. This
history is evidence of origin, not a substitute for qualifying the built target
library. Qualification must compare the actual new binary with Standard.

## Requested and effective threads

The recipe does not set process affinity, `OMP_NUM_THREADS`, or a fixed worker
count. All existing parallel regions use the model's `NumThreads`. The unchanged
EPA validation of `[OPTIONS] THREADS` determines the effective value:

- `THREADS 1` requests serial execution.
- `THREADS 0` requests automatic selection.
- A positive request is capped by the OpenMP runtime maximum; EPA may also select
  one thread for networks below its existing size threshold.

The RPT analysis options report the effective thread count. Inspect the input's
requested `THREADS`, the process/OpenMP configuration, and that RPT value when
comparing serial and parallel runs. Four-core affinity was a qualification
environment choice, not a backend requirement. Arithmetic order within each
node is independent of which worker owns that node.

## Prepare and build

Use an empty destination outside the original EPA checkout. Generated sources,
compiler temporary files, logs, libraries, and build receipts belong in a work
directory outside the repository. For example:

```powershell
python -B native/accelerated/prepare.py --source D:/tmp/epa-swmm-5.2.4 --destination D:/tmp/accelerated/source
```

The build interface matches `native/standard/build.py`, including the shared
`--target`, `--architecture`, `--deployment-target`, and `--openmp-prefix` options:

```sh
python -B native/accelerated/build.py \
  --source /tmp/accelerated/source --compiler gcc \
  --output /tmp/accelerated/libswmm-accelerated.so \
  --target linux --architecture x86_64
```

Windows uses an explicit MinGW x64 compiler. Darwin uses the pinned OpenMP prefix
and the existing native-host requirements. Build flags remain `-O2`,
`-fno-fast-math`, and `-ffp-contract=off`, with OpenMP and the shared target flags.
The build script places compiler temporary files beside the requested output and
does not install or select the library. Packaging, target qualification, and
runtime backend selection remain separate steps.

`prepared-source.json` retains the EPA manifest in `base`, the complete unchanged
Standard receipt in `standard_source`, the new policy in `patch`, and an
`acceleration` record with version, source hashes, features, and patch-recipe
hashes. Its top-level `files` are the final prepared hashes. The build validates
inherited profiles, recipe identities, the restricted changed-file set, and all
prepared bytes before compiling. `<output>.json` records the exact command,
compiler, target, `standard_fixes: 16`, `acceleration_version: 1`, source receipt,
and final library hash. The sibling `.log` records compiler output.

## Qualification boundary

Stable regression tests live in `tests/test_accelerated_backend_v2.py`,
`tests/test_native_v2_accelerated_runtime.py`, and
`tests/test_native_v2_accelerated_hydraulics.py`. They cover backend identity and
optional discovery, public Runner execution and checkpoint isolation, and
ordered hydraulic equivalence. The native wheel build and repair tests also
check optional packaging and inherited source-policy evidence. Release CI runs
the accelerated tests against the final installed platform wheel; a skip is not
accepted as successful release qualification.

Large-model benchmarks, generated source trees, compiler scratch files and
exploratory instrumentation belong outside the repository. Set the test
process's temporary directory and affinity when reproducing local measurements;
these are test environment settings, not requirements of the backend.

Useful regression coverage includes mixed pumps/regulators and conduit order,
reverse flow, losses, multiple barrels, self-loops, storage, time-varying outfalls,
empty adjacency, allocation fallback, and repeated open/close. Test more than one
effective thread count without changing the arithmetic comparison tolerance.
Full OUT comparisons must include every actual saved time and variable; public
per-step `double` traces and selected internal helper comparisons provide
additional coverage. Neither saved single-precision results nor a helper with
stubbed flow/depth kernels proves equality of every private state at every
internal iteration. Performance evidence is model- and platform-specific.

### Local integration observation (2026-11-12)

On Windows x86-64, two interleaved runs per backend used four-core affinity and
four OpenMP threads for the same 15-hour hydraulic input. Public process sessions
saved all OUT results and omitted the optional detailed `report()` call.

| Mean measured phase | Standard | Accelerated | Time reduction |
| --- | ---: | ---: | ---: |
| Complete session | 224.65 s | 200.99 s | 10.53% |
| Hydraulic stepping | 196.31 s | 175.81 s | 10.45% |

Each output contained 900 periods, 5,050 nodes and 5,104 links. The complete OUT
bytes matched across all four runs, including 50,238,000 node/link values and
64,046,700 values across all saved groups. Routing step counts (57,062), sampled
step clocks and continuity errors also matched. Input SHA-256:
`262136cf999bcf4f16d443d464eaccc382ac36fbb9339dbbd783e17fb87d7520`.
Output SHA-256:
`e625912ee0f0fd56019381c499bb71c954f31f827ca58949cf033928849778fa`.
This is a local two-repetition observation, not an all-model performance claim.
Water-quality equivalence was outside this qualification scope.
