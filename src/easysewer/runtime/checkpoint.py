"""Candidate public checkpoint storage and optional session result contracts.

Loading verifies stored content without loading native code. A matching native
engine still validates all state owners when a session restores the checkpoint.
"""
from dataclasses import dataclass, field
from datetime import timedelta
import hashlib
from pathlib import Path

from ._checkpoint_container import Checkpoint as _Archive, Limits as CheckpointLimits, load
from .backend import NativeFailure
from ..model.values import FileReference


@dataclass(frozen=True, kw_only=True)
class CheckpointSchedule:
    """Save at the next unfinished step boundary after each simulation interval.

    Every save gets a new directory. on_saved receives the committed
    RunnerCheckpoint; exceptions stop the run but do not remove that checkpoint.
    """
    directory: FileReference
    interval: timedelta = timedelta(minutes=5)
    on_saved: object | None = field(default=None,repr=False,compare=False)

    def __post_init__(self):
        if type(self.directory) is not FileReference:raise TypeError('Checkpoint directory requires FileReference')
        if self.directory.direction!='output':raise ValueError('Checkpoint directory requires an output FileReference')
        if type(self.interval) is not timedelta or self.interval<=timedelta(0):
            raise ValueError('Checkpoint interval must be a positive timedelta')
        if self.on_saved is not None and not callable(self.on_saved):raise TypeError('on_saved must be callable')


@dataclass(frozen=True)
class Checkpoint:
    """Verified checkpoint inspection value; its files must remain immutable."""
    _archive: _Archive = field(repr=False)
    _limits: CheckpointLimits = field(default_factory=CheckpointLimits, repr=False)

    def __post_init__(self):
        if type(self._archive) is not _Archive or type(self._limits) is not CheckpointLimits:
            raise TypeError('Checkpoint requires verified archive data and limits')

    @classmethod
    def load(cls, directory, *, limits=CheckpointLimits(), checkpoint=lambda: None):
        return cls(load(directory, limits=limits, checkpoint=checkpoint), limits)

    @property
    def directory(self): return self._archive.directory

    @property
    def snapshot(self): return self._archive.snapshot

    @property
    def simulation_seconds(self): return self._archive.simulation_seconds

    @property
    def sha256(self): return hashlib.sha256(self._archive.manifest).hexdigest()

    def materialize(self, directory, *, schema=None, checkpoint=lambda: None):
        """Create a new execution workspace; never replace an existing directory.

        Returns a relocated RunSnapshot for session.open_checkpoint(). Restoring
        uses this checkpoint's original execution identity and engine bytes.
        """
        verified = load(self.directory, limits=self._limits, checkpoint=checkpoint)
        if verified != self._archive:
            raise ValueError('Checkpoint changed after loading')
        return verified.materialize(directory, schema=schema, checkpoint=checkpoint)


@dataclass(frozen=True, kw_only=True)
class CheckpointOutput:
    """Current output file and its original workspace destination.

    Files may still be open until session.close(); collect them only afterwards.
    Ordinary streams redirect writes to path without replacing destination.
    directory identifies an explicitly restored output tree: path and destination
    are then aliases to the same live file. The caller owns final publication
    and workspace removal. role uses stable names, not C enums.
    """
    role: str
    path: Path
    destination: Path
    directory: Path | None = None


@dataclass(frozen=True, kw_only=True)
class CheckpointRestore:
    simulation_seconds: float
    outputs: tuple[CheckpointOutput, ...]
    cleanup: tuple[NativeFailure, ...] = ()
    committed: bool = True


OUTPUT_ROLES = ('run:report', 'run:output', 'swmm:interface.RUNOFF',
                'swmm:interface.OUTFLOWS', 'swmm:interface.HOTSTART', 'swmm:lid_report')
