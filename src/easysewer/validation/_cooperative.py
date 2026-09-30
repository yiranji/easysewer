"""Scoped cooperative work checks, independent of runtime/native backends."""

from contextlib import contextmanager
from contextvars import ContextVar


_active = ContextVar('easysewer_work_checkpoint', default=None)


class _CheckpointRaised(BaseException):
    # Data readers deliberately translate ValueError/OSError into diagnostics.
    # A caller's checkpoint failure must cross those handlers unchanged.
    def __init__(self, error):
        self.error = error
        self.traceback = error.__traceback__
        self.context = error.__context__
        self.cause = error.__cause__
        self.suppress_context = error.__suppress_context__


def checkpoint():
    callback = _active.get()
    if callback is not None:
        try:
            callback()
        except _CheckpointRaised:
            raise
        except BaseException as error:
            raise _CheckpointRaised(error) from None


def checkpointed(values, *, interval=256):
    """Poll bounded work batches; an inactive scope adds no per-item checks."""
    if _active.get() is None:
        yield from values
        return
    checkpoint()
    for index, value in enumerate(values):
        if index and index % interval == 0:
            checkpoint()
        yield value
    checkpoint()


@contextmanager
def checkpoint_scope(callback):
    if callback is not None and not callable(callback):
        raise TypeError('checkpoint must be callable or None')
    parent = _active.get()
    token = _active.set(callback if callback is not None else parent)
    try:
        checkpoint()
        yield
        checkpoint()
    except _CheckpointRaised as interrupted:
        if parent is not None:
            raise  # Only the outermost boundary unwraps the original exception.
        try:
            raise interrupted.error.with_traceback(interrupted.traceback)
        finally:
            # Raising across the private control-flow exception would otherwise
            # replace an implicit exception chain or suppress the caller's cause.
            interrupted.error.__context__ = interrupted.context
            interrupted.error.__cause__ = interrupted.cause
            interrupted.error.__suppress_context__ = interrupted.suppress_context
    finally:
        _active.reset(token)
