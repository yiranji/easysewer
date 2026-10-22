"""Read-only process identity, captured in the worker and checked on recovery.

Importing this module loads no native library. Windows/macOS APIs are used only
by explicit observation; POSIX identities are bound to the current kernel boot.
"""
import os
from pathlib import Path
import sys


def _boot_id(value):
    return (type(value) is str and len(value)==36
            and all(c in '0123456789abcdef-' for c in value)
            and {i for i,c in enumerate(value) if c=='-'}=={8,13,18,23})


def validate(value, *, pid=None):
    if (type(value) is not dict or set(value) != {'platform','pid','started','boot_id','pid_namespace'}
            or value['platform'] not in ('win32','linux','darwin')
            or type(value['pid']) is not int or not 0 < value['pid'] < 2**32
            or type(value['started']) is not int or not 0 < value['started'] < 2**64
            or (pid is not None and (value['pid'] != pid or value['platform'] != sys.platform))):
        raise ValueError('Invalid worker process identity')
    if value['platform'] == 'win32':
        if value['boot_id'] is not None or value['pid_namespace'] is not None:
            raise ValueError('Invalid Windows worker identity')
    elif value['platform'] == 'darwin':
        if value['pid'] >= 2**31 or not _boot_id(value['boot_id']) or value['pid_namespace'] is not None:
            raise ValueError('Invalid macOS worker environment')
    else:
        boot=value['boot_id'];namespace=value['pid_namespace']
        if (not _boot_id(boot)
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


def _darwin_environment():
    """Read the per-boot UUID, not the volume UUID or wall-clock boot time."""
    import ctypes
    import errno
    library=ctypes.CDLL('/usr/lib/libSystem.B.dylib',use_errno=True)
    library.sysctlbyname.argtypes=[ctypes.c_char_p,ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_size_t),ctypes.c_void_p,ctypes.c_size_t]
    library.sysctlbyname.restype=ctypes.c_int
    # XNU kern_sysctl.c exposes kern.bootsessionuuid as a read-only UUID string.
    buffer=ctypes.create_string_buffer(37);size=ctypes.c_size_t(len(buffer))
    ctypes.set_errno(0)
    if library.sysctlbyname(b'kern.bootsessionuuid',buffer,ctypes.byref(size),None,0)!=0:
        code=ctypes.get_errno() or errno.EIO
        raise OSError(code,'Cannot read macOS boot identity: '+os.strerror(code))
    if size.value!=37 or buffer.raw[-1:]!=b'\0':
        raise ValueError('Incomplete macOS boot identity')
    boot=buffer.raw[:-1].decode('ascii').lower()
    if not _boot_id(boot):raise ValueError('Invalid macOS boot identity')
    return boot


def _darwin(pid):
    """Observe libproc's fixed proc_bsdinfo ABI; never signal or parse ps text."""
    import ctypes
    import errno
    if type(pid) is not int or not 0<pid<2**31:
        raise ValueError('Invalid macOS process ID')
    boot=_darwin_environment()
    # Public XNU bsd/sys/proc_info.h: proc_bsdinfo, PROC_PIDTBSDINFO (3).
    # uint32_t/uid_t/gid_t are 32 bits on both Intel and Apple Silicon. Name
    # bytes are uninterpreted; start times use the explicit uint64_t fields.
    class BSDInfo(ctypes.Structure):
        _fields_=[(name,ctypes.c_uint32) for name in (
            'flags','status','xstatus','pid','ppid','uid','gid','ruid','rgid','svuid','svgid','reserved')]+[
            ('comm',ctypes.c_char*16),('name',ctypes.c_char*32)]+[
            (name,ctypes.c_uint32) for name in ('nfiles','pgid','pjobc','tdev','tpgid')]+[
            ('nice',ctypes.c_int32),('start_seconds',ctypes.c_uint64),('start_microseconds',ctypes.c_uint64)]
    library=ctypes.CDLL('/usr/lib/libproc.dylib',use_errno=True)
    library.proc_pidinfo.argtypes=[ctypes.c_int,ctypes.c_int,ctypes.c_uint64,ctypes.c_void_p,ctypes.c_int]
    library.proc_pidinfo.restype=ctypes.c_int
    info=BSDInfo();size=ctypes.sizeof(info)
    ctypes.set_errno(0)
    # A nonzero argument includes unreaped zombies, which cannot use files.
    count=library.proc_pidinfo(pid,3,1,ctypes.byref(info),size)
    if count==0:
        code=ctypes.get_errno() or errno.EIO
        if code==errno.ESRCH:return None
        raise OSError(code,'Cannot observe macOS worker: '+os.strerror(code))
    if (count!=size or info.pid!=pid or info.status not in (1,2,3,4,5)
            or not info.start_seconds or info.start_microseconds>=1000000):
        raise ValueError('Incomplete or invalid macOS process identity')
    started=info.start_seconds*1000000+info.start_microseconds
    value=validate(dict(platform='darwin',pid=pid,started=started,boot_id=boot,pid_namespace=None))
    return value,info.status!=5  # SZOMB in XNU bsd/sys/proc.h cannot execute.


def _observe(pid):
    if sys.platform=='win32':return _windows(pid)
    if sys.platform=='linux':return _linux(pid)
    if sys.platform=='darwin':return _darwin(pid)
    raise NotImplementedError('Worker identity recovery requires Windows, Linux, or macOS')


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
    elif sys.platform=='darwin' and _darwin_environment()!=value['boot_id']:
        raise ValueError('Worker boot changed')
    observation=_observe(value['pid'])
    return observation is None or observation[0]!=value or not observation[1]
