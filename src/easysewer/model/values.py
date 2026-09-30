"""Domain value types. File references are lexical: no filesystem access."""

from dataclasses import dataclass
from enum import Enum
from pathlib import PurePath, PurePosixPath, PureWindowsPath
import ntpath
import posixpath

from .fields import number


class Offset(Enum):
    """Explicit '*' in ELEVATION mode, distinct from a missing field."""
    NODE_INVERT = "node_invert"


@dataclass(frozen=True, kw_only=True)
class Point:
    x: float = number("map_coordinate")
    y: float = number("map_coordinate")


@dataclass(frozen=True, kw_only=True)
class FileReference:
    path: str
    base_directory: str | None = None
    flavor: str = "native"
    direction: str = "input"

    def __post_init__(self):
        if not isinstance(self.path, str) or not self.path or "\x00" in self.path:
            raise ValueError("A file reference needs a nonempty path without NUL")
        if self.flavor not in ("native", "windows", "posix"):
            raise ValueError("Path flavor must be native, windows or posix")
        if self.direction not in ("input", "output"):
            raise ValueError("File direction must be input or output")
        if self.base_directory is not None and not self.path_type(self.base_directory).is_absolute():
            raise ValueError("File reference base_directory must be absolute")
        # A drive-relative Windows path depends on hidden process drive state.
        path = self.path_type(self.path)
        if isinstance(path, PureWindowsPath) and path.drive and not path.is_absolute():
            raise ValueError("Drive-relative Windows paths are not portable references")

    @property
    def path_type(self):
        if self.flavor == "windows":
            return PureWindowsPath
        if self.flavor == "posix":
            return PurePosixPath
        return type(PurePath())

    @property
    def _operations(self):
        return ntpath if self.path_type is PureWindowsPath else posixpath

    def resolve(self, *, relative_to: str | None = None) -> PurePath:
        path = self.path_type(self.path)
        if not path.is_absolute():
            base = self.base_directory or relative_to
            if base is None or not self.path_type(base).is_absolute():
                raise ValueError("An absolute base directory is required to resolve a relative path")
            path = self.path_type(base) / path
        return self.path_type(self._operations.normpath(str(path)))

    def for_directory(self, directory: str, *, policy: str = "relative") -> str:
        """Rebase without IO; cross-drive Windows paths remain absolute."""
        if policy == "preserve":
            return self.path
        if policy not in ("relative", "absolute"):
            raise ValueError("Path policy must be preserve, relative or absolute")
        base = self.path_type(directory)
        if not base.is_absolute():
            raise ValueError("Destination directory must be absolute")
        target = self.resolve(relative_to=directory)
        if policy == "absolute":
            return str(target)
        try:
            return self._operations.relpath(str(target), str(base))
        except ValueError:
            return str(target)
