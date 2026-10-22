"""
Utility Functions Module

This module provides helper functions for file I/O operations and data processing
in the urban drainage model, particularly for SWMM input/output file handling.
It also includes utility functions for finding library paths across different operating systems.
"""

import os


class NativeCapabilityError(RuntimeError):
    pass


def _packaged_native_platform_error() -> str | None:
    """Explain unsupported process ABIs without loading a library."""
    from ._native_registry import platform_error
    return platform_error()


def _get_library_path_candidates(lib_name: str) -> tuple[str, list[str]]:
    """Select only the current process ABI's manifest entry, without loading it."""
    from ._native_registry import select_library
    artifact = select_library(lib_name)
    path = str(artifact.path)
    return path[path.rfind(lib_name) + len(lib_name):], [path]


def probe_library_path(lib_name: str) -> str | None:
    """
    Probe native library path without loading it.

    This function checks the packaged platform/ABI and regular-file existence.
    It never calls ctypes.CDLL or verifies system-library dependencies.

    Args:
        lib_name: The name of the library file to find

    Returns:
        str | None: Resolved library path if found, otherwise None
    """
    try:
        _, possible_paths = _get_library_path_candidates(lib_name)
    except OSError:
        return None
    for path in possible_paths:
        if os.path.isfile(path):
            return os.path.realpath(path)
    return None


def find_library_path(lib_name: str) -> str:
    """
    Find the path to a library file based on the operating system and execution environment.
    
    Args:
        lib_name: The name of the library file to find
        
    Returns:
        str: The path to the library file if found
        
    Raises:
        FileNotFoundError: If the library file cannot be found
        OSError: If the packaged libraries do not support this runtime ABI
    """
    lib_ext, possible_paths = _get_library_path_candidates(lib_name)
    lib_path = probe_library_path(lib_name)
    if lib_path is None:
        # Also resolve paths in error message for better debugging
        resolved_paths = [os.path.realpath(path) for path in possible_paths]
        raise FileNotFoundError(f"Could not find {lib_name}{lib_ext} in any of these locations: {resolved_paths}")
    return lib_path


def get_native_capabilities() -> dict[str, bool]:
    """
    Detect packaged native files for this platform/ABI without loading a CDLL.

    True means that a candidate file exists, not that the system can load it.
    Use StandardBackend().probe() or FlexiblePondingBackend().probe() to check
    real solver availability, including native system-library dependencies.

    Returns:
        dict[str, bool]: Capability flags keyed by feature name
    """
    return {
        "swmm_solver": probe_library_path("swmm5") is not None,
        "swmm_output": probe_library_path("swmm-output") is not None,
        "flexible_ponding": probe_library_path("flexible_ponding") is not None,
    }


def require_native_capability(feature: str, lib_name: str) -> str:
    try:
        lib_path = find_library_path(lib_name)
    except (FileNotFoundError, OSError) as exc:
        raise NativeCapabilityError(
            f"{feature} is unavailable in the current runtime. "
            f"This capability requires native library '{lib_name}' and cannot run in environments like Pyodide."
        ) from exc
    return lib_path
