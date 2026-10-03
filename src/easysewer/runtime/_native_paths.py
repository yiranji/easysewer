"""Private native path spellings, without moving or taking ownership of files."""

from pathlib import Path
import sys


def _short_path(path):
    """Read an existing Windows short name; never create aliases or change policy."""
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    function = kernel.GetShortPathNameW
    function.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    function.restype = wintypes.DWORD
    size = function(str(path), None, 0)
    if not 0 < size <= 32768:
        return None
    buffer = ctypes.create_unicode_buffer(size)
    count = function(str(path), buffer, size)
    if not 0 < count < size:
        return None
    return buffer.value


def worker_directory(directory):
    """Return a same-directory ASCII launch spelling when Windows provides one.

    Canonical paths remain the ownership/provenance locations. Short names may
    be disabled or unavailable, or may still contain non-ASCII characters. In
    those cases preserve the original launch path and native failure behavior.
    """
    original = str(directory)
    if sys.platform != 'win32' or original.isascii():
        return original
    try:
        spelling = _short_path(original)
        if not spelling or not spelling.isascii() or '\0' in spelling:
            return original
        candidate, canonical = Path(spelling), Path(directory)
        if (not candidate.is_absolute() or
                candidate.resolve(strict=True) != canonical.resolve(strict=True) or
                not candidate.samefile(canonical)):
            return original
        return spelling
    except (AttributeError, OSError, RuntimeError, ValueError):
        return original


def restore_path(path, root):
    """Spell an already verified private restore file relative to native cwd."""
    if sys.platform != 'win32':
        return path
    relative = path.relative_to(root)
    kind, _, index = relative.name.partition('-')
    if (not str(relative).isascii() or len(relative.parts) != 2 or
            not relative.parts[0].startswith('.checkpoint-restore-') or
            relative.parts[0] == '.checkpoint-restore-' or
            kind not in ('input', 'output') or not index.isascii() or not index.isdigit()):
        raise ValueError('Native checkpoint path is not a private restore file')
    return relative
