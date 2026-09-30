"""Private, revocable execution admission shared by launch and crash recovery.

The lock is held by the worker, never inferred from a parent lease or PID. A
revoked receipt prevents a delayed worker with an already-open file descriptor
from entering after recovery releases its lock. Imports load no native code.
"""
import os
from pathlib import Path
import uuid

from .recovery import _Lease


def validate(record, root):
    if (type(record) is not dict or set(record) != {'path','identity','token','revoked'}
            or type(record['token']) is not str or len(record['token']) != 32
            or any(c not in '0123456789abcdef' for c in record['token'])
            or record['path'] != str(Path(root)/('.easysewer-admission-'+record['token']))
            or type(record['revoked']) is not bool
            or type(record['identity']) not in (list,tuple) or len(record['identity']) != 2
            or any(type(v) is not int or v < 0 for v in record['identity'])):
        raise ValueError('Invalid execution admission record')


def create(root):
    token=uuid.uuid4().hex
    path=Path(root)/('.easysewer-admission-'+token)
    lease=_Lease(path,create=True)
    try:
        lease.stream.seek(1)
        lease.stream.write(b'A'+token.encode('ascii'))
        lease.stream.flush();os.fsync(lease.stream.fileno())
        return dict(path=str(path),identity=list(lease.identity),token=token,revoked=False)
    finally:
        lease.close()


def _read(lease, record):
    if lease.identity != tuple(record['identity']):
        raise ValueError('Execution admission file was replaced')
    lease.stream.seek(0)
    value=lease.stream.read(35)
    if value not in (b'0A'+record['token'].encode('ascii'),b'0R'+record['token'].encode('ascii')):
        raise ValueError('Execution admission content changed')
    return value[1:2]


def enter(record):
    validate(record,Path(record['path']).parent)
    lease=_Lease(record['path'])
    try:
        if record['revoked'] or _read(lease,record) != b'A':
            raise ValueError('Execution admission has been revoked')
        return lease
    except BaseException:
        lease.close()
        raise


def revoke(record, *, allow_missing=False):
    validate(record,Path(record['path']).parent)
    path=Path(record['path'])
    if allow_missing and record['revoked'] and not path.exists() and not path.is_symlink():
        return
    try:
        lease=_Lease(path)
    except OSError as error:
        raise RuntimeError(f'Workspace worker is still active or admission is inaccessible: {path}: {error}') from error
    try:
        state=_read(lease,record)
        if record['revoked'] and state != b'R':
            raise ValueError('Execution admission revocation was reversed')
        if state != b'R':
            lease.stream.seek(1);lease.stream.write(b'R')
            lease.stream.flush();os.fsync(lease.stream.fileno())
        # The caller persists this before recording any cleanup inventory.
        record['revoked']=True
    finally:
        lease.close()
