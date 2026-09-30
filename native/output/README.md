# SWMM OUT reader

This recipe builds the SMO reader independently of either solver. `source.json`
pins the seven EPA outfile files at SWMM 5.2.4 commit
`7952ca837988b1c32f791812eccc9fd64547e093` (LF-normalized SHA256). Preparation
verifies every file, retains the public headers, generates the export header
and replaces `swmm_output.c` with `output_reader.c`. The original error manager
is retained in the source record but not compiled. This establishes the new
build's provenance, not the source identity of previously distributed binaries.

```powershell
python -B native/output/prepare.py --source D:\tmp\epa-swmm --destination D:\tmp\out-source
python -B native/output/build.py --source D:\tmp\out-source --compiler gcc --output D:\tmp\swmm-output.dll --target windows
```

Use a GCC-compatible compiler for the requested target (MinGW-w64 for Windows).
Linux uses `--target linux` and a `.so` output. Prepared trees, outputs and logs
belong outside the repository. The adjacent JSON build record contains source,
recipe, compiler, flags and binary hashes. `swmm_getEasySewerOutputIO()` returns
`1`: an interface revision, not proof of qualification for an arbitrary binary.

## File and query contract

- Little-endian SWMM 5.2.4 (`52004`), exact magic/footer/period extent, valid
  counts, units and interval. Metadata plus cached indexes has a cumulative
  64 MiB allocation budget (failure 411). Result allocations are bounded by
  validated query ranges and allocation arithmetic; large queries may fail 411.
- Names retain their bytes; duplicate detection folds ASCII letters within
  each group. Names have 1–65536 bytes and no NUL. Windows paths are UTF-8;
  POSIX paths use filesystem bytes.
- Attribute arguments select saved variable codes, including pollutants,
  system code 14 and unknown nonnegative extension codes. Codes must be unique.
  Result arrays retain the file's original column order.
- Series ranges are nonempty and end-exclusive. Indexes equal to object counts
  are invalid. Empty groups return NULL/zero attribute arrays. The historical
  system result dummy index remains ignored.
- Queries check open state and file size. Queried dates/values and input float
  properties must be finite; series timestamps must increase. This does not
  snapshot or hash every read or validate every unqueried observation. Callers
  must not modify an open file or share handles concurrently. The portable
  `OutputReader` offers captured artifact identity and missing-value policies.
- The legacy C API returns 436 for zero periods and 422 for empty ranges. The
  portable reader deliberately accepts empty containers and queries.
- A nonnegative, nonzero footer simulation error returns warning 10 and leaves
  diagnostic data open. It is not a successful completed run. Portable result
  ingestion rejects failed-run footers.

## Ownership and errors

`SMO_init` initializes one unowned slot; do not initialize a live slot twice.
`SMO_open` replaces the current file. Failed open/parse leaves the handle alive,
unopened, retryable and safely closable. NULL/empty paths are rejected before
replacing a file. `SMO_close` consumes the handle and nulls the slot even on a
reported close failure; an already NULL slot closes successfully.

All returned arrays, names and error messages are heap allocations released
with `SMO_free`. Array outputs become NULL/zero before work and are published
only on complete success. Failed reads release temporary buffers. NULL
arguments return errors; stale aliases/arbitrary invalid non-NULL pointers are
outside the C ABI contract. Errors remain sticky until `SMO_clearError`.
Primary open/parse errors survive cleanup.

Codes: 411 allocation/budget, 421 parameter/code, 422 period, 423 element,
424 missing output pointer, 434 unopened/open failure, 435 invalid data or
file I/O failure, 436 no periods. Python `SWMMOutputError` exposes `operation`
and `code`. The adapter initializes once, supports explicit/idempotent `close`,
frees query buffers in `finally` and preserves primary context exceptions.

For direct ABI tests, set `EASYSEWER_TEST_OUTPUT_LIBRARY` and run
`tests/test_native_v2_output_io.py`. This selector is test-only. Independent
baselines, normal comparisons, fault injection and sanitizer records are kept
with the external development plan. Solver OUT writing and report rereads are
a separate implementation and qualification task.
