"""Build verified accelerated sources with the shared strict GCC/Clang policy."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import runpy
import subprocess
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent
STANDARD = ROOT.parent/'standard'
RECIPE = runpy.run_path(str(ROOT/'patch.py'))
sys.path.insert(0, str(STANDARD))
STANDARD_BUILD = runpy.run_path(str(STANDARD/'build.py'))
configuration = STANDARD_BUILD['configuration']
CORRECTIONS = ('rainfall_fixes', 'runoff_fixes', 'runoff_physics', 'runoff_rain_clock',
               'rdii_io', 'routing_io', 'solver_output_io', 'climate_io',
               'timeseries_io', 'report_io', 'lid_report_io')


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate(source):
    """Check both source receipts, inherited corrections, and every prepared byte."""
    source = Path(source).resolve()
    record = json.loads((source/'prepared-source.json').read_text(encoding='utf-8'))
    standard = record.get('standard_source', {})
    if record.get('patch') != RECIPE['PATCH'] or standard.get('patch') != RECIPE['BASE_PATCH']:
        raise ValueError('Unexpected accelerated or base Standard patch profile')
    RECIPE['validate_acceleration'](record.get('acceleration'))
    expected_recipe = {name: _sha(ROOT/name)
                       for name in ('prepare.py', *RECIPE['RECIPE_FILES'])}
    if record.get('recipe') != expected_recipe:
        raise ValueError('Prepared accelerated recipe differs')
    manifest = json.loads((STANDARD/'source.json').read_text(encoding='utf-8'))
    if record.get('base') != manifest or standard.get('base') != manifest:
        raise ValueError('Prepared EPA source identity differs')
    for name, digest in standard['recipe'].items():
        if _sha(STANDARD/name) != digest:
            raise ValueError('Prepared Standard recipe differs: '+name)
    for name in CORRECTIONS:
        if record.get(name) != 1 or standard.get(name) != 1:
            raise ValueError('Prepared inherited correction differs: '+name)
    for key, validator in (('checkpoint', 'validate_checkpoint'),
                           ('path_io', 'validate_path_io'),
                           ('horton_capacity', 'validate_horton'),
                           ('horton_outfall', 'validate_horton_outfall')):
        if record.get(key) != standard.get(key):
            raise ValueError('Prepared inherited profile differs: '+key)
        STANDARD_BUILD[validator](record.get(key))
    if set(record['files']) != set(standard['files']):
        raise ValueError('Accelerated preparation changed the source file set')
    changed = {'src/solver/dynwave.c', 'src/solver/swmm5.c'}
    for name, digest in record['files'].items():
        path = (source/name).resolve()
        if not path.is_relative_to(source) or _sha(path) != digest:
            raise ValueError('Prepared source changed: '+name)
        if name not in changed and digest != standard['files'][name]:
            raise ValueError('Unexpected change outside acceleration scope: '+name)
    if record['files']['src/solver/dynwave.c'] != RECIPE['DYNWAVE_SHA256']:
        raise ValueError('Prepared acceleration source is not the pinned revision')
    solver = (source/'src/solver/swmm5.c').read_bytes()
    marker = RECIPE['MARKER']
    if not solver.endswith(marker) or hashlib.sha256(solver[:-len(marker)]).hexdigest() != standard['files']['src/solver/swmm5.c']:
        raise ValueError('Accelerated identity export changed the Standard solver API')
    return record


def build(source, compiler, output, target, *, architecture=None,
          deployment_target=None, openmp_prefix=None):
    source = Path(source).resolve()
    output = Path(output).resolve()
    record = validate(source)
    output.parent.mkdir(parents=True, exist_ok=True)
    flags, link_flags, toolchain = configuration(
        target, architecture=architecture, deployment_target=deployment_target,
        openmp_prefix=openmp_prefix, openmp=True, output_name=output.name)
    flags += ['-Wall', '-Wextra', '-Werror=implicit-function-declaration']
    files = sorted(str(source/name) for name in record['files']
                   if name.startswith('src/solver/') and name.endswith('.c'))
    command = [str(compiler), *flags, '-I'+str(source/'src/solver/include'),
               *files, '-o', str(output), *link_flags]
    environment = os.environ.copy()
    environment['SOURCE_DATE_EPOCH'] = str(record['base']['commit_timestamp'])
    temporary = output.parent/'compiler-temp'
    temporary.mkdir(exist_ok=True)
    for key in ('TMPDIR', 'TMP', 'TEMP'):
        environment[key] = str(temporary)
    # Thread counts and process affinity are runtime/model decisions. Do not
    # bake the four-core qualification environment into the product recipe.
    completed = subprocess.run(command, cwd=output.parent, env=environment,
                               capture_output=True, text=True)
    output.with_suffix(output.suffix+'.log').write_text(
        completed.stdout+completed.stderr, encoding='utf-8')
    completed.check_returncode()
    evidence = dict(target=target, **toolchain, host=platform.platform(), source=record,
                    command=command,
                    compiler=subprocess.check_output([str(compiler), '--version'], text=True),
                    recipe_sha256=_sha(Path(__file__)), sha256=_sha(output),
                    size=output.stat().st_size, standard_fixes=16,
                    acceleration_version=RECIPE['VERSION'],
                    acceleration=record['acceleration'],
                    checkpoint=record['checkpoint'], path_io=record['path_io'],
                    horton_capacity=record['horton_capacity'],
                    horton_outfall=record['horton_outfall'],
                    **{name: record[name] for name in CORRECTIONS},
                    policy='Standard revision 16 with ordered hydraulic topology caches, '
                           'node-owned double accumulation, and pure-conduit gather/depth '
                           'fusion; original arithmetic order and optional-cache fallback; '
                           'model-driven effective threads; no fast-math or FP contraction')
    output.with_suffix(output.suffix+'.json').write_text(
        json.dumps(evidence, indent=2)+'\n', encoding='utf-8')
    return evidence


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True)
    parser.add_argument('--compiler', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--target', choices=('windows', 'linux', 'darwin'), required=True)
    parser.add_argument('--architecture')
    parser.add_argument('--deployment-target')
    parser.add_argument('--openmp-prefix')
    args = parser.parse_args()
    record = build(args.source, args.compiler, args.output, args.target,
                   architecture=args.architecture, deployment_target=args.deployment_target,
                   openmp_prefix=args.openmp_prefix)
    print(json.dumps({key: record[key] for key in
                      ('target', 'sha256', 'size', 'standard_fixes', 'acceleration_version')},
                     indent=2))
