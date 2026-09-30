"""Compare the isolated Modified Horton candidate on frozen full-project inputs.

--preflight checks inputs and helper identity without loading native code.
Actual qualification requires a verified installed package and both diagnostic
libraries. HOTSTART checks physical state; worker checkpoint identity is separate.
"""
import argparse
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import platform
import struct
import sys


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def preflight(inputs):
    inputs=Path(inputs).resolve()
    manifest=json.loads((inputs/'manifest.json').read_bytes())
    cases={'horton','green-ampt','modified-green-ampt','curve-number','unlimited',
        'constant-rate','zero-decay','finite-cap','small-cap','zero-decay-small-cap',
        'zero-decay-unlimited','ordinary-constant','ordinary-zero-decay','zero-rates'}
    units={'CFS','GPM','MGD','CMS','LPS','MLD'}
    rows=manifest['fixtures']
    if (len(rows)!=84 or {(row['units'],row['case']) for row in rows} !=
            {(unit,case) for unit in units for case in cases}):
        raise ValueError('Full 84-fixture matrix is required')
    if sha(Path(__file__).with_name('qualify_horton_network.py'))!=manifest['fixture_helper_sha256']:
        raise ValueError('Previously verified full-project helper differs')
    for row in rows:
        filename=row['units']+'-'+row['case']+'.inp'
        if Path(row['path']).name!=filename or sha(inputs/filename)!=row['sha256']:
            raise ValueError('Fixture identity differs: '+filename)
        expected='different' if row['case']=='zero-decay-small-cap' else 'equal'
        changed=row['case'] in ('zero-decay','zero-decay-small-cap','zero-decay-unlimited')
        if row['expected_output_relation']!=expected or row['expected_state_change']!=changed:
            raise ValueError('Fixture expectations differ: '+filename)
    return dict(input_manifest_sha256=sha(inputs/'manifest.json'),manifest=manifest,
        inputs_verified=True,native_executed=False,candidate_qualified=False,
        scripts={name:sha(Path(__file__).with_name(name)) for name in (
            'qualify_horton_degenerate_projects.py','qualify_horton_network.py','qualify_horton_capacity.py')})


def installed_package(directory,record_path):
    directory=Path(directory).resolve()
    record=json.loads(Path(record_path).read_bytes())
    files={name[4:]:digest for name,digest in record['source_digests'].items()
           if name.startswith('src/easysewer/')}
    if len(files)!=225:
        raise ValueError('Expected the current verified 225-member native package')
    for name,digest in files.items():
        if sha(directory/name)!=digest:raise ValueError('Installed package differs: '+name)
    sys.path.insert(0,str(directory))
    import easysewer
    if Path(easysewer.__file__).resolve()!=directory/'easysewer/__init__.py':
        raise ValueError('Unexpected imported package')
    return dict(directory=str(directory),record_sha256=sha(record_path),member_count=len(files))


