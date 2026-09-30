"""Build pristine and one-line Modified Horton capacity candidates with diagnostics.

The experiment replaces MAX(Fe,Fmax) with MIN(Fe,Fmax), following EPA Hydrology
Reference Manual section 4.3.3 step 5. It does not replace an installed solver or
change the native infiltration equations. Formal integration is a separate gate.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess


def build(source,destination,compiler,target,mode):
    root=Path(__file__).resolve().parents[2]
    manifest=json.loads((root/'native/standard/source.json').read_text(encoding='utf-8'))
    destination=Path(destination).resolve();destination.mkdir(parents=True,exist_ok=False)
    source=Path(source).resolve();copied=destination/'source'
    sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
    for name,digest in manifest['files'].items():
        raw=(source/name).read_bytes().replace(b'\r\n',b'\n')
        if hashlib.sha256(raw).hexdigest()!=digest:raise ValueError('Upstream source mismatch: '+name)
        path=copied/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(raw)
    path=copied/'src/solver/infil.c';text=path.read_text(encoding='utf-8')
    anchor='        if ( infil->Fmax > 0.0 ) infil->Fe = MAX(infil->Fe, infil->Fmax);'
    if text.count(anchor)!=1:raise ValueError('Modified Horton capacity anchor changed')
    if mode=='minimum-cap':path.write_text(text.replace(anchor,anchor.replace('MAX(', 'MIN(')),encoding='utf-8',newline='\n')
    elif mode!='pristine':raise ValueError('Unknown experiment mode')
    probe=Path(__file__).with_name('horton_probe.c').resolve();solver=copied/'src/solver'
    output=destination/('horton.dll' if target=='windows' else 'horton.so')
    flags=['-shared','-O2','-fno-fast-math','-ffp-contract=off','-fopenmp','-Wall','-Wextra','-Werror=implicit-function-declaration']
    flags+=['-static','-static-libgcc','-Wl,--no-insert-timestamp','-Wl,--image-base,0x180000000'] if target=='windows' else ['-fPIC']
    sources=[str(copied/name) for name in sorted(manifest['files']) if name.endswith('.c') and name!='src/solver/infil.c']
    command=[compiler,*flags,'-I'+str(solver),'-I'+str(solver/'include'),*sources,str(probe),'-o',str(output),'-lm']
    result=subprocess.run(command,capture_output=True,text=True)
    (destination/'build.log').write_text(result.stdout+result.stderr,encoding='utf-8');result.check_returncode()
    record=dict(adopted=False,mode=mode,base=manifest,command=command,library=str(output),binary_sha256=sha(output),
        source_digests={name:sha(copied/name) for name in manifest['files']},
        changed_native_files=['src/solver/infil.c'] if mode=='minimum-cap' else [],
        diagnostic_translation_unit=probe.name,probe_sha256=sha(probe),recipe_sha256=sha(Path(__file__)),
        compiler=subprocess.check_output([compiler,'--version'],text=True),
        reference='https://nepis.epa.gov/Exe/ZyPURL.cgi?Dockey=P100NYRA.TXT',reference_section='4.3.3, page 103, step 5')
    (destination/'build.json').write_text(json.dumps(record,indent=2)+'\n',encoding='utf-8')
    return record


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source',type=Path,required=True);p.add_argument('--destination',type=Path,required=True)
    p.add_argument('--compiler',required=True);p.add_argument('--target',choices=('windows','linux'),required=True)
    p.add_argument('--mode',choices=('pristine','minimum-cap'),required=True);a=p.parse_args()
    result=build(a.source,a.destination,a.compiler,a.target,a.mode)
    print(json.dumps({key:result[key] for key in ('mode','library','binary_sha256','adopted')}))
