"""Explicit trusted adapters for regular-directory format inspection.

Tree capture and content evidence are framework operations. The custom callback
only validates its declared format; it must not mutate the source or model.
"""
from dataclasses import dataclass, replace
from pathlib import Path

from ._directory_tree import DirectoryLimits, DirectoryEntry, DirectoryManifest, inspect_tree, verify_tree
from ..model.identity import namespace_key
from ..io.interface_inspection import InterfaceInspection
from ..validation._cooperative import checkpoint_scope, checkpoint as work_checkpoint


@dataclass(frozen=True, kw_only=True)
class DirectoryAdapter:
    inspector: object
    limits: DirectoryLimits = DirectoryLimits()

    def __post_init__(self):
        if not callable(self.inspector):
            raise TypeError('Directory adapter requires an explicit trusted inspector')
        if type(self.limits) is not DirectoryLimits:
            raise TypeError('Directory adapter requires DirectoryLimits')

    def inspect(self, root, *, use, model, encoding, source, max_bytes, checkpoint=None):
        """Inspect one complete bounded tree and reject callback source changes."""
        if type(max_bytes) is not int or max_bytes <= 0:
            raise ValueError('Directory inspection byte limit must be positive')
        with checkpoint_scope(checkpoint):
            limits = replace(self.limits, total_bytes=min(self.limits.total_bytes, max_bytes))
            manifest = inspect_tree(root, limits=limits)
            work_checkpoint()
            result = self.inspector(Path(root), manifest=manifest, use=use, model=model,
                                    encoding=encoding, source=source)
            work_checkpoint()
            if type(result) is not InterfaceInspection:
                raise TypeError('Directory inspector must return InterfaceInspection')
            if result.format != use.format:
                raise ValueError('Directory inspector returned a different format')
            verify_tree(root, manifest, limits=limits)
            return result


def directory_adapters(values):
    """Validate and snapshot the registry; never discover executable sidecars."""
    result = dict(values or {})
    for key, value in result.items():
        namespace_key(key)
        if type(value) is not DirectoryAdapter:
            raise TypeError('Directory formats require DirectoryAdapter instances')
    return result
