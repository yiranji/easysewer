"""Build a source archive and install both wheel profiles into a fresh directory."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import zipfile

from audit_sdist_tests import audit
from audit_docs import audit as audit_docs, published_files
from build_pure import build


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--python', default=sys.executable)
    args = parser.parse_args()
    # The documentation and archive auditors also use assertions. Fail before
    # creating output rather than silently omitting release gates under -O.
    if sys.flags.optimize:
        parser.error('Release validation requires assertions; run without -O, -OO or PYTHONOPTIMIZE.')
    root = Path(__file__).resolve().parents[1]
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    source = output / 'source'
    source.mkdir()
    sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
    inputs = {}
    selected = []
    audit_docs(root)
    for directory in ('src', 'tests', 'native', 'tools', 'examples', 'stubs', 'assets'):
        selected.extend(p for p in (root / directory).rglob('*') if p.is_file()
                        and not any(v in ('__pycache__', '.pytest_cache') or v.endswith('.egg-info') for v in p.parts)
                        and p.suffix not in ('.pyc', '.pyo'))
    selected.extend(root / 'docs' / name for name in published_files(root))
    selected.extend(root / name for name in ('pyproject.toml', 'MANIFEST.in', 'packaging_profile.json', 'README.md', 'cases/utils.py'))
    selected.extend(p for p in root.glob('LICENSE*') if p.is_file())
    for directory in (root / 'cases').glob('*Quick*Start'):
        selected.extend(directory / name for name in ('main.ipynb', 'cubic.inp'))
    for path in selected:
        relative = path.relative_to(root)
        target = source / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        inputs[relative.as_posix()] = sha(path)

    def run(label, command, cwd):
        with (output / (label + '.log')).open('x', encoding='utf-8') as log:
            subprocess.run(command, cwd=cwd, stdout=log, stderr=subprocess.STDOUT, check=True)

    dist = output / 'native'
    dist.mkdir()
    run('sdist', [args.python, '-B', '-c', 'import sys;from setuptools.build_meta import build_sdist;build_sdist(sys.argv[1])', str(dist)], source)
    sdist, = dist.glob('*.tar.gz')
    docs_audit = audit_docs(source, sdist=sdist)
    (output / 'docs-audit.json').write_text(json.dumps(docs_audit, indent=2) + '\n', encoding='utf-8')
    test_audit = audit(source, sdist)
    assert test_audit['passed'], test_audit
    (output / 'test-audit.json').write_text(json.dumps(test_audit, indent=2) + '\n', encoding='utf-8')
    extracted = output / 'extracted'
    extracted.mkdir()
    with tarfile.open(sdist) as archive:
        archive.extractall(extracted, filter='data')
    rebuilt, = extracted.iterdir()
    run('wheel', [args.python, '-B', '-c', 'import sys;from setuptools.build_meta import build_wheel;build_wheel(sys.argv[1])', str(dist)], rebuilt)
    pure = build(rebuilt, output / 'pure', python=args.python)
    packages = {}
    for kind in ('native', 'pure'):
        wheel, = (output / kind).glob('*.whl')
        with zipfile.ZipFile(wheel) as archive:
            members = {name: hashlib.sha256(archive.read(name)).hexdigest()
                       for name in archive.namelist() if name.startswith('easysewer/') and not name.endswith('/')}
        if kind == 'native':
            assert members == {name[4:]: digest for name, digest in inputs.items() if name.startswith('src/easysewer/')}
        else:
            assert members == {name: record['sha256'] for name, record in pure['files'].items()}
        installed = output / ('installed-' + kind)
        run('install-' + kind, [args.python, '-m', 'pip', 'install', '--no-index', '--no-deps', '--no-compile',
                               '--disable-pip-version-check', '--target', str(installed), str(wheel)], output)
        for name, digest in members.items():
            assert sha(installed / name) == digest, name
        packages[kind] = dict(wheel=str(wheel), sha256=sha(wheel), installed=str(installed), members=members)
    for name, digest in inputs.items():
        assert sha(root / name) == digest, 'Source changed during build: ' + name
    record = dict(sdist=str(sdist), sdist_sha256=sha(sdist), test_files=test_audit['expected_files'],
                  source_digests=inputs, packages=packages)
    (output / 'build.json').write_text(json.dumps(record, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(dict(sdist=str(sdist), test_files=record['test_files'],
                         wheels={kind: value['wheel'] for kind, value in packages.items()})))


if __name__ == '__main__':
    main()
