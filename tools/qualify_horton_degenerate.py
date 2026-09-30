"""Fixed state examples and unchanged-branch comparisons for the isolated candidate.

Omit --candidate to measure known baseline failures, never to claim qualification.
This is a local native-state gate; complete projects and worker resumes are separate.
"""
import argparse
import ctypes
import hashlib
import importlib.util
import itertools
import json
import math
import os
from pathlib import Path
import platform
import sys

_path=Path(__file__).with_name('qualify_horton_capacity.py')
_spec=importlib.util.spec_from_file_location('_capacity_trial',_path)
_helper=importlib.util.module_from_spec(_spec);_spec.loader.exec_module(_helper)


def qualify(baseline_path,candidate_path,destination):
    destination=Path(destination).resolve();destination.mkdir(parents=True,exist_ok=False)
    baseline=_helper.Trial(baseline_path,'minimum-cap')
    candidate=(_helper.Trial(candidate_path,'modified-degenerate') if candidate_path else baseline)
    if baseline.build['base']!=candidate.build['base'] or baseline.build['probe_sha256']!=candidate.build['probe_sha256']:
        raise ValueError('Source base or diagnostic ABI differs')
    changed=[name for name,digest in baseline.build['source_digests'].items()
             if candidate.build['source_digests'].get(name)!=digest]
    if changed != (['src/solver/infil.c'] if candidate_path else []):
        raise ValueError('Unexpected native source changes')
    dry=.02*math.exp(-.1);recovered=.025*math.exp(-.1)
    p=[.002,.001,0.,.01,.025];constant=[.001,.001,.1,.01,.025]
    wet=dict(dt=10.,rain=.003)
    cases=[
        dict(name='zero_decay_finite_capacity',parameters=p,state=[0.,0.],steps=[wet]*4,
             expected=[(.002,.01),(.002,.02),(.002,.025),(0.,.025)]),
        dict(name='zero_decay_dry_rewet',parameters=p,state=[0.,.025],
             steps=[dict(dt=10.,rain=0.),wet],expected=[(0.,recovered),(.002,.025)]),
        dict(name='zero_decay_unlimited_state',parameters=p[:-1]+[0.],state=[0.,0.],steps=[wet]*3,
             expected=[(.002,.01),(.002,.02),(.002,.03)]),
        dict(name='constant_fresh_control',parameters=constant,state=[0.,0.],steps=[wet]*4,
             expected=[(.001,0.)]*4),
        dict(name='constant_injected_saturated',parameters=constant,state=[7.,.025],steps=[wet],
             expected=[(0.,.025)],state_origin='Artificial state, not claimed reachable from a fresh constant-rate project.'),
        dict(name='constant_injected_dry',parameters=constant,state=[7.,.02],steps=[dict(dt=10.,rain=0.)],
             expected=[(0.,dry)],state_origin='Artificial state, not claimed reachable from a fresh constant-rate project.'),
        dict(name='zero_rates_injected_dry',parameters=[0.,0.,.1,.01,.025],state=[7.,.02],
             steps=[dict(dt=10.,rain=0.)],expected=[(0.,dry)],state_origin='Artificial nonzero state.'),
        dict(name='zero_decay_ponded',parameters=p,state=[0.,0.],steps=[dict(dt=10.,rain=0.,depth=.02)],
             expected=[(.002,.01)]),
        dict(name='zero_decay_infiltration_factor',parameters=p,state=[0.,0.],steps=[dict(wet,factor=.5)],
             expected=[(.001,.005)]),
        dict(name='zero_recovery_control',parameters=p,state=[7.,.02],steps=[dict(dt=10.,rain=0.,recovery=0.)],
             expected=[(0.,.02)]),
        dict(name='effective_zero_existing_threshold',parameters=p,state=[7.,.02],
             steps=[dict(dt=10.,rain=1e-12)],expected=[(0.,dry)],
             scope='Preserve the existing nondegenerate branch effective-zero rule (ZERO=1e-10).'),
    ]
    close=lambda a,b:math.isclose(a,b,rel_tol=1e-13,abs_tol=1e-15)
    checks=[]
    for case in cases:
        trace=_helper.sequence(candidate,case['parameters'],case['state'],case['steps'])
        assert len(trace)==len(case['expected'])
        case['trace']=trace
        passed=all(close(row['flux'],expected[0]) and close(row['state'][1],expected[1])
                   and row['state'][0]==case['state'][0] for row,expected in zip(trace,case['expected']))
        checks.append(dict(name=case['name'],passed=passed))
    controls=[]
    for method,dt,rain,state,factor,recovery,cap in itertools.product(
            (0,1),(1.,10.,60.),(0.,.0005,.003),([0.,0.],[7.,.01],[20.,.025]),(.5,1.),(0.,.5,1.),(0.,.025)):
        parameters=[.002,.001,.1,.01,cap]
        left=baseline.step(parameters,state,dt,rain,factor=factor,recovery=recovery,method=method)
        right=candidate.step(parameters,state,dt,rain,factor=factor,recovery=recovery,method=method)
        controls.append(dict(method=method,dt=dt,rain=rain,state=state,factor=factor,recovery=recovery,cap=cap,
                             baseline=left,candidate=right,equal=left==right))
    checks.append(dict(name='unchanged_nondegenerate_branches',passed=all(row['equal'] for row in controls),
                       comparisons=len(controls)))
    record=dict(phase='candidate-qualification' if candidate_path else 'baseline-reproduction',
        passed=all(row['passed'] for row in checks),adopted=False,full_project_qualified=False,
        checks=checks,cases=cases,unchanged_controls=controls,
        native_calls=sum(len(case['steps']) for case in cases)+2*len(controls),
        python=sys.version,platform=platform.platform(),
        baseline_sha256=baseline.build['binary_sha256'],tested_sha256=candidate.build['binary_sha256'],
        baseline_build_sha256=hashlib.sha256((baseline.path.parent/'build.json').read_bytes()).hexdigest(),
        tested_build_sha256=hashlib.sha256((candidate.path.parent/'build.json').read_bytes()).hexdigest(),
        probe_sha256=baseline.build['probe_sha256'],source_base=baseline.build['base'],changed_native_files=changed,
        scripts={path.name:hashlib.sha256(path.read_bytes()).hexdigest() for path in (Path(__file__),_path)},
        tolerance=dict(relative=1e-13,absolute=1e-15,scope='Fixed local hand-calculated state examples only'),
        reference='https://nepis.epa.gov/Exe/ZyPURL.cgi?Dockey=P100NYRA.TXT',
        reference_section='4.3.3, printed page 103; saturation boundary and effective-zero rule retained from existing normal branch',
        remaining=['Candidate builds and full projects','Ordinary Horton capacity/time recovery consistency',
                   'Fresh-process state replay, formal backends, worker checkpoints and numeric identity'])
    (destination/'result.json').write_text(json.dumps(record,indent=2)+'\n',encoding='utf-8')
    return record


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline',type=Path,required=True)
    parser.add_argument('--candidate',type=Path)
    parser.add_argument('--destination',type=Path,required=True)
    args=parser.parse_args();kernel=None;previous=None
    if os.name=='nt':
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.GetErrorMode.argtypes=[];kernel.GetErrorMode.restype=ctypes.c_uint
        kernel.SetErrorMode.argtypes=[ctypes.c_uint];kernel.SetErrorMode.restype=ctypes.c_uint
        previous=kernel.GetErrorMode();kernel.SetErrorMode(previous|1)
    try:
        record=qualify(args.baseline,args.candidate,args.destination)
        print(json.dumps(dict(phase=record['phase'],passed=record['passed'],native_calls=record['native_calls'],
                              checks=record['checks'])))
    finally:
        if kernel is not None:kernel.SetErrorMode(previous)
    raise SystemExit(not record['passed'])