def qualify(args,destination,prepared):
    package=installed_package(args.package,args.package_record)
    # Load exact adjacent tools, including in embedded Python with an isolated path.
    capacity_path=Path(__file__).with_name('qualify_horton_capacity.py')
    spec=importlib.util.spec_from_file_location('qualify_horton_capacity',capacity_path)
    capacity=importlib.util.module_from_spec(spec);sys.modules[spec.name]=capacity;spec.loader.exec_module(capacity)
    network_path=Path(__file__).with_name('qualify_horton_network.py')
    spec=importlib.util.spec_from_file_location('_horton_network_helper',network_path)
    network=importlib.util.module_from_spec(spec);spec.loader.exec_module(network)
    from easysewer.runtime._solver_worker import _configure_error_mode
    from easysewer.io.hotstart import HotstartData,HotstartLayout
    from easysewer.io.inp import InpDocument
    from easysewer.model import Model
    _configure_error_mode()
    baseline=capacity.Trial(args.baseline,'minimum-cap');candidate=capacity.Trial(args.candidate,'modified-degenerate')
    if baseline.build['base']!=candidate.build['base'] or baseline.build['probe_sha256']!=candidate.build['probe_sha256']:
        raise ValueError('Source base or native diagnostic ABI differs')
    if [name for name,digest in baseline.build['source_digests'].items()
        if candidate.build['source_digests'].get(name)!=digest]!=['src/solver/infil.c']:
        raise ValueError('Unexpected candidate source changes')
    checks=[];cases=[];hotstarts=[];runs=0;attempts=0;active_call=None;execution_error=None
    expected_periods=math.floor(((43831.+1./24.)-43831.)*86400.)
    def check(name,passed,**context):
        checks.append(dict(name=name,passed=bool(passed),**context))
    def save(complete=False):
        record=dict(prepared,native_executed=(True if runs else (None if attempts else False)),execution_complete=complete,
            candidate_qualified=False,whole_project_gate_passed=complete and all(row['passed'] for row in checks),
            checks=checks,cases=cases,hotstarts=hotstarts,native_runs=runs,package=package,
            native_run_attempts=attempts,active_call=active_call,execution_error=execution_error,
            python=sys.version,platform=platform.platform(),baseline_sha256=sha(args.baseline),
            candidate_sha256=sha(args.candidate),adopted=False,worker_checkpoints_qualified=False,
            ordinary_horton_repaired=False,
            scope='Isolated whole-catchment diagnostic libraries, complete OUT, native state and HOTSTART; no pipe-network or release-accuracy gate.')
        (destination/'result.json').write_text(json.dumps(record,indent=2)+'\n',encoding='utf-8')
        return record
    def run_native(library,folder,source,**kwargs):
        nonlocal runs,attempts,active_call,execution_error
        attempts+=1;active_call=str(folder);save()
        try:
            value=network.run(library,folder,source,**kwargs)
        except Exception as error:
            execution_error=dict(type=type(error).__name__,message=str(error));save()
            raise
        runs+=1;active_call=None
        return value
    save()
    for row in prepared['manifest']['fixtures']:
        unit,case=row['units'],row['case'];context=dict(units=unit,case=case)
        source=(args.inputs/(unit+'-'+case+'.inp')).read_text(encoding='ascii')
        folder=destination/'models'/(unit+'-'+case)
        left=run_native(baseline,folder/'baseline',source)
        right=run_native(candidate,folder/'candidate',source)
        out_equal=left['artifact_sha256']['model.out']==right['artifact_sha256']['model.out']
        check('complete_output_relation',out_equal==(row['expected_output_relation']=='equal'),**context)
        check('native_state_relation',(left['trace']!=right['trace'])==row['expected_state_change'],**context)
        check('periods_and_rain',left['periods']==right['periods']==expected_periods
              and max(right['series']['rain'])>0,**context)
        check('finite_mass_balance_values',all(math.isfinite(x) for x in right['balance_percent']),**context)
        if row['expected_output_relation']=='equal':
            check('unchanged_balance',left['balance_percent']==right['balance_percent'],**context)
        if right['trace']:
            check('finite_nonnegative_state',all(all(math.isfinite(x) and x>=0 for x in state)
                  for state in right['trace']),**context)
        if case not in ('horton','ordinary-constant','ordinary-zero-decay') and right['trace']:
            check('modified_capacity_bound',all(state[7]<=state[5] for state in right['trace'] if state[5]>0),**context)
        if case in ('zero-decay','zero-decay-unlimited'):
            check('positive_excess_and_recovery',max(state[7] for state in right['trace'])>0
                  and any(b[7]<a[7] and 1500<=b[0]<=2300
                          for a,b in zip(right['trace'],right['trace'][1:])),**context)
        if case=='zero-decay-small-cap':
            check('finite_capacity_limits_infiltration',sum(right['series']['infiltration'])<sum(left['series']['infiltration'])
                  and max(right['series']['runoff'])>0
                  and any(state[7]==state[5] and state[5]>0 for state in right['trace']),**context)
        if case=='zero-rates':check('zero_rate_has_no_infiltration',max(right['series']['infiltration'])==0,**context)
        cases.append(dict(context,baseline=left,candidate=right,input_sha256=row['sha256']))
        save()
    for unit in ('CFS','CMS'):
        for case in ('zero-decay','zero-decay-small-cap','zero-decay-unlimited'):
            source=(args.inputs/(unit+'-'+case+'.inp')).read_text(encoding='ascii')
            layout=HotstartLayout.from_model(Model.from_document(InpDocument.from_text(source),strict=True))
            for label,producer in (('baseline',baseline),('candidate',candidate)):
                folder=destination/'hotstarts'/(unit+'-'+case+'-'+label);folder.mkdir(parents=True)
                path=folder/'state.hsf';rewritten=folder/'rewritten.hsf'
                produced=run_native(producer,folder/'producer',source,stop=720,save=path)
                raw=path.read_bytes();HotstartData.from_bytes(raw,layout=layout).write(rewritten)
                expected=tuple(produced['final'][6:8]);context=dict(units=unit,case=case,producer=label)
                check('hotstart_state_bytes',raw[:15]==b'SWMM5-HOTSTART4'
                      and struct.unpack_from('<2d',raw,39+32)==expected,**context)
                check('typed_state_preserves_all_bytes',raw==rewritten.read_bytes(),**context)
                loaded=run_native(candidate,folder/'load-original',source,use=path)
                rewritten_loaded=run_native(candidate,folder/'load-rewritten',source,use=rewritten)
                check('native_initial_state_exact',tuple(loaded['initial'][6:8])==expected,**context)
                check('rewritten_state_same_output',loaded['artifact_sha256']['model.out']==
                      rewritten_loaded['artifact_sha256']['model.out'],**context)
                hotstarts.append(dict(context,state=list(expected),state_sha256=sha(path),
                    consumer_out_sha256=loaded['artifact_sha256']['model.out']))
                save()
    assert runs==204
    return save(complete=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inputs',type=Path,required=True)
    parser.add_argument('--destination',type=Path,required=True)
    parser.add_argument('--preflight',action='store_true')
    for name in ('baseline','candidate','package','package-record'):parser.add_argument('--'+name,type=Path)
    args=parser.parse_args();args.inputs=args.inputs.resolve()
    if not args.preflight and any(getattr(args,name) is None for name in ('baseline','candidate','package','package_record')):
        parser.error('Actual qualification requires both libraries, installed package and package record')
    prepared=preflight(args.inputs)
    destination=args.destination.resolve();destination.mkdir(parents=True,exist_ok=False)
    (destination/'preflight.json').write_text(json.dumps(prepared,indent=2)+'\n',encoding='utf-8')
    if args.preflight:
        print(json.dumps(dict(inputs_verified=True,fixture_count=84,native_executed=False,candidate_qualified=False)))
    else:
        record=qualify(args,destination,prepared)
        print(json.dumps(dict(native_runs=record['native_runs'],whole_project_gate_passed=record['whole_project_gate_passed'],
                              failed=[row for row in record['checks'] if not row['passed']])))
        raise SystemExit(not record['whole_project_gate_passed'])
