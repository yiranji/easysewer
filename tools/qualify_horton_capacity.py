"""Check the isolated Modified Horton capacity fix against documented state steps.

Uses actual native functions through the experiment ABI. The fixed examples are
small hand-calculated transitions, not an alternative production solver. Native
state roundtrip here covers the Horton tp/Fe pair; full worker checkpoints remain
a separate qualification requirement.
"""
import argparse
import ctypes
import hashlib
import itertools
import json
import math
from pathlib import Path
import platform
import subprocess
import sys


class Trial:
    def __init__(self,path,mode):
        self.path=Path(path).resolve();self.build=json.loads((self.path.parent/'build.json').read_text(encoding='utf-8'))
        if self.build['mode']!=mode or hashlib.sha256(self.path.read_bytes()).hexdigest()!=self.build['binary_sha256']:
            raise ValueError('Experiment library differs from its build record')
        self.dll=ctypes.CDLL(str(self.path))
        ptr=ctypes.POINTER(ctypes.c_double)
        self.dll.easysewer_horton_trial.argtypes=[ctypes.c_int,ptr,ptr,*([ctypes.c_double]*5),ptr]
        self.dll.easysewer_horton_trial.restype=ctypes.c_int

    def step(self,parameters,state,dt,rain,depth=0.,factor=1.,recovery=1.,method=1):
        p=(ctypes.c_double*5)(*parameters);x=(ctypes.c_double*2)(*state);flux=ctypes.c_double()
        if self.dll.easysewer_horton_trial(method,p,x,dt,rain,depth,factor,recovery,ctypes.byref(flux))!=1:
            raise ValueError('Diagnostic ABI rejected input')
        return dict(flux=flux.value,state=list(x))


def sequence(library,parameters,state,steps):
    result=[]
    for step in steps:
        value=library.step(parameters,state,**step);state=value['state'];result.append(value)
    return result


