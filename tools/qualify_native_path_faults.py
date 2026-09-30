"""Compile Linux path allocation/read fault checks against a prepared tree.

Requires GCC with ASan/UBSan and glibc fopencookie. Outputs retain exact commands,
source/test digests and logs. This checks fault handling, not public Runner use.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import subprocess


SOURCE = '''[OPTIONS]
FLOW_UNITS CFS
START_DATE 01/01/2020
END_DATE 01/01/2020
END_TIME 0:10
REPORT_STEP 0:01
WET_STEP 0:01
ROUTING_STEP 1
[JUNCTIONS]
J 2 10
[OUTFALLS]
O 0 FREE
[CONDUITS]
C J O 100 .013 0 0
[XSECTIONS]
C CIRCULAR 2 0 0 0 1
'''


def qualify(source, output, compiler):
    source, output = Path(source).resolve(), Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    repo = Path(__file__).resolve().parent.parent
    tests = repo/'tests'
    manifest_path = source/'path-source.json'
    if not manifest_path.is_file():
        manifest_path = source/'prepared-source.json'
    manifest = json.loads(manifest_path.read_bytes())
    for name, digest in manifest['files'].items():
        path = source/name
        if not path.resolve().is_relative_to(source):
            raise ValueError('Source path escapes tree')
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError('Prepared source changed: ' + name)
    flags = ['-O1', '-g', '-fno-fast-math', '-ffp-contract=off', '-fopenmp',
             '-fno-omit-frame-pointer', '-fsanitize=address,undefined', '-no-pie',
             '-I'+str(source/'src/solver'), '-I'+str(source/'src/solver/include')]
    files = sorted(str(source/name) for name in manifest['files']
                   if name.startswith('src/solver/') and name.endswith('.c'))
    inp = output/'model.inp'
    resources = output/('long-input-'+('x'*120))
    resources.mkdir()
    rain = resources/'rain.dat'
    rain.write_text('Station 2020 1 1 0 0 .1\nStation 2020 1 1 0 5 .2\n', encoding='utf-8')
    series = [resources/('series-'+letter+'.dat') for letter in ('a', 'b')]
    for path in series:
        path.write_text('0 .1\n0:05 .3\n0:10 .2\n', encoding='utf-8')
    inp.write_text(SOURCE+f'[RAINGAGES]\nR INTENSITY 0:01 1 FILE "{rain}" Station IN\n'
                   f'[TIMESERIES]\nA FILE "{series[0]}"\nB FILE "{series[1]}"\n'
                   '[INFLOWS]\nJ FLOW A FLOW 1 1\n', encoding='utf-8')
    close_inp = output/'close.inp'
    close_inp.write_text(SOURCE+f'[TIMESERIES]\nA FILE "{series[0]}"\n'
                        '[TEMPERATURE]\nTIMESERIES A\n', encoding='utf-8')
    environment = dict(os.environ, ASAN_OPTIONS='detect_leaks=1:halt_on_error=1',
                       UBSAN_OPTIONS='halt_on_error=1:print_stacktrace=1')
    # Parse and close harnesses share the same verified sanitized solver.
    # Compile it once, then link different wrappers without production hooks.
    object_dir = output/'objects'
    object_dir.mkdir()
    def compile_object(name):
        obj = object_dir/(Path(name).stem+'.o')
        command = [compiler, *flags, '-c', name, '-o', str(obj)]
        result = subprocess.run(command, capture_output=True)
        obj.with_suffix('.log').write_bytes(result.stdout+result.stderr)
        result.check_returncode()
        return dict(path=str(obj), command=command,
                    sha256=hashlib.sha256(obj.read_bytes()).hexdigest())
    with ThreadPoolExecutor(max_workers=2) as pool:
        objects = list(pool.map(compile_object, files))
    evidence = []
    for kind in ('pool', 'parse', 'close'):
        harness = tests/('native_path_'+kind+'_faults.c')
        exe = output/kind
        command = [compiler, *flags, str(harness)]
        if kind == 'parse':
            command += [*(obj['path'] for obj in objects), '-Wl,--wrap=fopen',
                        '-Wl,--wrap=Alloc', '-Wl,--wrap=malloc', '-lm']
        if kind == 'close':
            command += [*(obj['path'] for obj in objects), '-Wl,--wrap=fopen',
                        '-Wl,--wrap=fclose', '-lm']
        command += ['-o', str(exe)]
        result = subprocess.run(command, capture_output=True)
        (output/(kind+'-build.log')).write_bytes(result.stdout+result.stderr)
        result.check_returncode()
        run = [str(exe)]
        if kind == 'parse':
            run += [str(inp), str(output/'model.rpt'), str(output/'model.out')]
        runs = [run] if kind != 'close' else [run+[str(close_inp), str(output/'close.rpt'),
                str(output/'close.out'), str(series[0]), str(index)] for index in range(1, 4)]
        for index, run in enumerate(runs):
            result = subprocess.run(run, env=environment, capture_output=True)
            label = kind+('-'+str(index+1) if kind == 'close' else '')
            (output/(label+'-stdout.log')).write_bytes(result.stdout)
            (output/(label+'-stderr.log')).write_bytes(result.stderr)
            evidence.append(dict(kind=label, command=command, run=run, returncode=result.returncode,
                             harness_sha256=hashlib.sha256(harness.read_bytes()).hexdigest(),
                             exe_sha256=hashlib.sha256(exe.read_bytes()).hexdigest(),
                             shared_read_harness_sha256=(hashlib.sha256(
                                 (tests/'native_path_read_faults.c').read_bytes()).hexdigest()
                                 if kind == 'parse' else None)))
            print(label, result.returncode, flush=True)
    record = dict(manifest_sha256=hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
                  recipe_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                  compiler=subprocess.check_output([compiler, '--version'], text=True),
                  sanitizer_environment={key: environment[key] for key in ('ASAN_OPTIONS', 'UBSAN_OPTIONS')},
                  objects=objects, checks=evidence,
                  passed=all(row['returncode'] == 0 for row in evidence))
    (output/'result.json').write_text(json.dumps(record, indent=2)+'\n', encoding='utf-8')
    if not record['passed']:
        raise RuntimeError('Fault qualification failed; inspect retained logs')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--compiler', default='gcc')
    args = parser.parse_args()
    qualify(args.source, args.output, args.compiler)
