"""Build an isolated pristine EPA solver with a read-only diagnostic ABI.

Run under Linux with a GCC-compatible compiler. This does not prepare, replace,
or qualify an easysewer release library. Source hashes must match the recorded
upstream tree before compilation.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source', type=Path, required=True)
    p.add_argument('--manifest', type=Path, required=True)
    p.add_argument('--compiler', required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--target', choices=('windows', 'linux'), required=True)
    a = p.parse_args()
    base = json.loads(a.manifest.read_text())
    raw_hashes = {}
    for name, digest in base['files'].items():
        raw = (a.source / name).read_bytes()
        raw_hashes[name] = hashlib.sha256(raw).hexdigest()
        if hashlib.sha256(raw.replace(b'\r\n', b'\n')).hexdigest() != digest:
            raise ValueError('Upstream source mismatch: ' + name)
    probe = Path(__file__).with_name('native_dry_start_probe.c').resolve()
    solver = a.source.resolve() / 'src/solver'
    a.output = a.output.resolve()
    a.output.parent.mkdir(parents=True, exist_ok=True)
    flags = ['-shared', '-O2', '-fno-fast-math', '-ffp-contract=off',
             '-fopenmp', '-Wall', '-Wextra', '-Werror=implicit-function-declaration']
    flags += (['-static', '-static-libgcc', '-Wl,--no-insert-timestamp',
               '-Wl,--image-base,0x180000000'] if a.target == 'windows' else ['-fPIC'])
    command = [a.compiler, *flags, '-I'+str(solver), '-I'+str(solver/'include'),
               *[str(a.source.resolve()/name) for name in sorted(base['files'])
                 if name.startswith('src/solver/') and name.endswith('.c')],
               str(probe), '-o', str(a.output), '-lm']
    done = subprocess.run(command, capture_output=True, text=True)
    a.output.with_suffix(a.output.suffix+'.log').write_text(done.stdout + done.stderr)
    done.check_returncode()
    record = dict(scope='Pristine EPA source plus read-only diagnostic ABI; not release library',
                  base=base, raw_source_sha256=raw_hashes,
                  normalization='CRLF to LF for upstream manifest comparison only',
                  probe_sha256=sha(probe), recipe_sha256=sha(Path(__file__)),
                  command=command, compiler=subprocess.check_output([a.compiler, '--version'], text=True),
                  sha256=sha(a.output))
    a.output.with_suffix(a.output.suffix+'.json').write_text(json.dumps(record, indent=2)+'\n')
    print(json.dumps(dict(output=str(a.output), sha256=record['sha256'])))


if __name__ == '__main__':
    main()
