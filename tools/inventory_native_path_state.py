"""Enumerate candidate static storage and compare a prior explicit owner audit.

An unchanged symbol set does not prove checkpoint equivalence. Heap fields and
changed owner behavior require a separate review and execution qualification.
Run on Linux with GCC and MinGW; all commands and object digests are retained.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import re
import subprocess


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def inventory(standard, custom, baseline, output, cross_bin):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    baseline = Path(baseline).resolve()
    prior = json.loads(baseline.read_bytes())
    known = {(r['family'], r['file'], r['symbol']): r for r in prior['rows']}
    jobs = []
    sources = {}
    manifests = {}
    for family, source in (('standard', standard), ('custom', custom)):
        source = Path(source).resolve()
        sources[family] = source
        manifests[family] = source/'path-source.json'
        if not manifests[family].is_file():
            manifests[family] = source/'prepared-source.json'
        manifest = json.loads(manifests[family].read_bytes())
        for name, expected in manifest['files'].items():
            path = source/name
            if not path.resolve().is_relative_to(source) or digest(path) != expected:
                raise ValueError('Source changed or escaped its tree: '+name)
            if name.startswith('src/solver/') and name.endswith('.c'):
                for target in ('linux', 'windows'):
                    jobs.append((family, source, name, expected, target))

    def inspect(job):
        family, source, name, expected, target = job
        prefix = str(Path(cross_bin)/'x86_64-w64-mingw32-')
        compiler = prefix+'gcc-posix' if target == 'windows' else 'gcc'
        nm = prefix+'nm' if target == 'windows' else 'nm'
        obj = output/family/target/(Path(name).stem+'.o')
        obj.parent.mkdir(parents=True, exist_ok=True)
        # MinGW normally strips L-prefixed local symbols. Keep all locals.
        command = [compiler, '-c', '-O0', '-g', '-Wa,-L', '-fno-fast-math',
                   '-ffp-contract=off', '-fopenmp', '-I'+str(source/'src/solver/include'),
                   str(source/name), '-o', str(obj)]
        process = subprocess.run(command, capture_output=True)
        obj.with_suffix('.build.log').write_bytes(process.stdout+process.stderr)
        process.check_returncode()
        raw = subprocess.check_output([nm, '-S', '--defined-only', '--format=posix', str(obj)])
        obj.with_suffix('.nm').write_bytes(raw)
        symbols = []
        for line in raw.decode().splitlines():
            values = line.split()
            if len(values) >= 3 and values[1] in 'bBdDgGsSC' and not values[0].startswith('.'):
                symbols.append(dict(name=re.sub(r'\.\d+$', '', values[0]),
                                    native_name=values[0], kind=values[1],
                                    size=int(values[3], 16) if len(values) > 3 else None))
        return dict(family=family, file=name, target=target, source_sha256=expected,
                    object_sha256=digest(obj), command=command, symbols=symbols)

    with ThreadPoolExecutor(max_workers=2) as pool:
        records = list(pool.map(inspect, jobs))
    platform_sets = {}
    for target in ('linux', 'windows'):
        platform_sets[target] = {(r['family'], r['file'], s['name'])
                                 for r in records if r['target'] == target for s in r['symbols']}
    current = platform_sets['linux']
    differences = dict(added=sorted(current-known.keys()), removed=sorted(known.keys()-current),
                       windows_only=sorted(platform_sets['windows']-current),
                       linux_only=sorted(current-platform_sets['windows']))
    rows = []
    for key in sorted(current):
        family, name, symbol = key
        row = dict(family=family, file=name, symbol=symbol)
        previous = known.get(key)
        if previous:
            row['prior_decision'] = previous['decision']
            row['prior_reason'] = previous['reason']
            row['checkpoint_owners'] = previous['checkpoint_owners']
        row['current_references'] = []
        for file in ([name, 'src/solver/globals.h'] if Path(name).stem == 'swmm5' else [name]):
            lines = (sources[family]/file).read_text().splitlines()
            hits = [index for index, line in enumerate(lines, 1)
                    if re.search(r'\b'+re.escape(symbol)+r'\b', line)]
            if hits:
                row['current_references'].append(dict(file=file, lines=hits))
        rows.append(row)
    record = dict(scope='Static storage comparison only; prior decisions are evidence links, not renewed R02 acceptance.',
                  baseline=dict(path=str(baseline), sha256=digest(baseline)),
                  source_manifests={f:digest(path) for f,path in manifests.items()},
                  script_sha256=digest(Path(__file__)), records=records, rows=rows,
                  counts={f:sum(k[0] == f for k in current) for f in sources},
                  differences=differences, same_symbol_sets=not any(differences.values()))
    (output/'result.json').write_text(json.dumps(record, indent=2)+'\n')
    print(json.dumps({k: record[k] for k in ('counts', 'differences', 'same_symbol_sets')}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('standard', 'custom', 'baseline', 'output', 'cross-bin'):
        parser.add_argument('--'+name, required=True)
    args = parser.parse_args()
    inventory(args.standard, args.custom, args.baseline, args.output, args.cross_bin)
