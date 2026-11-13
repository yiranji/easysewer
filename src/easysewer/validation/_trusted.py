"""Context-local policy for internal trusted conversion and external callbacks."""

from contextlib import contextmanager
from contextvars import ContextVar


_enabled = ContextVar("easysewer_trusted_inp", default=False)
active = _enabled.get


@contextmanager
def trusted_scope():
    """Skip redundant checks only during a trusted built-in conversion."""
    token = _enabled.set(True)
    try:
        yield
    finally:
        _enabled.reset(token)


@contextmanager
def checking_scope():
    """Keep public API checks active while invoking caller-provided code."""
    token = _enabled.set(False)
    try:
        yield
    finally:
        _enabled.reset(token)
