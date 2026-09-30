"""Read-only process identity, captured in the worker and checked on recovery.

Importing this module loads no native library. Windows APIs are used only by
explicit observation; Linux identities are bound to the boot and PID namespace.
"""
import os
from pathlib import Path
import sys


def validate(value, *, pid=None):
    if (type(value) is not dict or set(value) != {'platform','pid','started','boot_id','pid_namespace'}
            or value['platform'] not in ('win32','linux')
            or type(value['pid']) is not int or not 0 < value['pid'] < 2**32
            or type(value['started']) is not int or not 0 < value['started'] < 2**64
            or (pid is not None and (value['pid'] != pid or value['platform'] != sys.platform))):
        raise ValueError('Invalid worker process identity')
    if value['platform'] == 'win32':
        if value['boot_id'] is not None or value['pid_namespace'] is not None:
            raise ValueError('Invalid Windows worker identity')
    else:
        boot=value['boot_id'];namespace=value['pid_namespace']
        if (type(boot) is not str or len(boot)!=36 or any(c not in '0123456789abcdef-' for c in boot)
                or {i for i,c in enumerate(boot) if c=='-'}!={8,13,18,23}
                or type(namespace) is not list or len(namespace)!=2
                or any(type(v) is not int or v<0 for v in namespace)):
            raise ValueError('Invalid Linux worker environment')
    return dict(value, pid_namespace=list(value['pid_namespace']) if value['pid_namespace'] is not None else None)


def _linux_environment():
    boot=Path('/proc/sys/kernel/random/boot_id').read_text(encoding='ascii').strip()
    namespace=Path('/proc/self/ns/pid').stat()
    return boot,[namespace.st_dev,namespace.st_ino]


def _linux(pid):
    boot,namespace=_linux_environment()
    try:
        raw=Path(f'/proc/{pid}/stat').read_bytes()
    except FileNotFoundError:
        # Missing /proc itself is not evidence of process exit.
        Path('/proc/self/stat').read_bytes()
        return None
    closing=raw.rfind(b')');opening=raw.find(b'(')
    if opening<0 or closing<=opening or raw[:opening].strip()!=str(pid).encode('ascii'):
        raise ValueError('Malformed Linux process identity')
    fields=raw[closing+1:].split()
    if len(fields)<20 or len(fields[0])!=1:
        raise ValueError('Incomplete Linux process identity')
    value=validate(dict(platform='linux',pid=pid,started=int(fields[19]),boot_id=boot,pid_namespace=namespace))
    return value,fields[0] not in (b'Z',b'X',b'x')


def _windows(pid):
    import ctypes
    from ctypes import wintypes
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
    kernel.OpenProcess.restype=wintypes.HANDLE
    kernel.GetProcessTimes.argtypes=[wintypes.HANDLE]+[ctypes.POINTER(wintypes.FILETIME)]*4
    kernel.GetProcessTimes.restype=wintypes.BOOL
    kernel.WaitForSingleObject.argtypes=[wintypes.HANDLE,wintypes.DWORD]
    kernel.WaitForSingleObject.restype=wintypes.DWORD
    kernel.CloseHandle.argtypes=[wintypes.HANDLE];kernel.CloseHandle.restype=wintypes.BOOL
    handle=kernel.OpenProcess(0x1000|0x00100000,False,pid)  # Query limited information and synchronize.
    if not handle:
        code=ctypes.get_last_error()
        if code==87:  # Positive, valid DWORD PID no longer names a process.
            return None
        raise ctypes.WinError(code)
    try:
        times=[wintypes.FILETIME() for _ in range(4)]
        if not kernel.GetProcessTimes(handle,*(ctypes.byref(t) for t in times)):
            raise ctypes.WinError(ctypes.get_last_error())
        started=times[0].dwLowDateTime+(times[0].dwHighDateTime<<32)
        state=kernel.WaitForSingleObject(handle,0)
        if state not in (0,258):raise ctypes.WinError(ctypes.get_last_error())
        return validate(dict(platform='win32',pid=pid,started=started,boot_id=None,pid_namespace=None)),state==258
    finally:
        kernel.CloseHandle(handle)


def _observe(pid):
    if sys.platform=='win32':return _windows(pid)
    if sys.platform=='linux':return _linux(pid)
    raise NotImplementedError('Worker identity recovery requires Windows or Linux')


def capture():
    observation=_observe(os.getpid())
    if observation is None or not observation[1]:raise RuntimeError('Cannot identify the active worker')
    return observation[0]


def has_exited(identity):
    value=validate(identity)
    if value['platform']!=sys.platform:raise ValueError('Worker platform changed')
    if sys.platform=='linux':
        boot,namespace=_linux_environment()
        if boot!=value['boot_id'] or namespace!=value['pid_namespace']:
            raise ValueError('Worker boot or PID namespace changed')
    observation=_observe(value['pid'])
    return observation is None or observation[0]!=value or not observation[1]
