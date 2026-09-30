"""Reproduce ordinary Horton state recovery across a real monthly boundary.

Uses existing isolated native libraries and literal INP projects, with a verified
installed package only for parsing/output. Never changes a production library.
"""
import argparse
import hashlib
import json
import math
from pathlib import Path
import platform
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()


def fixture(units,case):
    metric=units=='CMS';depth=25.4 if metric else 1.
    doubled='1 2 1 1 1 1 1 1 1 1 1 1'
    flat='1 1 1 1 1 1 1 1 1 1 1 1'
    text=f'''[OPTIONS]
FLOW_UNITS {units}
INFILTRATION HORTON
FLOW_ROUTING KINWAVE
START_DATE 01/31/2020
START_TIME 23:55:00
REPORT_START_DATE 01/31/2020
REPORT_START_TIME 23:55:00
END_DATE 02/01/2020
END_TIME 00:05:00
REPORT_STEP 00:00:01
WET_STEP 00:00:10
DRY_STEP 00:00:10
ROUTING_STEP 1
VARIABLE_STEP 0
[RAINGAGES]
R INTENSITY 00:01:00 1 TIMESERIES Rain
[OUTFALLS]
O 0 FREE
[SUBCATCHMENTS]
S R O {0.40468564224 if metric else 1} 0 {30.48 if metric else 100} 1 0
[SUBAREAS]
S .015 .2 0 0 25 OUTLET
[INFILTRATION]
S {2*depth:.17g} {.2*depth:.17g} 4 2 {12*depth:.17g}
[REPORT]
SUBCATCHMENTS ALL
NODES ALL
[TIMESERIES]
Rain 01/31/2020 23:55:00 {.1*depth:.17g}
Rain 01/31/2020 23:56:00 {.1*depth:.17g}
Rain 01/31/2020 23:57:00 {.1*depth:.17g}
Rain 01/31/2020 23:58:00 0
Rain 01/31/2020 23:59:00 0
Rain 02/01/2020 00:00:00 0
Rain 02/01/2020 00:05:00 0
'''
    if case=='global-double':text+=f'[ADJUSTMENTS]\nCONDUCTIVITY {doubled}\n'
    elif case=='pattern-double':text+=f'[PATTERNS]\nP MONTHLY {doubled}\n[ADJUSTMENTS]\nINFIL S P\n'
    elif case=='pattern-override':text+=f'[PATTERNS]\nP MONTHLY {flat}\n[ADJUSTMENTS]\nCONDUCTIVITY {doubled}\nINFIL S P\n'
    else:assert case=='constant'
    return text


