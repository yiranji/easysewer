"""Prepare or build an isolated Modified Horton degenerate-parameter candidate.

Retains the previously qualified MIN state bound. Removes only the Modified
Horton constant-rate shortcut so the existing, division-free state equations
also run at zero decay or equal maximum/minimum rates. Ordinary Horton stays
unchanged. No production binary or numeric capability is installed here.
"""
import argparse
import difflib
import hashlib
import json
from pathlib import Path
import subprocess


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def prepare(source, destination):
    root = Path(__file__).resolve().parents[2]
    manifest = json.loads((root/'native/standard/source.json').read_bytes())
    source, destination = Path(source).resolve(), Path(destination).resolve()
    originals = {}
    for name, digest in manifest['files'].items():
        raw = (source/name).read_bytes().replace(b'\r\n', b'\n')
        if hashlib.sha256(raw).hexdigest() != digest:
            raise ValueError('Upstream source mismatch: '+name)
        originals[name] = raw
    name = 'src/solver/infil.c'
    original = originals[name].decode('utf-8')
    capacity = '        if ( infil->Fmax > 0.0 ) infil->Fe = MAX(infil->Fe, infil->Fmax);'
    if original.count(capacity) != 1:
        raise ValueError('Modified Horton capacity anchor changed')
    baseline = original.replace(capacity, capacity.replace('MAX(', 'MIN('))
    start = baseline.index('\ndouble modHorton_getInfil(')
    end = baseline.index('\nvoid grnampt_getParams(', start)
    old_function = baseline[start:end]
    shortcut = '''    if ( df == 0.0 || kd == 0.0 )
    {
        fp = f0;
        fa = irate + depth / tstep;
        if ( fp > fa ) fp = fa;
        return MAX(0.0, fp);
    }
'''
    if old_function.count(shortcut) != 1:
        raise ValueError('Modified Horton shortcut anchor changed')
    new_function = old_function.replace(shortcut, '').replace(
        '// --- special cases of no or constant infiltration',
        '// --- reject invalid infiltration parameters')
    updated = baseline[:start]+new_function+baseline[end:]
    # The ordinary Horton function and every other native file retain baseline bytes.
    assert updated[:start] == baseline[:start]
    assert updated[start+len(new_function):] == baseline[end:]
    originals[name] = updated.encode('utf-8')
    destination.mkdir(parents=True, exist_ok=False)
    copied = destination/'source'
    for filename, raw in originals.items():
        path = copied/filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    probe = Path(__file__).with_name('horton_probe.c')
    (destination/probe.name).write_bytes(probe.read_bytes())
    (destination/'from-minimum-cap.patch').write_text(''.join(difflib.unified_diff(
        baseline.splitlines(keepends=True), updated.splitlines(keepends=True),
        fromfile='minimum-cap/src/solver/infil.c', tofile='modified-degenerate/src/solver/infil.c')),
        encoding='utf-8', newline='\n')
    record = dict(mode='modified-degenerate', adopted=False, compiled=False, base=manifest,
        source_digests={filename:sha(copied/filename) for filename in originals},
        changed_native_files=[name], baseline='pristine plus previously qualified MIN(Fe,Fmax)',
        baseline_infil_sha256=hashlib.sha256(baseline.encode('utf-8')).hexdigest(),
        difference_from_minimum_cap='Only Modified Horton shortcut removal and its preceding comment.',
        diagnostic_translation_unit=probe.name, probe_sha256=sha(destination/probe.name),
        recipe_sha256=sha(Path(__file__)), diff_sha256=sha(destination/'from-minimum-cap.patch'),
        reference='https://nepis.epa.gov/Exe/ZyPURL.cgi?Dockey=P100NYRA.TXT',
        reference_section='4.3.3, printed page 103',
        remaining=['native state and whole-project qualification', 'ordinary Horton degenerate branches',
                   'formal standard/custom integration, numeric identity and checkpoint compatibility'])
    (destination/'prepared.json').write_text(json.dumps(record,indent=2)+'\n',encoding='utf-8')
    return record


def build(prepared, compiler, target):
    destination = Path(prepared).resolve()
    record = json.loads((destination/'prepared.json').read_bytes())
    assert record['mode'] == 'modified-degenerate' and not record['adopted'] and not record['compiled']
    assert record['recipe_sha256'] == sha(Path(__file__))
    assert sha(destination/record['diagnostic_translation_unit']) == record['probe_sha256']
    assert sha(destination/'from-minimum-cap.patch') == record['diff_sha256']
    copied = destination/'source'
    for name,digest in record['source_digests'].items():
        if sha(copied/name) != digest:
            raise ValueError('Prepared native source differs: '+name)
    solver = copied/'src/solver'
    output = destination/('horton.dll' if target == 'windows' else 'horton.so')
    for path in (output,destination/'build.json',destination/'build.log'):
        if path.exists():
            raise FileExistsError(path)
    flags = ['-shared','-O2','-fno-fast-math','-ffp-contract=off','-fopenmp',
             '-Wall','-Wextra','-Werror=implicit-function-declaration']
    flags += (['-static','-static-libgcc','-Wl,--no-insert-timestamp',
               '-Wl,--image-base,0x180000000'] if target == 'windows' else ['-fPIC'])
    sources = [str(copied/name) for name in sorted(record['base']['files'])
               if name.endswith('.c') and name != 'src/solver/infil.c']
    command = [compiler,*flags,'-I'+str(solver),'-I'+str(solver/'include'),*sources,
               str(destination/record['diagnostic_translation_unit']),'-o',str(output),'-lm']
    result = subprocess.run(command,capture_output=True,text=True)
    (destination/'build.log').write_text(result.stdout+result.stderr,encoding='utf-8')
    result.check_returncode()
    record.update(compiled=True,command=command,library=str(output),binary_sha256=sha(output),
        compiler=subprocess.check_output([compiler,'--version'],text=True),
        prepared_sha256=sha(destination/'prepared.json'))
    (destination/'build.json').write_text(json.dumps(record,indent=2)+'\n',encoding='utf-8')
    return record


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command',required=True)
    prep = commands.add_parser('prepare')
    prep.add_argument('--source',type=Path,required=True)
    prep.add_argument('--destination',type=Path,required=True)
    compile_parser = commands.add_parser('build')
    compile_parser.add_argument('--prepared',type=Path,required=True)
    compile_parser.add_argument('--compiler',required=True)
    compile_parser.add_argument('--target',choices=('windows','linux'),required=True)
    args = parser.parse_args()
    result = (prepare(args.source,args.destination) if args.command == 'prepare'
              else build(args.prepared,args.compiler,args.target))
    print(json.dumps({key:result[key] for key in ('mode','adopted','compiled','source_digests')}))
