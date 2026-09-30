"""Build a verified prepared source with an explicit GCC-compatible compiler.

All object/output files stay beside --output. Compiler installation and system
configuration are outside this script. Windows builds use a MinGW cross compiler.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys

sys.path.insert(0,str(Path(__file__).resolve().parent.parent/'standard'))
from checkpoint import validate_checkpoint
from path_io import validate_path_io
from horton import validate_horton
from horton_outfall import validate_horton_outfall


def build(source, compiler, output, target):
    source=Path(source).resolve();output=Path(output).resolve()
    evidence=json.loads((source/'prepared-source.json').read_text(encoding='utf-8'))
    if (evidence['patch'] != 'easysewer:flexible-ponding:abi:201' or
            evidence.get('native_io_fixes') != 14 or evidence.get('rainfall_fixes') != 1 or
            evidence.get('runoff_fixes') != 1 or evidence.get('runoff_physics') != 1 or evidence.get('runoff_rain_clock') != 1 or evidence.get('rdii_io') != 1 or evidence.get('routing_io') != 1 or evidence.get('solver_output_io') != 1 or evidence.get('climate_io') != 1 or evidence.get('timeseries_io') != 1 or evidence.get('report_io') != 1 or evidence.get('lid_report_io') != 1):
        raise ValueError('Unexpected custom ABI or native I/O patch profile')
    validate_checkpoint(evidence.get('checkpoint'))
    validate_path_io(evidence.get('path_io'))
    validate_horton(evidence.get('horton_capacity'))
    validate_horton_outfall(evidence.get('horton_outfall'))
    for name,digest in evidence['files'].items():
        if hashlib.sha256((source/name).read_bytes()).hexdigest()!=digest:
            raise ValueError('Prepared source changed: '+name)
    output.parent.mkdir(parents=True,exist_ok=True)
    flags=['-shared','-O2','-fno-fast-math','-ffp-contract=off','-fopenmp',
           '-Werror=implicit-function-declaration']
    flags+=['-static','-static-libgcc','-Wl,--no-insert-timestamp',
            '-Wl,--image-base,0x180000000'] if target=='windows' else ['-fPIC']
    sources=sorted(str(source/name) for name in evidence['files'] if name.endswith('.c'))
    command=[str(compiler),*flags,'-I'+str(source/'src/solver/include'),*sources,'-o',str(output),'-lm']
    environment=os.environ.copy()
    environment['SOURCE_DATE_EPOCH']='1753142400'
    result=subprocess.run(command,cwd=output.parent,env=environment,capture_output=True,text=True)
    output.with_suffix(output.suffix+'.log').write_text(result.stdout+result.stderr,encoding='utf-8')
    result.check_returncode()
    record=dict(target=target,host=platform.platform(),compiler=subprocess.check_output([str(compiler),'--version'],text=True),
        command=command,source=evidence,sha256=hashlib.sha256(output.read_bytes()).hexdigest(),
        size=output.stat().st_size,abi=201,native_io_fixes=14,path_io=evidence['path_io'],horton_capacity=evidence['horton_capacity'],horton_outfall=evidence['horton_outfall'],checkpoint=evidence['checkpoint'],rainfall_fixes=1,runoff_fixes=1,runoff_physics=1,runoff_rain_clock=1,rdii_io=1,routing_io=1,solver_output_io=1,climate_io=1,timeseries_io=1,report_io=1,lid_report_io=1,
        recipe_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        policy='Discrete external removal; ordinary SWMM integration separate; post-adjustment statistics; checkpoint ABI2 and six event/clock/exfiltration/inlet/groundwater/snow corrections; RUNOFF dimension/state restoration 1 and consumer rain clock 1; checked HOTSTART/RAIN/RUNOFF/RDII/routing, solver OUT, climate, time-series, main RPT and detailed LID report I/O; climate calendar, evaporation events and independent sequential/interpolation cursors; 4095-byte checked paths, physical INP records, pool-owned names and exclusive scratch streams; Modified Horton excess-capacity MIN bound 1, degenerate-parameter state 1 and outfall route-to gate reader 1')
    output.with_suffix(output.suffix+'.json').write_text(json.dumps(record,indent=2)+'\n',encoding='utf-8')
    return record


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',required=True);parser.add_argument('--compiler',required=True)
    parser.add_argument('--output',required=True);parser.add_argument('--target',choices=('windows','linux'),required=True)
    args=parser.parse_args();record=build(args.source,args.compiler,args.output,args.target)
    print(json.dumps({key:record[key] for key in ('target','sha256','size','abi')},indent=2))
