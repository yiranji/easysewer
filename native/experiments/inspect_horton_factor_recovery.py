"""Observe native ordinary Horton recovery when its context factor changes.

Calls existing diagnostic libraries; no new infiltration equations or patches.
These local ABI sequences do not prove calendar dispatch in a full project.
"""
import argparse
import ctypes
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import platform
import sys


ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / 'tools/qualify_horton_capacity.py'
spec = importlib.util.spec_from_file_location('horton_capacity', HELPER)
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)
sha = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()


def observe(family, destination):
    destination.mkdir(parents=True, exist_ok=False)
    suffix = 'dll' if family == 'windows' else 'so'
    libraries = {
        name: helper.Trial(ROOT / f'build/horton-capacity-20260930/{folder}-{family}/horton.{suffix}', mode)
        for name, folder, mode in [('pristine', 'pristine', 'pristine'),
                                  ('minimum-cap', 'minimum', 'minimum-cap')]
    }
    assert libraries['pristine'].build['base'] == libraries['minimum-cap'].build['base']
    assert libraries['pristine'].build['probe_sha256'] == libraries['minimum-cap'].build['probe_sha256']
    rows = []
    for cap in (0.025, 1.0):
        for factor in (0.5, 1.0, 2.0):
            for recovery in (0.0, 1.0):
                parameters = [.002, .001, .01, .01, cap]
                steps = [dict(dt=10., rain=.003, method=0, factor=1.),
                         dict(dt=10., rain=0., method=0, factor=factor, recovery=recovery)]
                traces = {name: helper.sequence(library, parameters, [0., 0.], steps)
                          for name, library in libraries.items()}
                assert traces['pristine'] == traces['minimum-cap']
                wet, dry = traces['pristine']
                assert all(math.isfinite(value) for trace in traces.values()
                           for step in trace for value in [step['flux'], *step['state']])
                assert 0 < wet['state'][1] < cap
                assert math.isclose(wet['flux'] * 10, wet['state'][1], rel_tol=1e-14)
                assert dry['flux'] == 0.
                rows.append(dict(parameters=parameters, steps=steps, traces=traces,
                                 cap=cap, dry_factor=factor, recovery_factor=recovery,
                                 wet_below_cap=True,
                                 dry_increases_Fe=dry['state'][1] > wet['state'][1],
                                 dry_exceeds_cap=dry['state'][1] > cap))
    increased = [row for row in rows if row['dry_increases_Fe']]
    assert len(increased) == 2
    assert all(row['dry_factor'] == 2. and row['recovery_factor'] == 1. for row in increased)
    # A large cap removes saturation from both steps: simple min(Fe, Fmax)
    # cannot address the observed increase in this control.
    large = next(row for row in increased if row['cap'] == 1.)
    assert not large['dry_exceeds_cap']
    assert all(row['traces']['pristine'][0]['state'] == row['traces']['pristine'][1]['state']
               for row in rows if row['recovery_factor'] == 0.)
    record = dict(reproduction_verified=True, physical_acceptance=False,
                  production_changed=False, python=sys.version, platform=platform.platform(),
                  native_calls=48, rows=rows, tool_sha256=sha(Path(__file__)),
                  helper_sha256=sha(HELPER), source_base=libraries['pristine'].build['base'],
                  libraries={name: dict(path=str(lib.path), binary_sha256=sha(lib.path),
                                       build_sha256=sha(lib.path.parent/'build.json'))
                             for name, lib in libraries.items()},
                  scope='Fresh ordinary Horton state; one wet and one dry local ABI call. No full-model calendar dispatch.',
                  implications=['Wet capacity clamping alone is insufficient: the 1 ft cap is never reached.',
                                'Clamping dry Fe to Fmax alone leaves the below-cap increase.',
                                'Zero recovery controls isolate the recomputation in the dry recovery branch.',
                                'Factor transition semantics and full monthly-pattern project reproduction remain open.'])
    (destination/'result.json').write_text(json.dumps(record, indent=2)+'\n', encoding='utf-8')
    print(json.dumps(dict(native_calls=48, increased=[dict(cap=row['cap'],
                         wet=row['traces']['pristine'][0], dry=row['traces']['pristine'][1])
                         for row in increased])))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--family', choices=('windows', 'linux'), required=True)
    parser.add_argument('--destination', type=Path, required=True)
    args = parser.parse_args()
    kernel = None
    if os.name == 'nt':
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.GetErrorMode.restype = ctypes.c_uint
        kernel.SetErrorMode.argtypes = [ctypes.c_uint]
        kernel.SetErrorMode.restype = ctypes.c_uint
        previous = kernel.GetErrorMode()
        kernel.SetErrorMode(previous | 1)
    try:
        observe(args.family, args.destination)
    finally:
        if kernel is not None:
            kernel.SetErrorMode(previous)