def main(args):
    package=args.package.resolve();package_record=args.package_record.resolve()
    freeze=json.loads(package_record.read_bytes())
    assert len(freeze['source_digests'])==225
    for name,digest in freeze['source_digests'].items():assert sha(package/name[4:])==digest,name
    sys.path.insert(0,str(package))
    from easysewer.runtime._solver_worker import _configure_error_mode
    from easysewer.model import Model
    from easysewer.io.inp import InpDocument
    _configure_error_mode()
    sys.path.insert(0,str(ROOT/'tools'))
    import qualify_horton_network as network
    from qualify_horton_capacity import Trial
    args.destination=args.destination.resolve();args.destination.mkdir(parents=True,exist_ok=False)
    suffix='dll' if args.family=='windows' else 'so'
    libraries={name:Trial(ROOT/f'build/horton-capacity-20260930/{folder}-{args.family}/horton.{suffix}',mode)
               for name,folder,mode in [('pristine','pristine','pristine'),('minimum','minimum','minimum-cap')]}
    checks=[];rows=[]
    record=dict(reproduction_verified=False,physical_acceptance=False,production_changed=False,
                native_runs_completed=0,python=sys.version,platform=platform.platform(),started=time.time(),
                tool_sha256=sha(Path(__file__)),package_record_sha256=sha(package_record),
                helper_sha256=sha(ROOT/'tools/qualify_horton_network.py'),
                trial_helper_sha256=sha(ROOT/'tools/qualify_horton_capacity.py'),
                libraries={name:dict(path=str(trial.path),sha256=sha(trial.path)) for name,trial in libraries.items()},
                checks=checks,rows=rows,scope='One catchment, ten minutes across Jan/Feb 2020; two unit systems, global and per-catchment monthly factors. No pipes or candidate repair.')
    result_path=args.destination/'result.json'
    def save():result_path.write_text(json.dumps(record,indent=2)+'\n',encoding='utf-8')
    def check(name,condition,**data):checks.append(dict(name=name,passed=bool(condition),**data))
    save()
    try:
        for units in ('CFS','CMS'):
            for case in ('constant','global-double','pattern-double','pattern-override'):
                source=fixture(units,case)
                model=Model.from_document(InpDocument.from_text(source),strict=True)
                assert not model.validate(for_run=True).errors
                traces={};outputs={}
                for name,library in libraries.items():
                    folder=args.destination/(units+'-'+case)/name
                    record['current_call']=str(folder);save()
                    result=network.run(library,folder,source)
                    record['native_runs_completed']+=1
                    traces[name]=result['trace'];outputs[name]=result['artifact_sha256']['model.out']
                    assert all(math.isfinite(v) for row in result['trace'] for v in row)
                    assert all(math.isfinite(v) for v in result['balance_percent'])
                    jumps=[dict(before=left,after=right) for left,right in zip(result['trace'],result['trace'][1:])
                           if right[0]>240 and right[7]>left[7]+1e-12]
                    affected=case in ('global-double','pattern-double')
                    check('monthly_jump_observed' if affected else 'constant_or_override_has_no_jump',
                          len(jumps)==(1 if affected else 0),units=units,case=case,library=name)
                    check('never_reaches_capacity',all(0<=r[7]<r[5] for r in result['trace']),units=units,case=case,library=name)
                    check('zero_runoff_and_final_ponding',max(result['series']['runoff'])==0 and
                          result['final_ponded_depths_ft']==(0.,0.,0.),units=units,case=case,library=name)
                    for jump in jumps:
                        left,right=jump['before'],jump['after']
                        check('dry_month_boundary_state_jump',290<=left[0]<=310 and 300<=right[0]<=320
                              and right[6]<left[6] and right[7]>1.9*left[7]
                              and max(result['series']['rain'][240:])==0
                              and max(result['series']['infiltration'][240:])==0,
                              units=units,case=case,library=name)
                    rows.append(dict(units=units,case=case,library=name,jumps=jumps,
                                     result_path=str(folder/'result.json'),result_sha256=sha(folder/'result.json'),
                                     periods=result['periods'],final=result['final'],balance_percent=result['balance_percent'],
                                     out_sha256=outputs[name]))
                    save()
                check('pristine_and_minimum_identical',traces['pristine']==traces['minimum'] and
                      outputs['pristine']==outputs['minimum'],units=units,case=case)
            unit_rows=[row for row in rows if row['units']==units and row['library']=='pristine']
            by_case={row['case']:row for row in unit_rows}
            check('global_and_pattern_agree',by_case['global-double']['jumps']==by_case['pattern-double']['jumps'],units=units)
            check('override_matches_constant_output',by_case['constant']['out_sha256']==by_case['pattern-override']['out_sha256'],units=units)
        record.update(reproduction_verified=all(c['passed'] for c in checks),finished=time.time())
        save()
        print(json.dumps(dict(reproduction_verified=record['reproduction_verified'],runs=record['native_runs_completed'],
                             failed=[c for c in checks if not c['passed']],seconds=record['finished']-record['started'])))
        return 0 if record['reproduction_verified'] else 1
    except BaseException as error:
        record['error']=dict(type=type(error).__name__,message=str(error));save();raise


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--family',choices=('windows','linux'),required=True)
    parser.add_argument('--package',type=Path,required=True)
    parser.add_argument('--package-record',type=Path,required=True)
    parser.add_argument('--destination',type=Path,required=True)
    raise SystemExit(main(parser.parse_args()))