def qualify(pristine,candidate,destination):
    destination=Path(destination).resolve();destination.mkdir(parents=True,exist_ok=False)
    original=Trial(pristine,'pristine');fixed=Trial(candidate,'minimum-cap')
    if original.build['base']!=fixed.build['base']:raise ValueError('Candidate and pristine source bases differ')
    changed=[name for name,digest in original.build['source_digests'].items() if fixed.build['source_digests'].get(name)!=digest]
    if changed!=['src/solver/infil.c']:raise ValueError('Unexpected native changes')
    if original.build['probe_sha256']!=fixed.build['probe_sha256']:raise ValueError('Diagnostic translation units differ')
    checks=[];traces={}
    def check(name,passed,**data):checks.append(dict(name=name,passed=bool(passed),**data))
    close=lambda a,b:math.isclose(a,b,rel_tol=1e-13,abs_tol=1e-15)
    p=[.01,.001,.1,.01,.1]
    wet=[dict(dt=10.,rain=.0005)]*12
    a=sequence(original,p,[0.,0.],wet);b=sequence(fixed,p,[0.,0.],wet)
    traces['below_minimum_rate']=dict(pristine=a,candidate=b)
    check('subminimum_rain_does_not_consume_excess_capacity',all(close(r['flux'],.0005) and r['state']==[0.,0.] for r in b))
    check('reproduces_original_false_saturation',a[0]['state'][1]==.1 and all(r['flux']==0 for r in a[1:]))
    wet=[dict(dt=10.,rain=.002)]*2
    a=sequence(original,p,[0.,0.],wet);b=sequence(fixed,p,[0.,0.],wet)
    traces['partial_capacity']=dict(pristine=a,candidate=b)
    check('two_partial_steps_preserve_remaining_capacity',all(close(r['flux'],.002) and close(r['state'][1],e) for r,e in zip(b,[.01,.02])))
    capped=[.01,.001,.1,.01,.025]
    a=sequence(original,capped,[0.,0.],[dict(dt=10.,rain=.02)])
    b=sequence(fixed,capped,[0.,0.],[dict(dt=10.,rain=.02)])
    traces['large_step_cap']=dict(pristine=a,candidate=b)
    check('state_is_capped_after_large_step',close(b[0]['state'][1],.025) and close(b[0]['flux'],.01),
        original_excess_state=a[0]['state'][1],cap=.025)
    check('original_can_overshoot_state_cap',a[0]['state'][1]>.025)
    b=sequence(fixed,capped,[0.,0.],[dict(dt=10.,rain=.002)]*4)
    traces['reach_capacity']=b
    check('reaching_capacity_stops_next_wet_step',all(close(r['state'][1],e) and close(r['flux'],f)
        for r,e,f in zip(b,[.01,.02,.025,.025],[.002,.002,.002,0.])))
    b=fixed.step(capped,[0.,.025],10.,0.,recovery=.5)
    expected=.025*math.exp(-.05)
    c=fixed.step(capped,b['state'],10.,.0005)
    traces['dry_then_rewet']=dict(dry=b,rewet=c)
    check('dry_recovery_and_subminimum_rewet',close(b['state'][1],expected) and b['flux']==0 and
        close(c['state'][1],expected) and close(c['flux'],.0005))
    b=fixed.step(p,[0.,0.],10.,0.,depth=.03)
    c=fixed.step(p,[0.,0.],10.,.002,factor=.5)
    traces['ponding_and_factors']=dict(ponded=b,adjusted=c)
    check('ponded_water_uses_native_available_rate',close(b['flux'],.003) and close(b['state'][1],.02))
    check('infiltration_factor_scales_both_rates',close(c['flux'],.002) and close(c['state'][1],.015))
    # The unchanged branches are part of the compatibility boundary, not a claim
    # that their pre-existing treatment of a finite cap is physically complete.
    controls=[]
    for name,method,params in [('horton',0,p),('unlimited-modified',1,p[:-1]+[0.]),
        ('constant-rate',1,[.001,.001,.1,.01,.1]),('zero-decay',1,[.01,.001,0.,.01,.1])]:
        for dt,rain,state,factor,recovery in itertools.product((1.,10.,60.),(0.,.0005,.002),
                ([0.,0.],[0.,.02]),(.5,1.),(.25,1.)):
            left=original.step(params,state,dt,rain,factor=factor,recovery=recovery,method=method)
            right=fixed.step(params,state,dt,rain,factor=factor,recovery=recovery,method=method)
            controls.append(dict(case=name,dt=dt,rain=rain,state=state,factor=factor,recovery=recovery,
                original=left,candidate=right,equal=left==right))
    check('unaffected_branches_byte_value_equal',all(r['equal'] for r in controls),comparisons=len(controls))
    steps=[dict(dt=10.,rain=.002)]*2+[dict(dt=10.,rain=0.,recovery=.5)]*3+[dict(dt=10.,rain=.0005)]*3
    whole=sequence(fixed,capped,[0.,0.],steps);split=3
    request=dict(parameters=capped,state=whole[split-1]['state'],steps=steps[split:])
    request_path=destination/'resume-input.json';request_path.write_text(json.dumps(request)+'\n',encoding='utf-8')
    child_path=destination/'resume-output.json'
    child=subprocess.run([sys.executable,'-B',str(Path(__file__).resolve()),'--candidate',str(fixed.path),
        '--resume',str(request_path),'--destination',str(child_path)],capture_output=True,text=True)
    (destination/'resume.log').write_text(child.stdout+child.stderr,encoding='utf-8');child.check_returncode()
    tail=json.loads(child_path.read_text(encoding='utf-8'))
    check('fresh_process_native_state_roundtrip',tail==whole[split:])
    traces['state_replay']=dict(uninterrupted=whole,split_after=split,resumed=tail)
    record=dict(passed=all(c['passed'] for c in checks),checks=checks,traces=traces,unaffected_controls=controls,
        python=sys.version,platform=platform.platform(),pristine_sha256=original.build['binary_sha256'],
        candidate_sha256=fixed.build['binary_sha256'],changed_native_files=changed,adopted=False,
        tolerance=dict(relative=1e-13,absolute=1e-15,scope='hand-calculated float64 state examples'),
        state_replay_scope='native Horton tp/Fe getter/setter in a fresh process; not full worker checkpoint qualification',
        remaining_scope=['constant-rate finite-cap behavior','full-project hotstart and checkpoint compatibility',
            'formal standard/custom builds and adoption'],
        tool_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
    (destination/'result.json').write_text(json.dumps(record,indent=2)+'\n',encoding='utf-8')
    return record


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--pristine',type=Path)
    p.add_argument('--candidate',type=Path,required=True);p.add_argument('--destination',type=Path,required=True)
    p.add_argument('--resume',type=Path);a=p.parse_args()
    if a.resume:
        request=json.loads(a.resume.read_text(encoding='utf-8'));library=Trial(a.candidate,'minimum-cap')
        result=sequence(library,request['parameters'],request['state'],request['steps'])
        a.destination.write_text(json.dumps(result)+'\n',encoding='utf-8')
    else:
        if a.pristine is None:p.error('--pristine is required for qualification')
        result=qualify(a.pristine,a.candidate,a.destination)
        print(json.dumps({key:result[key] for key in ('passed','checks','candidate_sha256','adopted')}))
        raise SystemExit(not result['passed'])
