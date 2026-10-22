"""Native-only macOS identity APIs, loaded lazily by worker identity observation.

The pure profile deliberately excludes this module. Importing it still performs
no native call; kernel libraries are opened only by explicit observations.
"""
import os

from ._worker_identity import _boot_id, validate


def environment():
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


def observe(pid, *, boot):
    """Observe libproc's fixed proc_bsdinfo ABI; never signal or parse ps text."""
    import ctypes
    import errno
    if type(pid) is not int or not 0<pid<2**31:
        raise ValueError('Invalid macOS process ID')
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
