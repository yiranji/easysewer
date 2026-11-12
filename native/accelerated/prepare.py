"""Prepare a separately named accelerated backend from the pinned EPA sources."""

import argparse
import hashlib
import json
from pathlib import Path
import runpy
import subprocess
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent
STANDARD = ROOT.parent/'standard'
RECIPE = runpy.run_path(str(ROOT/'patch.py'))
PATCH = RECIPE['PATCH']


def prepare(source, destination):
    """Reuse the unchanged Standard recipe before applying the cumulative patch."""
    source = Path(source).resolve()
    destination = Path(destination).resolve()
    # Use an isolated interpreter so Standard's existing helper imports cannot
    # be confused with this backend's preparation or patch modules.
    command = [sys.executable, '-B', str(STANDARD/'prepare.py'),
               '--source', str(source), '--destination', str(destination)]
    subprocess.run(command, check=True, capture_output=True, text=True)
    receipt = destination/'prepared-source.json'
    standard = json.loads(receipt.read_text(encoding='utf-8'))
    if standard['patch'] != RECIPE['BASE_PATCH']:
        raise ValueError('Accelerated preparation requires Standard revision 16')
    changed = ('src/solver/dynwave.c', 'src/solver/swmm5.c')
    contents = {name: (destination/name).read_bytes() for name in changed}
    acceleration = RECIPE['patch'](contents)
    for name, raw in contents.items():
        (destination/name).write_bytes(raw)
    record = dict(standard)
    record.update(patch=PATCH, standard_source=standard, acceleration=acceleration,
                  recipe={name: hashlib.sha256((ROOT/name).read_bytes()).hexdigest()
                          for name in ('prepare.py', *RECIPE['RECIPE_FILES'])},
                  files={**standard['files'],
                         **{name: hashlib.sha256(raw).hexdigest()
                            for name, raw in contents.items()}})
    receipt.write_text(json.dumps(record, indent=2)+'\n', encoding='utf-8')
    return record


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True)
    parser.add_argument('--destination', required=True)
    args = parser.parse_args()
    record = prepare(args.source, args.destination)
    print(json.dumps(dict(patch=record['patch'],
                          acceleration_version=record['acceleration']['version'],
                          files=len(record['files']))))
