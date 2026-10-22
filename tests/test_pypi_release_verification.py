"""Post-publication checks must not confuse upload success with verified bytes."""
import copy
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
try:
    import verify_pypi_release as VERIFY
finally:
    sys.path.pop(0)


class PyPIVerificationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.expected = self.root / 'expected'
        self.expected.mkdir()
        names = [f'easysewer-2.1.0-py3-none-{tag}.whl' for tag in VERIFY.TAGS.values()]
        names.append('easysewer-2.1.0.tar.gz')
        for name in names:
            (self.expected / name).write_bytes(name.encode())
        self.records = VERIFY.expected_files(self.expected, '2.1.0')
        self.data = {'info': {'name': 'easysewer', 'version': '2.1.0'}, 'urls': [
            {'filename': name, 'digests': {'sha256': row['sha256']}, 'size': row['size'],
             'yanked': False, 'url': 'https://files.pythonhosted.org/packages/' + name}
            for name, row in self.records.items()]}

    def test_matches_all_files_and_downloads_exact_native_wheel(self):
        target = 'linux-x86_64'
        name = f'easysewer-2.1.0-py3-none-{VERIFY.TAGS[target]}.whl'
        with patch.object(VERIFY, 'read_url', side_effect=[json.dumps(self.data).encode(), name.encode()]) as read:
            result = VERIFY.verify_and_download(self.expected, '2.1.0', target, self.root / 'download')
        self.assertEqual(len(result['files']), 5)
        self.assertEqual((self.root / 'download' / name).read_bytes(), name.encode())
        self.assertEqual(read.call_args_list[0].args[0], 'https://pypi.org/pypi/easysewer/2.1.0/json')
        self.assertEqual(read.call_args_list[1].args[0], 'https://files.pythonhosted.org/packages/' + name)

    def test_rejects_changed_yanked_duplicate_or_unexpected_index_files(self):
        for mutation in ('hash', 'size', 'yanked', 'url', 'duplicate', 'extra', 'version'):
            data = copy.deepcopy(self.data)
            row = data['urls'][0]
            if mutation == 'hash': row['digests']['sha256'] = '0' * 64
            if mutation == 'size': row['size'] += 1
            if mutation == 'yanked': row['yanked'] = True
            if mutation == 'url': row['url'] = 'https://example.com/' + row['filename']
            if mutation == 'duplicate': data['urls'].append(row)
            if mutation == 'extra': row['filename'] = 'unexpected.whl'
            if mutation == 'version': data['info']['version'] = '2.0.1'
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                VERIFY.verify_index(data, self.records, '2.1.0')

    def test_incomplete_index_is_pending_then_explicitly_fails(self):
        self.data['urls'].pop()
        self.assertIsNone(VERIFY.verify_index(self.data, self.records, '2.1.0'))
        with patch.object(VERIFY, 'read_url', return_value=json.dumps(self.data).encode()), self.assertRaisesRegex(RuntimeError, 'deadline'):
            VERIFY.verify_and_download(self.expected, '2.1.0', 'linux-x86_64', self.root / 'download', max_wait=0)
        self.assertFalse((self.root / 'download').exists())

    def test_rejects_downloaded_bytes_and_unprepared_versions(self):
        with patch.object(VERIFY, 'read_url', side_effect=[json.dumps(self.data).encode(), b'wrong']), self.assertRaisesRegex(ValueError, 'Downloaded PyPI bytes'):
            VERIFY.verify_and_download(self.expected, '2.1.0', 'linux-x86_64', self.root / 'download')
        self.assertFalse((self.root / 'download').exists())
        for version in ('2.1.0rc1', '../../escape', '2.0.1'):
            with self.subTest(version=version), self.assertRaises(ValueError):
                VERIFY.expected_files(self.expected, version)


if __name__ == '__main__':
    unittest.main()
