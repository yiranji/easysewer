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
