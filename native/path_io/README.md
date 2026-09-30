# Shared native path I/O recipe

The standard and flexible-ponding preparation scripts apply this shared recipe
after checkpoint preparation. It produces standard revision 14 / custom native
I/O revision 12, keeps checkpoint ABI 2 and adds `swmm_getEasySewerPathIO() == 1`.
The runtime grants path I/O capability only to independently qualified hashes.
The standalone prepare/build commands remain available to reproduce the earlier
candidate from a verified historical standard 13 / custom 11 prepared source.

The recipe checks complete source digests before transforming the input tree.
It records recipe and resulting source digests in `path-source.json`. Build
checks those digests again, uses the explicitly selected compiler, and retains
the command, compiler version, diagnostics and binary digest. It does not
install compilers or replace package libraries.

## Changes

- Check copying and joining native paths before use; accept up to 4095 bytes
  and refuse an unrepresentable path without using a truncated prefix.
- Store table and rain-gage input names in the project pool by actual length.
  Stream and checkpoint cursors borrow immutable names. Global output names
  retain their fixed buffers and existing transaction ownership.
- Respect POSIX filename syntax for colon and backslash. On Windows, use the
  wide full-path resolver with the ANSI code page of the existing narrow API;
  handle slash normalization, drive-relative and UNC path syntax. Refuse a
  failed or lossy conversion.
- Read the INP in binary mode, normalize record terminators explicitly, and
  reject NUL, Ctrl+Z, overlong physical records and actual read failures.
  External data formats retain their own line limits. INP content is limited
  to 1023 bytes per physical record, excluding LF or CRLF.
- Make pool initialization and extension allocation failures release partial
  allocations and leave the current pool usable. Check block capacity before
  pointer arithmetic and keep existing borrowed names stable.
- Keep every scratch stream open from exclusive creation to its final owner.
  Resolve explicit TEMPDIR against the INP directory; do not fall back to a
  different directory when it fails. Clean up failed ownership transfers.
- Preserve working-directory-relative RDII and LID report names; other FILES
  entries retain their document-relative base.
- Close climate table consumers before releasing the project name pool, so
  close-error diagnostics cannot read freed input names.

## Rebuild from original source

Prepare the pinned original sources directly with the integrated recipes:

```sh
python -B native/standard/prepare.py --source /path/to/pinned-swmm \
  --destination /work/path-standard
python -B native/flexible_ponding/prepare.py --source /path/to/pinned-custom \
  --destination /work/path-custom
python -B native/standard/build.py --source /work/path-standard \
  --compiler /usr/bin/gcc --target linux --output /work/linux/solver.so
```

Use an explicit MinGW GCC compiler and `--target windows` for the Windows
library. Use `native/flexible_ponding/build.py` with the custom source.
All preparation destinations must
be empty. The independent rebuild performed on 2026-09-29 reproduced all four
candidate library hashes with the same installed compilers.

## Verification

Set `PYTHONPATH` to `src` and `tests`, and set
`EASYSEWER_PATH_TEST_STANDARD` / `EASYSEWER_PATH_TEST_CUSTOM` to the absolute
candidate library paths. Run:

```sh
python -B -m unittest -v test_native_v2_path_io test_native_v2_path_resources \
  test_native_v2_path_temp test_native_v2_path_capacity test_native_v2_path_interfaces
```

Set `EASYSEWER_CHECKPOINT_STANDARD` / `EASYSEWER_CHECKPOINT_CUSTOM` to the same
libraries for coordinator, container and lifecycle regressions. These exercise
the direct native/worker checkpoint protocol; they do not grant public Runner
capabilities to an unknown library hash.

For Linux ASan/UBSan with real stdio read errors, parsing allocation failures
and table-stream close failures:

```sh
python -B tools/qualify_native_path_faults.py --source /work/path-standard \
  --output /work/faults-standard --compiler /usr/bin/gcc
```

Repeat for the custom source. The standalone `tests/native_path_syntax.c`
harness covers platform syntax, capacity guards, in-place joins and input-name
ownership; compile it with `-I native/path_io -I /work/path-standard/src/solver`.
Add `-DWINDOWS` for MinGW, or `-fsanitize=address,undefined` for Linux.

## Verification boundaries

The native harnesses cover path syntax, capacity guards, in-place joins, ownership,
allocation failures and checked I/O. They do not cover every entrypoint combination
or language environment. Actual UNC share I/O remains outside these checks;
UNC syntax tests perform no network access.

See [resource management](../../docs/2.0-resource-management.md) for current behavior
and [development archive](../../docs/development-archive.md) for original batch records.
