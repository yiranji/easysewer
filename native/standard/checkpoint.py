"""Apply and record the shared complete-checkpoint native transformation."""
import hashlib
from pathlib import Path
import runpy


def patch_checkpoint(contents, *, custom=False):
    root=Path(__file__).resolve().parent.parent/'checkpoint'
    recipe=runpy.run_path(str(root/'patch.py'))
    recipe['patch'](contents,custom=custom)
    return dict(abi=recipe['ABI'],fixes=recipe['FIXES'],
                recipes={name:hashlib.sha256((root/name).read_bytes()).hexdigest()
                         for name in recipe['RECIPE_FILES']})


def validate_checkpoint(record):
    """Refuse incomplete or stale checkpoint preparation before compilation."""
    root=Path(__file__).resolve().parent.parent/'checkpoint'
    recipe=runpy.run_path(str(root/'patch.py'))
    expected=dict(abi=recipe['ABI'],fixes=recipe['FIXES'],
                  recipes={name:hashlib.sha256((root/name).read_bytes()).hexdigest()
                           for name in recipe['RECIPE_FILES']})
    if record!=expected:raise ValueError('Prepared checkpoint profile or source recipe differs')
