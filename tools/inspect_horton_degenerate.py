"""Record existing Horton shortcut behavior; this tool does not fix the solver.

Only calls the isolated diagnostic ABI, in this process with no active project.
Reproduction success must not be interpreted as physical acceptance.
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

_helper_path = Path(__file__).with_name('qualify_horton_capacity.py')
_spec = importlib.util.spec_from_file_location('_horton_capacity_helper', _helper_path)
_helper = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_helper)
Trial, sequence = _helper.Trial, _helper.sequence


def inspect(pristine, minimum, destination):
    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=False)
    original = Trial(pristine, 'pristine')
    revised = Trial(minimum, 'minimum-cap')
    assert original.build['base'] == revised.build['base']
    assert original.build['probe_sha256'] == revised.build['probe_sha256']
    assert [name for name, digest in original.build['source_digests'].items()
            if revised.build['source_digests'].get(name) != digest] == ['src/solver/infil.c']
    close = lambda a, b: math.isclose(a, b, rel_tol=1e-13, abs_tol=1e-15)
    wet = dict(dt=10., rain=.003)
    cases = [
        dict(name='horton_constant_wet', method=0,
             parameters=[.001, .001, .1, .01, .025], state=[0., 0.], steps=[wet]*4,
             expected_fluxes=[.001]*4, expected_excess_or_cumulative=[0.]*4,
             issue='Observed total infiltration exceeds supplied finite capacity.'),
        dict(name='horton_zero_decay_wet', method=0,
             parameters=[.002, .001, 0., .01, .025], state=[0., 0.], steps=[wet]*3,
             expected_fluxes=[.002]*3, expected_excess_or_cumulative=[0.]*3,
             issue='Observed total infiltration exceeds supplied finite capacity.'),
        dict(name='modified_zero_decay_wet', method=1,
             parameters=[.002, .001, 0., .01, .025], state=[0., 0.], steps=[wet]*3,
             expected_fluxes=[.002]*3, expected_excess_or_cumulative=[0.]*3,
             first_step_documented_excess=.01,
             issue='Excess state does not increase despite flux above the minimum rate.'),
        dict(name='modified_constant_fresh_control', method=1,
             parameters=[.001, .001, .1, .01, .025], state=[0., 0.], steps=[wet]*4,
             expected_fluxes=[.001]*4, expected_excess_or_cumulative=[0.]*4,
             issue=None),
        dict(name='modified_constant_injected_dry', method=1,
             parameters=[.001, .001, .1, .01, .025], state=[0., .02],
             steps=[dict(dt=10., rain=0.)], expected_fluxes=[0.],
             expected_excess_or_cumulative=[.02], documented_dry_excess=.02*math.exp(-.1),
             state_origin='Artificial nonzero state; not claimed reachable from a fresh constant-rate project.',
             issue='Injected nonzero excess state does not recover during the dry step.'),
        dict(name='modified_nondegenerate_dry_control', method=1,
             parameters=[.002, .001, .1, .01, .025], state=[0., .02],
             steps=[dict(dt=10., rain=0.)], expected_fluxes=[0.],
             expected_excess_or_cumulative=[.02*math.exp(-.1)], issue=None),
    ]
    checks = []
    for case in cases:
        steps = [dict(step, method=case['method']) for step in case['steps']]
        traces = {name: sequence(library, case['parameters'], case['state'], steps)
                  for name, library in [('pristine', original), ('minimum-cap', revised)]}
        case['traces'] = traces
        assert traces['pristine'] == traces['minimum-cap'], case['name']
        trace = traces['minimum-cap']
        assert all(close(row['flux'], f) and close(row['state'][1], fe)
                   for row, f, fe in zip(trace, case['expected_fluxes'], case['expected_excess_or_cumulative']))
        case['total_infiltration_ft'] = sum(row['flux']*step['dt'] for row, step in zip(trace, steps))
        if case['method'] == 0:
            assert case['total_infiltration_ft'] > case['parameters'][4]
        if 'first_step_documented_excess' in case:
            assert not close(trace[0]['state'][1], case['first_step_documented_excess'])
        if 'documented_dry_excess' in case:
            assert not close(trace[0]['state'][1], case['documented_dry_excess'])
        checks.append(dict(name=case['name'], reproduced=True))
    record = dict(
        reproduction_verified=True, physical_acceptance=False, production_changed=False,
        platform=platform.platform(), python=sys.version, cases=cases, checks=checks,
        diagnostic_native_calls=2*sum(len(case['steps']) for case in cases),
        libraries={name: dict(path=str(library.path), binary_sha256=library.build['binary_sha256'],
                             build_sha256=hashlib.sha256((library.path.parent/'build.json').read_bytes()).hexdigest())
                   for name, library in [('pristine', original), ('minimum-cap', revised)]},
        source_base=original.build['base'], probe_sha256=original.build['probe_sha256'],
        scripts={path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                 for path in (Path(__file__), Path(__file__).with_name('qualify_horton_capacity.py'))},
        units=dict(rates='ft/s', time='s', state_depth='ft', decay_and_regen='1/s'),
        reference='https://nepis.epa.gov/Exe/ZyPURL.cgi?Dockey=P100NYRA.TXT',
        reference_sections=['4.2.3, printed pp. 93-94', '4.3.3, printed p. 103'],
        limitations=[
            'Diagnostic ABI calls only; no full project or worker checkpoint qualification.',
            'Modified Horton Fe stores excess infiltration, not total infiltration.',
            'Fresh constant-rate Modified Horton is a control, not a demonstrated capacity defect.',
            'No new dry-recovery equation or saturation-boundary interpretation is adopted.',
            'Reproduction does not authorize or validate a production solver change.',
        ])
    (destination/'result.json').write_text(json.dumps(record, indent=2)+'\n', encoding='utf-8')
    return record


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pristine', type=Path, required=True)
    parser.add_argument('--minimum', type=Path, required=True)
    parser.add_argument('--destination', type=Path, required=True)
    args = parser.parse_args()
    kernel = None
    previous = None
    if os.name == 'nt':
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.GetErrorMode.argtypes = []; kernel.GetErrorMode.restype = ctypes.c_uint
        kernel.SetErrorMode.argtypes = [ctypes.c_uint]; kernel.SetErrorMode.restype = ctypes.c_uint
        previous = kernel.GetErrorMode()
        kernel.SetErrorMode(previous | 0x0001)
    try:
        result = inspect(args.pristine, args.minimum, args.destination)
        print(json.dumps({name: result[name] for name in (
            'reproduction_verified', 'physical_acceptance', 'production_changed', 'diagnostic_native_calls', 'checks')}))
    finally:
        if kernel is not None:
            kernel.SetErrorMode(previous)
