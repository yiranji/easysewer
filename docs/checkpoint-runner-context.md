# Runner checkpoint context (development)

The private Runner envelope joins a qualified Session checkpoint with the
execution evidence that is not present in `RunSnapshot`: diagnostics, cache
production conditions and consumption decisions, declared backend products,
observed step count and prior continuation records. `RunnerCheckpoint` exposes
verified loading and inspection; the envelope implementation remains private.
[Runner](checkpoint-runner.md) uses it for scheduled saves and `resume()`.

The new outer directory contains `state/` (a versioned low-level checkpoint),
content-addressed metadata `blobs/`, a versioned `runner.json`, and a
`runner.commit` marker written last. It binds the inner manifest digest to the
context. It refuses an existing destination and verifies both inventories,
digests, byte limits and typed fields on load. Loading does not open native
libraries or follow old input/output paths. Metadata write failure cleans only
the new owned directory; if cleanup also fails, the primary error survives with
the cleanup diagnostic and the incomplete directory remains explicit.

Context validation requires complete cache kinds for the snapshot, matching
input/engine/cache digests, allowed captured reuse decisions and matching
consumer-context identities. Backend products must be unique and remain below
the asset directory. A custom worker trace must be declared as a product.
Continuation records must identify the same execution and cannot claim a
restore boundary later than the saved clock/step count. Stored history is
metadata integrity, not authentication or proof of how arbitrary callers ran a
solver.

Capture uses Session's qualified default bounds and checks the combined state
and context blob budget before committing outer metadata. Loading accepts
explicit limits. Materialization revalidates the whole envelope before creating
a new independent input workspace; a forged in-memory context is not a
validation token. It keeps the original execution identity so the Session can
still validate native and Python state together.

The outer envelope now declares codec 1.2 to retain structured diagnostic
locations, and explicitly reads codec 1.1 with empty location metadata. The
inner file-only state format and snapshot codec remain 1.0 and 1.1.
Directory resources select an explicit newer inner format and snapshot codec;
see [directory resources](2.0-directory-resources.md). Context diagnostics do not
silently change execution identity or old state bindings. Older codecs refuse
values they cannot represent rather than discarding metadata.

File-only result archive 1.3 (and directory-capable 1.4–1.9) separately persists continuation history and diagnostic
locations for all five result statuses, with explicit migration from 1.0–1.2
and compact, verified report evidence.
Resume retains the original Runner diagnostics and appends diagnostics from
revalidating the frozen execution INP. Previously unlabelled spans from that
revalidation use `checkpoint-input:<input SHA-256>` as their source label. This
identifies archived input content, not a filesystem path or the original edited
document, and does not change file-reference base directories. The diagnostic
list can therefore contain the same warning about two distinct source contexts.
See [result archives](2.0-result-archive.md). Complete checkpoint behavior has
its own qualified scope; final public API and overall release checks remain open.
