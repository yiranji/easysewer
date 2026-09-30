# Development archive

All 217 docs files from before the 2026-09-30 cleanup are preserved byte-for-byte in the local repository:

```text
build/docs-cleanup-20260930/archive/docs/
```

Of those files, 145 stage reports, experiments, candidate patches and detailed evidence files have been removed from current docs. The snapshot also includes the original versions of retained guides. Original failures and unresolved issues were not deleted; current guidance is in [known issues](2.0-known-issues.md).

The [archive index](development-archive.json) records each original file's path, size, SHA-256, disposition and replacement guide. A complete backup is also stored locally at `build/docs-cleanup-20260930/development-docs.zip`. These paths exist only on machines retaining that development workspace. Release packages do not contain the archived material.

Verify the local snapshot:

```console
python tools/audit_docs.py --archive-root build/docs-cleanup-20260930/archive
```

To share the historical material, distribute the ZIP and index separately. Extract into a separate directory for inspection rather than overwriting current docs. Historical gates bind old source and package digests and do not prove acceptance of the current version. See the [support inventory](2.0-support-matrix.md) for optional auditing.

Current documentation is maintained in English. The immutable historical archive retains its original languages and bytes so its evidence hashes remain valid.
