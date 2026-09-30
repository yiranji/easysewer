"""Apply and verify the shared path and scratch ownership transformation."""
import hashlib
from pathlib import Path
import runpy


def _recipe():
    root = Path(__file__).resolve().parent.parent/'path_io'
    recipe = runpy.run_path(str(root/'patch.py'))
    record = dict(revision=1, recipes={
        name: hashlib.sha256((root/name).read_bytes()).hexdigest()
        for name in recipe['RECIPE_FILES']})
    return recipe, record


def patch_path_io(contents, *, custom=False):
    recipe, record = _recipe()
    recipe['patch'](contents, custom=custom)
    return record


def validate_path_io(record):
    if record != _recipe()[1]:
        raise ValueError('Prepared path I/O profile or source recipe differs')
