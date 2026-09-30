"""Build only the verified SMO reader, independently of either SWMM solver."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess


def build(source, compiler, output, target):
    source = Path(source).resolve()
    output = Path(output).resolve()
    record = json.loads((source / 'prepared-source.json').read_text(encoding='utf-8'))
    if record['patch'] != 'easysewer:output-io:1' or record.get('output_io') != 1:
        raise ValueError('Unexpected output reader profile')
    for name, digest in record['files'].items():
        if hashlib.sha256((source / name).read_bytes()).hexdigest() != digest:
            raise ValueError('Prepared source changed: ' + name)
    output.parent.mkdir(parents=True, exist_ok=True)
    flags = ['-std=c99', '-shared', '-O2', '-fno-fast-math', '-ffp-contract=off',
             '-Wall', '-Wextra', '-Werror', '-fvisibility=hidden']
    flags += (['-static', '-static-libgcc', '-Wl,--no-insert-timestamp',
               '-Wl,--image-base,0x180000000'] if target == 'windows' else ['-fPIC'])
    command = [str(compiler), *flags, '-I' + str(source / 'src/outfile/include'),
               str(source / 'src/outfile/swmm_output.c'), '-o', str(output), '-lm']
    environment = os.environ.copy()
    environment['SOURCE_DATE_EPOCH'] = str(record['base']['commit_timestamp'])
    completed = subprocess.run(command, cwd=output.parent, env=environment, capture_output=True, text=True)
    output.with_suffix(output.suffix + '.log').write_text(completed.stdout + completed.stderr, encoding='utf-8')
    completed.check_returncode()
    evidence = dict(target=target, host=platform.platform(), source=record, command=command,
                    compiler=subprocess.check_output([str(compiler), '--version'], text=True),
                    recipe_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                    sha256=hashlib.sha256(output.read_bytes()).hexdigest(), size=output.stat().st_size,
                    output_io=1, policy='Bounded SWMM 5.2.4 OUT reader; retained SMO ABI; checked ownership and I/O')
    output.with_suffix(output.suffix + '.json').write_text(json.dumps(evidence, indent=2) + '\n', encoding='utf-8')
    return evidence


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True)
    parser.add_argument('--compiler', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--target', choices=('windows', 'linux'), required=True)
    args = parser.parse_args()
    result = build(args.source, args.compiler, args.output, args.target)
    print(json.dumps({key: result[key] for key in ('target', 'sha256', 'size', 'output_io')}, indent=2))
