"""Build an independently identified candidate without installing it."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import runpy


def build(source, compiler, output, target):
    source, output = Path(source).resolve(), Path(output).resolve()
    root = Path(__file__).resolve().parent
    record = json.loads((source/'path-source.json').read_bytes())
    if record.get('kind') != 'easysewer:path-io-candidate' or record.get('version') != 2:
        raise ValueError('Unexpected candidate profile')
    recipe = runpy.run_path(str(root/'patch.py'))
    expected = {name: hashlib.sha256((root/name).read_bytes()).hexdigest()
                for name in ('prepare.py', *recipe['RECIPE_FILES'])}
    if record.get('recipes') != expected:
        raise ValueError('Incomplete or stale candidate recipes')
    if set(record['files']) != set(record['base']['files']):
        raise ValueError('Candidate source file set differs from its base')
    for name, digest in record['recipes'].items():
        path = root/name
        if not path.resolve().is_relative_to(root):
            raise ValueError('Recipe path escapes tree')
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError('Candidate recipe changed: ' + name)
    for name, digest in record['files'].items():
        path = source/name
        if not path.resolve().is_relative_to(source):
            raise ValueError('Source path escapes tree')
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError('Candidate source changed: ' + name)
    output.parent.mkdir(parents=True, exist_ok=True)
    flags = ['-shared', '-O2', '-fno-fast-math', '-ffp-contract=off', '-fopenmp',
             '-Wall', '-Wextra', '-Werror=implicit-function-declaration']
    flags += (['-static', '-static-libgcc', '-Wl,--no-insert-timestamp',
               '-Wl,--image-base,0x180000000'] if target == 'windows' else ['-fPIC'])
    files = sorted(str(source/name) for name in record['files']
                   if name.startswith('src/solver/') and name.endswith('.c'))
    command = [compiler, *flags, '-I'+str(source/'src/solver/include'), *files, '-o', str(output), '-lm']
    environment = dict(os.environ, SOURCE_DATE_EPOCH='1696942175')
    result = subprocess.run(command, cwd=output.parent, env=environment, capture_output=True)
    output.with_suffix(output.suffix+'.log').write_bytes(result.stdout+result.stderr)
    result.check_returncode()
    evidence = dict(target=target, source=record, command=command, qualified=False,
                    compiler=subprocess.check_output([compiler, '--version'], text=True),
                    recipe_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                    sha256=hashlib.sha256(output.read_bytes()).hexdigest())
    output.with_suffix(output.suffix+'.json').write_text(json.dumps(evidence, indent=2)+'\n', encoding='utf-8')
    return evidence


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True)
    parser.add_argument('--compiler', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--target', choices=('windows', 'linux'), required=True)
    args = parser.parse_args()
    record = build(args.source, args.compiler, args.output, args.target)
    print(json.dumps({key: record[key] for key in ('target', 'sha256', 'qualified')}))
