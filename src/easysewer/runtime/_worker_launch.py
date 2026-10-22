"""Launch the interpreter itself, not a Windows virtualenv redirector.

CPython's Windows venv launcher starts another process, so its PID/handle must
not be mistaken for the solver's. Reproduce its private environment handoff
only for Windows CPython venvs, and check the resulting runtime before loading
EasySewer. This preserves normal isolated venv site/.pth initialization instead
of copying the application's potentially unsafe sys.path into the worker.

The handoff is implemented by CPython's PC/venvlauncher.c (smuggle_path) and
PC/getpathp.c in 3.10, and Modules/getpath.py in 3.11 and later. It deliberately
survives -I; unsupported or changed runtime behavior fails the bootstrap checks.
"""

import os
from pathlib import Path
import sys


def _windows_venv():
    return (sys.platform == 'win32' and sys.implementation.name == 'cpython'
            and sys.prefix != sys.base_prefix)


def _base_interpreter():
    """Fail closed for missing, relative, or recursive redirector selections."""
    base = getattr(sys, '_base_executable', None)
    if not isinstance(base, str) or not base or not Path(base).is_absolute():
        raise RuntimeError('Windows venv worker requires an absolute CPython base executable')
    executable = Path(sys.executable)
    prefix = Path(sys.prefix)
    if (not executable.is_absolute() or not prefix.is_absolute()
            or not (prefix / 'pyvenv.cfg').is_file()
            or executable.parent not in (prefix, prefix / 'Scripts')):
        raise RuntimeError('Windows venv worker cannot verify its pyvenv.cfg and launcher location')
    base = Path(base).resolve()
    if not base.is_file() or base.is_relative_to(prefix.resolve()):
        raise RuntimeError('Windows venv worker requires an existing base interpreter outside the venv')
    return str(base)


def environment():
    # macOS uses the same variable differently. Do not change its environment.
    if sys.platform != 'win32':
        return None
    result = dict(os.environ)
    # These can affect getpath even under -I. Never inherit another launcher's
    # selection, and never mutate the application's environment.
    for name in tuple(result):
        if name.upper() in ('__PYVENV_LAUNCHER__', 'PYTHONEXECUTABLE'):
            del result[name]
    if _windows_venv():
        result['__PYVENV_LAUNCHER__'] = sys.executable
    return result


def command(package_root, *, module='easysewer.runtime._solver_worker'):
    if module not in ('easysewer.runtime._solver_worker', 'easysewer._doctor_worker'):
        raise ValueError('Unsupported internal worker entry point')
    executable = sys.executable
    bootstrap = 'import sys\n'
    if _windows_venv():
        executable = _base_interpreter()
        # sys.version includes the CPython build/compiler and target machine;
        # also check word size and ABI/cache tag, not the host OS architecture.
        signature = (sys.implementation.name, sys.version, sys.maxsize,
                     sys.implementation.cache_tag, getattr(sys, 'abiflags', ''))
        prefixes = (sys.prefix, sys.exec_prefix, sys.base_prefix, sys.base_exec_prefix)
        bootstrap += (
            'import os, json\n'
            'def fail(message):\n'
            # The first transport request is always load, with request ID 1.
            '    print(json.dumps(dict(id=1, ok=False, failure=dict(stage="launch", '
            'code=None, message=message))), flush=True)\n'
            '    raise SystemExit(78)\n'
            'actual = (sys.implementation.name, sys.version, sys.maxsize, '
            'sys.implementation.cache_tag, getattr(sys, "abiflags", ""))\n'
            f'if actual != {signature!r}:\n'
            '    fail("Windows venv worker base interpreter version, architecture, or ABI differs")\n'
            'normalize = lambda path: os.path.normcase(os.path.realpath(path))\n'
            'actual_prefixes = (sys.prefix, sys.exec_prefix, sys.base_prefix, sys.base_exec_prefix)\n'
            f'if tuple(map(normalize, actual_prefixes)) != tuple(map(normalize, {prefixes!r})):\n'
            '    fail("Windows venv worker did not preserve its virtual environment prefixes")\n'
            f'if normalize(sys.executable) != normalize({sys.executable!r}):\n'
            '    fail("Windows venv worker did not preserve its launcher path")\n'
            f'if normalize(getattr(sys, "_base_executable", "")) != normalize({executable!r}):\n'
            '    fail("Windows venv worker selected a different base executable")\n'
        )
    # -I excludes PYTHONPATH and cwd. Pin the installed package (or src tree),
    # while retaining the interpreter's own isolated dependency search paths.
    bootstrap += (f'sys.path.insert(0, {package_root!r})\n'
                  f'from {module} import main\nmain()\n')
    return [executable, '-I', '-B', '-u', '-c', bootstrap]
