"""
Utility Functions Module

This module provides helper functions for file I/O operations and data processing
in the urban drainage model, particularly for SWMM input/output file handling.
It also includes utility functions for finding library paths across different operating systems.
"""

import os
import sys
import platform


class NativeCapabilityError(RuntimeError):
    pass


def _get_library_path_candidates(lib_name: str) -> tuple[str, list[str]]:
    """
    Build candidate library paths for current runtime platform.

    Args:
        lib_name: Base library name without extension

    Returns:
        tuple[str, list[str]]: (library extension, candidate absolute paths)

    Raises:
        OSError: If current operating system is not supported
    """
    # Determine base path based on execution environment
    if getattr(sys, 'frozen', False):
        if hasattr(sys, '_MEIPASS'):
            # Use realpath to resolve short file names (8.3 format) to full paths
            base_path = os.path.realpath(sys._MEIPASS)
        else:
            # Use realpath to resolve short file names (8.3 format) to full paths
            base_path = os.path.realpath(os.path.dirname(sys.executable))
    else:
        # Development environment
        base_path = os.path.dirname(os.path.abspath(__file__))

    # Define library paths for different operating systems
    lib_paths = {
        'Windows': ('.dll.esdll', 'win'),
        'Linux': ('.so.esso', 'linux')
    }

    # Get the current operating system
    system = platform.system()
    if system not in lib_paths:
        raise OSError(f'Unsupported operating system: {system}')

    # Get the file extension and directory for the current OS
    lib_ext, lib_dir = lib_paths[system]

    # Build possible paths to search for the library
    possible_paths = [
        os.path.join(base_path, 'libs', lib_dir, f'{lib_name}{lib_ext}'),
        os.path.join(base_path, 'easysewer', 'libs', lib_dir, f'{lib_name}{lib_ext}'),
        os.path.join(os.path.dirname(__file__), 'libs', lib_dir, f'{lib_name}{lib_ext}')
    ]
    return lib_ext, possible_paths


def probe_library_path(lib_name: str) -> str | None:
    """
    Probe native library path without loading it.

    This function only checks file existence and never calls ctypes.CDLL.

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
        if os.path.exists(path):
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
        OSError: If the operating system is not supported
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
    Detect availability of packaged native capabilities without loading any CDLL.

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


def get_swmm_inp_content(filename, flag):
    """
    Extracts content from a specific section of a SWMM input file.
    
    Args:
        filename (str): Path to the SWMM input file
        flag (str): Section identifier (e.g., '[TITLE]', '[JUNCTIONS]')
    
    Returns:
        list: Lines of content from the specified section
    """
    flag += '\n'
    result = []

    with open(filename, 'r', encoding='utf-8') as f:
        # getting to the flag line
        for line in f:
            if line == flag:
                break
        # adding related lines to results
        for line in f:
            # finish when getting to another section
            if line[0] == '[':
                break
            # skip if this line is blank or annotation
            if line == '\n' or line[0] == ';':
                continue
            result.append(line[0:-1])

    return result


def combine_swmm_inp_contents(content1, content2):
    """
    Combines two sections of SWMM input content based on matching identifiers.
    
    Args:
        content1 (list): Primary content lines
        content2 (list): Secondary content lines to merge
    
    Returns:
        list: Combined content with merged information
    """
    # generate a name list of content1
    index_dic = []
    for line in content1:
        pair = line.split()
        index_dic.append(pair[0])
    #
    for line in content2:
        pair = line.split()
        index = index_dic.index(pair[0])
        content1[index] = content1[index] + ' ' + ' '.join(pair[1::])
    #
    return content1


def get_swmm_rpt_content(filename, flag):
    """
    Extracts content from a specific section of a SWMM report file.
    
    Args:
        filename (str): Path to the SWMM report file
        flag (str): Section identifier
    
    Returns:
        list: Lines of content from the specified report section
    """
    # example:
    # res = ut.get_swmm_rpt_content('calculate_temp/test.rpt', 'Node G80F425')
    flag = f'  <<< {flag} >>>\n'
    result = []
    with open(filename, 'r', encoding='utf-8') as f:
        # getting to the flag line
        for line in f:
            if line == flag:
                break
        # adding related lines to results
        i = 0
        for line in f:
            # skip title bar ( four lines )
            if i < 4:
                i += 1
                continue
            # finish when getting to another section
            if line == '  \n':
                break
            result.append(line[0:-1])
    return result
