"""Observe ordinary Horton's reachable wet-to-dry capacity state, without patching it."""
from pathlib import Path
import argparse,ctypes,hashlib,importlib.util,json,os,platform,sys

batch=Path(__file__).resolve().parent;root=batch.parents[1]
path=root/'tools/qualify_horton_capacity.py'
spec=importlib.util.spec_from_file_location('capacity_probe',path)
helper=importlib.util.module_from_spec(spec);spec.loader.exec_module(helper)
parser=argparse.ArgumentParser();parser.add_argument('--family',choices=('windows','linux'),required=True)
parser.add_argument('--destination',type=Path,required=True);args=parser.parse_args()
args.destination.mkdir(parents=True,exist_ok=False)
kernel=None;previous=None
if os.name=='nt':
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.GetErrorMode.argtypes=[];kernel.GetErrorMode.restype=ctypes.c_uint
    kernel.SetErrorMode.argtypes=[ctypes.c_uint];kernel.SetErrorMode.restype=ctypes.c_uint
    previous=kernel.GetErrorMode();kernel.SetErrorMode(previous|1)
try:
    suffix='dll' if args.family=='windows' else 'so'
    libraries={name:helper.Trial(root/f'build/horton-capacity-20260930/{folder}-{args.family}/horton.{suffix}',mode)
               for name,folder,mode in [('pristine','pristine','pristine'),('minimum-cap','minimum','minimum-cap')]}
    rows=[]
    for decay in (.1,.01,.001,.00001,0.):
        parameters=[.002,.001,decay,.01,.025]
        steps=[dict(dt=10.,rain=.003,method=0)]*4+[dict(dt=10.,rain=0.,method=0)]*2
        traces={name:helper.sequence(library,parameters,[0.,0.],steps) for name,library in libraries.items()}
        assert traces['pristine']==traces['minimum-cap']
        trace=traces['minimum-cap'];before=trace[3]['state'][1];after=trace[4]['state'][1]
        rows.append(dict(decay=decay,parameters=parameters,steps=steps,traces=traces,
                         first_dry_increases_Fe=after>before,first_dry_exceeds_cap=after>parameters[4],
                         wet_state=trace[3]['state'],first_dry_state=trace[4]['state']))
    assert any(row['first_dry_increases_Fe'] and row['first_dry_exceeds_cap'] for row in rows)
    record=dict(reproduction_verified=True,physical_acceptance=False,production_changed=False,
        python=sys.version,platform=platform.platform(),rows=rows,native_calls=60,
        source_base=libraries['pristine'].build['base'],tool_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        helper_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        libraries={name:dict(binary_sha256=library.build['binary_sha256'],path=str(library.path)) for name,library in libraries.items()},
        scope='Ordinary Horton only; local ABI wet-to-dry sequence from fresh state; no full project claim.')
    (args.destination/'result.json').write_text(json.dumps(record,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(dict(native_calls=60,rows=[{key:row[key] for key in (
        'decay','wet_state','first_dry_state','first_dry_increases_Fe','first_dry_exceeds_cap')} for row in rows])))
finally:
    if kernel is not None:kernel.SetErrorMode(previous)
