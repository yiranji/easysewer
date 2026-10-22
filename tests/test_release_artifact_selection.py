"""Publication must select exactly the qualified platform artifacts."""
import base64
import csv
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('select_release', ROOT / 'tools/select_release_artifacts.py')
SELECT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SELECT)


def wheel(path, target, *, external=False, tag=None, corrupt=False):
    members = {}
    libraries = {}
    for name in ('swmm5', 'swmm-output'):
        native = b'native bytes:' + name.encode()
        build = b'{"test": true}'
        libraries[name] = dict(path='libs/test/' + name, sha256=hashlib.sha256(native).hexdigest(),
            size=len(native), build_record='libs/test/' + name + '.json',
            build_record_sha256=hashlib.sha256(build).hexdigest(), dependencies=[],
            external_dependencies=['libgomp.so.1'] if external else [])
        members['easysewer/libs/test/' + name] = native + (b'changed' if corrupt else b'')
        members['easysewer/libs/test/' + name + '.json'] = build
    members['easysewer/libs/runtime-manifest.json'] = json.dumps(dict(
        schema='easysewer:native-runtime:1', targets={target: dict(libraries=libraries)})).encode()
    members['easysewer-2.0.1.dist-info/METADATA'] = b'Name: easysewer\nVersion: 2.0.1\n'
    members['easysewer-2.0.1.dist-info/WHEEL'] = (
        'Wheel-Version: 1.0\nRoot-Is-Purelib: false\nTag: py3-none-' + (tag or SELECT.TAGS[target]) + '\n').encode()
    record = io.StringIO()
    writer = csv.writer(record, lineterminator='\n')
    for name, raw in members.items():
        writer.writerow((name, 'sha256=' + base64.urlsafe_b64encode(hashlib.sha256(raw).digest()).rstrip(b'=').decode(), len(raw)))
    writer.writerow(('easysewer-2.0.1.dist-info/RECORD', '', ''))
    members['easysewer-2.0.1.dist-info/RECORD'] = record.getvalue().encode()
    with zipfile.ZipFile(path, 'w') as archive:
        for name, raw in members.items():
            archive.writestr(name, raw)


class ReleaseArtifactSelectionTests(unittest.TestCase):
    def test_requires_closed_dependencies_and_final_hashes(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'easysewer-2.0.1-py3-none-manylinux_2_33_x86_64.whl'
            for options in ({'external': True}, {'corrupt': True}, {'tag': 'any'}):
                with self.subTest(options=options):
                    wheel(path, 'linux-x86_64', **options)
                    with self.assertRaises(ValueError):
                        SELECT.verify_wheel(path, '2.0.1', 'linux-x86_64')
            wheel(path, 'linux-x86_64')
            SELECT.verify_wheel(path, '2.0.1', 'linux-x86_64')

    def test_rejects_generic_linux_and_higher_or_unproven_lower_glibc_tags(self):
        with tempfile.TemporaryDirectory() as temporary:
            for tag in ('linux_x86_64', 'manylinux_2_34_x86_64', 'manylinux_2_28_x86_64'):
                path = Path(temporary) / f'easysewer-2.0.1-py3-none-{tag}.whl'
                wheel(path, 'linux-x86_64', tag=tag)
                with self.subTest(tag=tag), self.assertRaisesRegex(ValueError, 'wheel name'):
                    SELECT.verify_wheel(path, '2.0.1', 'linux-x86_64')

    def test_selects_four_native_wheels_and_source_not_pure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            packages = root / 'packages'
            packages.mkdir()
            for target, tag in SELECT.TAGS.items():
                wheel(packages / f'easysewer-2.0.1-py3-none-{tag}.whl', target)
            (packages / 'pure').mkdir()
            (packages / 'pure/easysewer-2.0.1-py3-none-any.whl').write_bytes(b'not selected')
            with tarfile.open(packages / 'easysewer-2.0.1.tar.gz', 'w:gz') as archive:
                raw = b'Name: easysewer\nVersion: 2.0.1\n'
                member = tarfile.TarInfo('easysewer-2.0.1/PKG-INFO')
                member.size = len(raw)
                archive.addfile(member, io.BytesIO(raw))
            records = SELECT.select(packages, '2.0.1', root / 'selected')
            self.assertEqual(len(records), 5)
            self.assertFalse(any(row['name'].endswith('any.whl') for row in records))
            (packages / 'unexpected.whl').write_bytes(b'extra')
            with self.assertRaisesRegex(ValueError, 'additional native wheel'):
                SELECT.select(packages, '2.0.1', root / 'bad')

    def test_rejects_missing_targets_and_nonfinal_versions(self):
        with tempfile.TemporaryDirectory() as temporary:
            for version in ('2.0.1', '2.0.1rc1', '../../escape'):
                with self.subTest(version=version), self.assertRaises(ValueError):
                    SELECT.select(Path(temporary), version, Path(temporary) / 'selected')


if __name__ == '__main__':
    unittest.main()
