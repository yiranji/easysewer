"""Observe whether the reproduced dry monthly state jump affects a second storm."""
import argparse
import hashlib
import json
from pathlib import Path
import platform
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()


def main(args):
    package=args.package.resolve();freeze=json.loads(args.package_record.read_bytes())
    assert len(freeze['source_digests'])==225
    for name,digest in freeze['source_digests'].items():assert sha(package/name[4:])==digest,name
    sys.path[:0]=[str(package),str(ROOT/'tools')]
    import inspect_horton_monthly_recovery as monthly
    import qualify_horton_network as network
    from qualify_horton_capacity import Trial
    from easysewer.io.inp import InpDocument
    from easysewer.model import Model
    from easysewer.runtime._solver_worker import _configure_error_mode
    _configure_error_mode()
    args.destination=args.destination.resolve();args.destination.mkdir(parents=True,exist_ok=False)
    suffix='dll' if args.family=='windows' else 'so'
    libraries={name:Trial(ROOT/f'build/horton-capacity-20260930/{folder}-{args.family}/horton.{suffix}',mode)
               for name,folder,mode in [('pristine','pristine','pristine'),('minimum','minimum','minimum-cap')]}
    record=dict(reproduction_verified=False,physical_acceptance=False,production_changed=False,
                native_runs_completed=0,python=sys.version,platform=platform.platform(),started=time.time(),
                tool_sha256=sha(Path(__file__)),fixture_helper_sha256=sha(Path(monthly.__file__)),
                helper_sha256=sha(Path(network.__file__)),package_record_sha256=sha(args.package_record),
                libraries={name:dict(path=str(trial.path),sha256=sha(trial.path)) for name,trial in libraries.items()},
                checks=[],rows=[],scope='Ten-minute single-catchment month-boundary projects with 0.0078 inch capacity and a second storm; no repair or release accuracy acceptance.')
    def save():(args.destination/'result.json').write_text(json.dumps(record,indent=2)+'\n',encoding='utf-8')
    def check(name,condition,**data):record['checks'].append(dict(name=name,passed=bool(condition),**data))
    save()
    try:
        for units in ('CFS','CMS'):
            depth=25.4 if units=='CMS' else 1.
            results={}
            for case in ('constant','global-double','pattern-double','pattern-override'):
                source=monthly.fixture(units,case)
                old=f'S {2*depth:.17g} {.2*depth:.17g} 4 2 {12*depth:.17g}\n'
                new=f'S {2*depth:.17g} {.2*depth:.17g} 4 2 {.0078*depth:.17g}\n'
                assert source.count(old)==1;source=source.replace(old,new)
                old='Rain 02/01/2020 00:05:00 0\n'
                new=f'''Rain 02/01/2020 00:02:00 {.1*depth:.17g}
Rain 02/01/2020 00:03:00 {.1*depth:.17g}
Rain 02/01/2020 00:04:00 0
Rain 02/01/2020 00:05:00 0
'''
                assert source.count(old)==1;source=source.replace(old,new)
                model=Model.from_document(InpDocument.from_text(source),strict=True)
                assert not model.validate(for_run=True).errors
                for label,library in libraries.items():
                    folder=args.destination/(units+'-'+case)/label
                    record['current_call']=str(folder);save()
                    result=network.run(library,folder,source);record['native_runs_completed']+=1
                    results[(case,label)]=result
                    jumps=[dict(before=a,after=b) for a,b in zip(result['trace'],result['trace'][1:])
                           if 290<=a[0]<=310 and b[7]>a[7]+1e-12]
                    affected=case in ('global-double','pattern-double')
                    check('dry_jump_before_second_storm',len(jumps)==(1 if affected else 0),units=units,case=case,library=label)
                    if affected:
                        check('dry_state_exceeds_capacity',jumps[0]['before'][7]<jumps[0]['before'][5]
                              and jumps[0]['after'][7]>jumps[0]['after'][5]
                              and jumps[0]['after'][6]<jumps[0]['before'][6],units=units,case=case,library=label)
                    check('dry_output_has_no_rain_or_infiltration',max(result['series']['rain'][250:380])==0
                          and max(result['series']['infiltration'][250:380])==0,units=units,case=case,library=label)
                    # Reports are sampled at one second; rates are in in/hr or
                    # mm/hr. Sum is an observed integral of report rates, not
                    # the solver's exact runoff volume or a release tolerance.
                    row=dict(units=units,case=case,library=label,jumps=jumps,
                             final_pervious_ponding_ft=result['final_ponded_depths_ft'][2],
                             second_pulse_infiltration_report_sum=sum(result['series']['infiltration'][430:530])/depth,
                             second_pulse_runoff_report_sum=sum(result['series']['runoff'][430:])/ (0.028316846592 if units=='CMS' else 1.),
                             out_sha256=result['artifact_sha256']['model.out'],balance_percent=result['balance_percent'],
                             result_path=str(folder/'result.json'),result_sha256=sha(folder/'result.json'))
                    record['rows'].append(row);save()
                a,b=results[(case,'pristine')],results[(case,'minimum')]
                check('pristine_minimum_equal',a['trace']==b['trace'] and
                      a['artifact_sha256']['model.out']==b['artifact_sha256']['model.out'],units=units,case=case)
            for label in libraries:
                bycase={r['case']:r for r in record['rows'] if r['units']==units and r['library']==label}
                constant=bycase['constant']
                for case in ('global-double','pattern-double'):
                    row=bycase[case]
                    check('second_storm_infiltration_reduced_and_ponding_increased',
                          row['out_sha256']!=constant['out_sha256']
                          and row['second_pulse_infiltration_report_sum']<constant['second_pulse_infiltration_report_sum']
                          and row['final_pervious_ponding_ft']>constant['final_pervious_ponding_ft'],
                          units=units,case=case,library=label)
                check('pattern_override_restores_control',bycase['pattern-override']['out_sha256']==constant['out_sha256'],units=units,library=label)
                check('global_pattern_complete_output_equal',bycase['global-double']['out_sha256']==bycase['pattern-double']['out_sha256'],units=units,library=label)
        record.update(reproduction_verified=all(r['passed'] for r in record['checks']),finished=time.time());save()
        print(json.dumps(dict(reproduction_verified=record['reproduction_verified'],runs=record['native_runs_completed'],
                             failed=[r for r in record['checks'] if not r['passed']],seconds=record['finished']-record['started'])))
        return 0 if record['reproduction_verified'] else 1
    except BaseException as error:
        record['error']=dict(type=type(error).__name__,message=str(error));save();raise


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--family',choices=('windows','linux'),required=True)
    p.add_argument('--package',type=Path,required=True);p.add_argument('--package-record',type=Path,required=True)
    p.add_argument('--destination',type=Path,required=True)
    raise SystemExit(main(p.parse_args()))
