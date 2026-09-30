"""Compile verified standard sources using an explicit GCC-compatible compiler."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
from checkpoint import validate_checkpoint
from path_io import validate_path_io
from horton import validate_horton
from horton_outfall import validate_horton_outfall


def build(source,compiler,output,target):
    source=Path(source).resolve();output=Path(output).resolve()
    record=json.loads((source/'prepared-source.json').read_text(encoding='utf-8'))
    if (record['patch']!='easysewer:standard:5.2.4:16' or record.get('rainfall_fixes')!=1 or
            record.get('runoff_fixes')!=1 or record.get('runoff_physics')!=1 or record.get('runoff_rain_clock')!=1 or record.get('rdii_io')!=1 or record.get('routing_io')!=1 or record.get('solver_output_io')!=1 or record.get('climate_io')!=1 or record.get('timeseries_io')!=1 or record.get('report_io')!=1 or record.get('lid_report_io')!=1):
        raise ValueError('Unexpected standard patch profile')
    validate_checkpoint(record.get('checkpoint'))
    validate_path_io(record.get('path_io'))
    validate_horton(record.get('horton_capacity'))
    validate_horton_outfall(record.get('horton_outfall'))
    for name,digest in record['files'].items():
        if hashlib.sha256((source/name).read_bytes()).hexdigest()!=digest:raise ValueError('Prepared source changed: '+name)
    output.parent.mkdir(parents=True,exist_ok=True)
    flags=['-shared','-O2','-fno-fast-math','-ffp-contract=off','-fopenmp','-Wall','-Wextra',
           '-Werror=implicit-function-declaration']
    # MinGW otherwise derives a preferred image base from the output path.
    # Keep relocation/ASLR enabled, but use a stable preferred base.
    flags+=['-static','-static-libgcc','-Wl,--no-insert-timestamp','-Wl,--image-base,0x180000000'] if target=='windows' else ['-fPIC']
    files=sorted(str(source/name) for name in record['files'] if name.startswith('src/solver/') and name.endswith('.c'))
    command=[str(compiler),*flags,'-I'+str(source/'src/solver/include'),*files,'-o',str(output),'-lm']
    environment=os.environ.copy();environment['SOURCE_DATE_EPOCH']=str(record['base']['commit_timestamp'])
    completed=subprocess.run(command,cwd=output.parent,env=environment,capture_output=True,text=True)
    output.with_suffix(output.suffix+'.log').write_text(completed.stdout+completed.stderr,encoding='utf-8')
    completed.check_returncode()
    evidence=dict(target=target,host=platform.platform(),source=record,command=command,
        compiler=subprocess.check_output([str(compiler),'--version'],text=True),
        recipe_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        sha256=hashlib.sha256(output.read_bytes()).hexdigest(),size=output.stat().st_size,
        standard_fixes=16,path_io=record['path_io'],horton_capacity=record['horton_capacity'],horton_outfall=record['horton_outfall'],checkpoint=record['checkpoint'],rainfall_fixes=1,runoff_fixes=1,runoff_physics=1,runoff_rain_clock=1,rdii_io=1,routing_io=1,solver_output_io=1,climate_io=1,timeseries_io=1,report_io=1,lid_report_io=1,policy='EPA SWMM 5.2.4 with checkpoint ABI2 and six event/clock/exfiltration/inlet/groundwater/snow corrections; RUNOFF dimension/state restoration 1 and consumer rain clock 1; checked HOTSTART/RAIN/RUNOFF/RDII/routing, solver OUT, climate, time-series, main RPT and detailed LID report I/O; climate calendar, evaporation events and independent sequential/interpolation cursors; 4095-byte checked paths, physical INP records, pool-owned names and exclusive scratch streams; Modified Horton excess-capacity MIN bound 1, degenerate-parameter state 1 and outfall route-to gate reader 1; no fast-math or FP contraction')
    output.with_suffix(output.suffix+'.json').write_text(json.dumps(evidence,indent=2)+'\n',encoding='utf-8')
    return evidence


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',required=True);parser.add_argument('--compiler',required=True)
    parser.add_argument('--output',required=True);parser.add_argument('--target',choices=('windows','linux'),required=True)
    args=parser.parse_args();record=build(args.source,args.compiler,args.output,args.target)
    print(json.dumps({key:record[key] for key in ('target','sha256','size','standard_fixes')},indent=2))
