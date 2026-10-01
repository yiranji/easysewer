# LID detailed report I/O patch

The shared patch is integrated into installed standard12/custom10 libraries via
`native/standard/lid_report_io.py`. See the [report guide](../../docs/2.0-native-io.md).
The standalone development recipe retains its standard11/custom9 input contract
to reproduce the historical candidate and fault evidence. It is not a complete
LID checkpoint owner.

All nine header and three result write sites use a checked formatter. Pending
result formatting rejects negative returns and truncation. A linked dirty queue
flushes changed streams at the existing native public report boundaries; an
unchanged boundary does not scan all deployed LIDs. Close detaches each stream
before collecting errors and freeing its owner. Diagnostic names are owned and
released with the report. Failed partial allocations are safe to delete.

The first I/O failure records error 306 and the detailed report path, without
recursively writing to the failed stream. Earlier native errors remain primary;
close errors are still returned separately. Repeated close succeeds. A guarded
new open resets failure state only after the previous project has been closed.
The dry-period counter saturates at two because its consumers distinguish only
zero, one and more than one; this prevents signed overflow without changing rows.

Qualification uses uninstrumented and separately recorded fault-injection builds
on both platforms and engine families. Tests compare full OUT, timing-normalized
main RPT and exact detailed reports, check boundary visibility, POSIX /dev/full,
every reachable checked call in a two-report fixture, first-error precedence,
handle stability, repeated close and subsequent successful runs.

The initial targeted Windows/Linux checks and both families' 50-lifecycle
ASan/UBSan/leak runs passed. A covered-barrel fixture initially failed to produce
wet periods; the corrected uncovered-barrel case proves both dry edges and
previous-row output. Extended qualification now also passes nine native tests
in Windows Python 3.10/3.13 and Linux Python 3.12, including partial-open cleanup
and all LID allocations. Each platform passes 60 real Runner failure/recovery
cases with both failed-artifact retention policies, preserving all pre-existing
published and external reports. Both engine families pass 95 sanitizer lifecycles;
four candidate binaries reproduce from the source distribution.
Formal production recipes add revision and capability markers to this same
qualified source patch. Installed libraries are uninstrumented; the separate
development fault artifacts are not installed.
The checkpoint branch's six numerical candidates are separate. LID persistent
state, previous detail row and output-prefix transactions also remain pending.

The extended allocation audit found that project creation overwrote an error
from `lid_create` with the result of `controls_create`, then read input through
unallocated LID arrays. The candidate now stops after a failed LID creation and
before input parsing after a failed object creation. Early exit also requires
checking the subcatchment land-factor pointer during deletion: its nested array
has not yet been allocated at that point. Fault instrumentation covers every
LID model allocation, report-owner/path allocation and report open, separately
from the checked report writes. These hooks exist only in test builds.

## Current-source test-only Linux fault profile

The historical `prepare.py` CLI still requires standard11/custom9 source. Do not
apply it to a current prepared tree. The shared audited test builder now accepts
`--profile lid-report-faults`, which prepares the pinned upstream bytes with the
**current** family recipe (standard16/custom native-I/O14), then instruments a
separate copy. It does not change production recipes, installed binaries,
numerical expressions, checkpoint hooks, or the historical recipe contract.

```sh
python -B tools/build_checkpoint_test_library.py --profile lid-report-faults \
  --family standard --source /path/to/pinned-epa-source \
  --compiler /usr/bin/gcc --destination /tmp/lid-test-standard
python -B tools/build_checkpoint_test_library.py --profile lid-report-faults \
  --family custom --source /path/to/authorized-pinned-custom-source \
  --compiler /usr/bin/gcc --destination /tmp/lid-test-custom
python -B tools/qualify_checkpoint_test_libraries.py --profile lid-report-faults \
  --standard-build /tmp/lid-test-standard --custom-build /tmp/lid-test-custom \
  --destination /tmp/lid-test-results
```

The explicit profile ID is `easysewer:test-only:lid-report-faults:1`. It uses
`lid-report-test-source.json`, `lid-report-test-build.json`, and separately named
`lid-report-test-{family}.so` and `lid-report-test-control-{family}.so` artifacts.
The control library is built from the untouched current prepared source; the
fault library changes only 13 exact `lid.c` call sites, appends the existing
`tests/native_lid_report_faults.inc`, and exports `es_test_lid_report_profile()`.
Report and model allocations remain separately injected. No global allocator
macros intercept other owners or the wrapper bodies. The close wrapper performs
the real close before returning its injected failure.

Both libraries, both source-tree inventories, preparation inputs, compiler bytes,
and test helpers are hash-checked. Control output is checked again after the
fault build. The instrumented tree never receives `prepared-source.json`.
Destinations must be fresh and disjoint from source/repository/build directories;
all generated source and libraries stay outside the repository. The default
checkpoint profile and its filenames/CLI remain unchanged, and the LID qualifier
rejects checkpoint-profile artifacts. Rebuild test libraries after builder edits.

Qualification runs the five existing fault-only methods unchanged plus one gate
containing 32 no-fault comparisons across eight LID types, two unit systems, and
both families. OUT and both detailed reports must match the clean control bytes;
main RPT comparison removes only timing lines. Native version, family revision,
custom ABI, LID capability, and distinct test-profile markers are checked before
running. Each reachable write/flush/close/format/report-allocation injection must
fire and recover; model allocations and report opens must clean up partial state.
The Runner test covers all 60 unique combinations of family, artifact-retention
policy, and injection, including primary-error precedence. Existing assertions
require `fired=1` from each fresh subprocess, exact failure stage/code, preserved
published/external reports, released locks, and successful recovery.

Ambient `ES_LID_IO_FAULT`, `ES_LID_IO_HIT`, and `ES_LID_IO_PRIMARY` are cleared
before qualification. Direct tests use the reset API; only fresh Runner children
use environment-driven injection. `OMP_NUM_THREADS=1`, unoptimized Python, and
Linux `/proc/self/fd` are required. Any skip, expected failure, unexpected success,
wrong test count, incomplete/duplicated case evidence, or post-test input change
fails qualification. `qualification.json` records all case evidence, clean-control
hashes, identities, and test/runtime inputs; `tests.log` records the full results.

Compiler-independent guardrails run with:

```sh
PYTHONPATH=src:tests python -B -m unittest \
  test_checkpoint_test_build_tool test_lid_report_test_build_tool
```

This profile is Linux source-development evidence only. It does not reproduce the
historical Windows artifacts, establish cross-platform/cross-toolchain bit
identity, certify every numerical model, or qualify a production release. Do not
install, publish, register, or copy either test artifact into packaged libraries.
