"""Locate optional, separately preserved historical reader installations.

This only checks the fixture layout. The callers run the real reader in an
isolated subprocess and verify its import path before testing rejection.
"""

import os
from pathlib import Path
import unittest


HISTORY_ROOT_ENV = "EASYSEWER_DIRECTORY_HISTORY_ROOT"


def historical_package(candidate, *, default_root=None):
    """Return a candidate import root, skipping only absent optional fixtures.

    An explicit environment setting is an instruction to exercise history;
    invalid settings must fail, never turn requested coverage into a skip.
    ``default_root`` lets helper tests use temporary directories instead of
    depending on any historical installation on the developer's machine.
    """
    explicit = HISTORY_ROOT_ENV in os.environ
    if explicit:
        configured = os.environ[HISTORY_ROOT_ENV]
        if not configured.strip():
            raise AssertionError(f"{HISTORY_ROOT_ENV} is explicitly set but empty")
        root = Path(configured).expanduser().absolute()
    else:
        root = (Path(default_root) if default_root is not None
                else Path(__file__).resolve().parent.parent).absolute()

    def missing(path):
        detail = f"historical reader {candidate} is unavailable: {path} does not exist"
        if explicit:
            raise AssertionError(f"{HISTORY_ROOT_ENV} is explicitly set; {detail}")
        raise unittest.SkipTest(
            f"Optional {detail}; set {HISTORY_ROOT_ENV} to preserved candidate installations"
        )

    # Check the directory entry before following links: a dangling fixture link
    # is a malformed installation, not an absent optional fixture.
    if not os.path.lexists(root):
        missing(root)
    if not root.is_dir():
        raise AssertionError(f"Invalid {HISTORY_ROOT_ENV} root: {root} is not a directory")
    root = root.resolve()
    package = root / candidate
    if not os.path.lexists(package):
        missing(package)
    if not package.is_dir():
        raise AssertionError(f"Invalid historical reader {candidate}: {package} is not a directory")
    initializer = package / "easysewer" / "__init__.py"
    if not initializer.is_file():
        raise AssertionError(
            f"Invalid historical reader {candidate}: expected package file {initializer}"
        )
    return package
