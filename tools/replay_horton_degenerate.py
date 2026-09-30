"""Replay native Horton tp/Fe in a fresh process; not a worker checkpoint gate."""
import argparse
import ctypes
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import subprocess
import sys

helper_path=Path(__file__).with_name('qualify_horton_capacity.py')
spec=importlib.util.spec_from_file_location('_horton_replay_helper',helper_path)
helper=importlib.util.module_from_spec(spec);spec.loader.exec_module(helper)
sha=lambda path:hashlib.sha256(Path(path).read_bytes()).hexdigest()


def replay(library,mode,destination,request_path=None):
    trial=helper.Trial(library,mode)
    if request_path is not None:
        request=json.loads(request_path.read_bytes())
        if request['library_sha256']!=trial.build['binary_sha256'] or request['mode']!=mode:
            raise ValueError('Replay request library identity differs')
        traces={row['id']:helper.sequence(trial,row['parameters'],row['state'],row['steps'])
                for row in request['requests']}
        result=dict(pid=os.getpid(),python=sys.version,library_sha256=trial.build['binary_sha256'],
            native_calls=sum(len(row['steps']) for row in request['requests']),traces=traces,
            tool_sha256=sha(__file__),helper_sha256=sha(helper_path))
        with destination.open('x',encoding='utf-8') as stream:json.dump(result,stream,indent=2)
        return result
    destination.mkdir(parents=True,exist_ok=False)
    wet=dict(dt=10.,rain=.003);dry=dict(dt=10.,rain=0.)
    steps=[wet]*4+[dry]*2+[wet]*2
    scenarios=[
        ('zero_decay_finite',1,[.002,.001,0.,.01,.025],[0.,0.]),
        ('zero_decay_unlimited',1,[.002,.001,0.,.01,0.],[0.,0.]),
        ('constant_fresh',1,[.001,.001,.1,.01,.025],[0.,0.]),
        ('constant_injected_state',1,[.001,.001,.1,.01,.025],[9.,.025]),
        ('ordinary_horton_control',0,[.002,.001,.1,.01,.025],[0.,0.]),
        ('nondegenerate_modified_control',1,[.002,.001,.1,.01,.025],[0.,0.]),
    ]
    cases=[];requests=[];expected={}
    for name,method,parameters,state in scenarios:
        actual_steps=[dict(step,method=method) for step in steps]
        whole=helper.sequence(trial,parameters,state,actual_steps)
        cases.append(dict(name=name,method=method,parameters=parameters,initial_state=state,
                          steps=actual_steps,whole=whole))
        for split in (1,4,6):
            key=name+'-'+str(split)
            requests.append(dict(id=key,parameters=parameters,state=whole[split-1]['state'],steps=actual_steps[split:]))
            expected[key]=whole[split:]
    request_path=destination/'requests.json';output_path=destination/'child-results.json'
    request=dict(mode=mode,library_sha256=trial.build['binary_sha256'],requests=requests)
    request_path.write_text(json.dumps(request,indent=2)+'\n',encoding='utf-8')
    command=[sys.executable,'-B',str(Path(__file__).resolve()),'--library',str(trial.path),
        '--mode',mode,'--destination',str(output_path),'--request',str(request_path)]
    child=subprocess.run(command,capture_output=True,text=True,timeout=30)
    (destination/'child.log').write_text(child.stdout+child.stderr,encoding='utf-8')
    child.check_returncode();response=json.loads(output_path.read_bytes())
    checks=[dict(id=row['id'],exact_tail_equal=response['traces'].get(row['id'])==expected[row['id']]) for row in requests]
    identity=(response['pid']!=os.getpid() and response['python']==sys.version and
        response['library_sha256']==trial.build['binary_sha256'] and response['tool_sha256']==sha(__file__)
        and response['helper_sha256']==sha(helper_path) and set(response['traces'])==set(expected))
    record=dict(replay_passed=identity and all(row['exact_tail_equal'] for row in checks),
        mode=mode,parent_pid=os.getpid(),child_pid=response['pid'],python=sys.version,platform=platform.platform(),
        child_identity_verified=identity,checks=checks,cases=cases,command=command,
        parent_native_calls=sum(len(case['steps']) for case in cases),child_native_calls=response['native_calls'],
        library_sha256=trial.build['binary_sha256'],build_sha256=sha(trial.path.parent/'build.json'),
        tool_sha256=sha(__file__),helper_sha256=sha(helper_path),
        request_sha256=sha(request_path),response_sha256=sha(output_path),child_returncode=child.returncode,
        full_worker_checkpoint_qualified=False,physical_acceptance=False,candidate_qualified=False,adopted=False,
        scope='tp/Fe getter/setter state only, same library/platform/interpreter, three split points per scenario; one fresh child handles the 18 independent tails.',
        limitations=['Replay equality does not correct existing physical-state errors.',
            'Artificial constant-rate nonzero state is not claimed reachable from a fresh constant-rate model.',
            'No complete model clock, file cursor, worker protocol or cross-library resume is tested.'])
    (destination/'result.json').write_text(json.dumps(record,indent=2)+'\n',encoding='utf-8')
    return record


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--library',type=Path,required=True)
    parser.add_argument('--mode',choices=('minimum-cap','modified-degenerate'),required=True)
    parser.add_argument('--destination',type=Path,required=True)
    parser.add_argument('--request',type=Path)
    args=parser.parse_args();kernel=None;previous=None
    if os.name=='nt':
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.GetErrorMode.argtypes=[];kernel.GetErrorMode.restype=ctypes.c_uint
        kernel.SetErrorMode.argtypes=[ctypes.c_uint];kernel.SetErrorMode.restype=ctypes.c_uint
        previous=kernel.GetErrorMode();kernel.SetErrorMode(previous|1)
    try:
        result=replay(args.library,args.mode,args.destination.resolve(),args.request)
        if args.request is None:
            print(json.dumps({name:result[name] for name in ('replay_passed','mode','parent_pid','child_pid',
                'parent_native_calls','child_native_calls','full_worker_checkpoint_qualified','candidate_qualified')}))
    finally:
        if kernel is not None:kernel.SetErrorMode(previous)
    if args.request is None:raise SystemExit(not result['replay_passed'])
