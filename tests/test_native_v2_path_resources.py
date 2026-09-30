"""Long paths across native input/output owners, with bytewise OUT oracles."""
import os
from pathlib import Path
import unittest

from test_native_v2_path_io import PathCase
from test_native_v2_checkpoint_coordinator import native_source
from test_native_v2_checkpoint_gage import fixture as rain_fixture
from test_native_v2_lid_report_io import fixture as lid_fixture


class ResourceChecks(PathCase):
    def sized(self, length, name):
        parent = self.case/'data'
        remaining = length-len(os.fsencode(parent))-2-len(os.fsencode(name))
        while remaining > 180:
            parent /= 'x'*150
            remaining -= 151
        self.assertGreater(remaining, 0)
        path = parent/('y'*remaining)/name
        path.parent.mkdir(parents=True, exist_ok=True)
        self.assertEqual(len(os.fsencode(path)), length)
        return path

    def test_long_primary_and_series_paths_preserve_results(self):
        data = b'0 .1\n0:05 .3\n0:10 .2\n'
        (self.case/'short.dat').write_bytes(data)
        code, expected, _ = self.run_model(self.model_with_series('short.dat'))
        self.assertEqual(code, 0)
        for length in (261, 600):
            long = self.sized(length, 'rain.dat')
            long.write_bytes(data)
            if length == 261:
                alias = Path(os.fsdecode(os.fsencode(long)[:258]))
                alias.write_bytes(b'0 0\n0:05 0\n0:10 0\n')
            body = self.model_with_series(str(long.relative_to(self.case)))
            code, actual, _ = self.run_model(body)
            self.assertEqual((code, actual), (0, expected))
            inp, rpt, out = (self.sized(length, 'model'+suffix) for suffix in ('.inp', '.rpt', '.out'))
            inp.write_text(self.model_with_series(str(long)), encoding='utf-8')
            code = self.lib.swmm_run(*[os.fsencode(p) for p in (inp, rpt, out)])
            self.assertEqual(code, 0)
            self.assertEqual(out.read_bytes(), expected)

    def test_over_capacity_primary_path_preserves_files_and_recovers(self):
        body = self.model_with_series('series.dat')
        (self.case/'series.dat').write_bytes(b'0 .1\n0:05 .3\n0:10 .2\n')
        code, expected, _ = self.run_model(body)
        self.assertEqual(code, 0)
        for index in range(3):
            paths = [self.case/'network.inp', self.root/'guard.rpt', self.root/'guard.out']
            paths[1].write_bytes(b'original-report')
            paths[2].write_bytes(b'original-output')
            args = [os.fsencode(path) for path in paths]
            args[index] = b'x'*4096
            self.assertEqual(self.lib.swmm_run(*args), 364)
            self.assertEqual(paths[1].read_bytes(), b'original-report')
            self.assertEqual(paths[2].read_bytes(), b'original-output')
            code, actual, _ = self.run_model(body)
            self.assertEqual((code, actual), (0, expected))

    def test_long_input_owners_match_short_path_outputs(self):
        # These generators produce real weather, rainfall, RUNOFF, RDII and
        # routing input files. Moving them changes only resource paths.
        for kind in ('climate', 'combined', 'rdii-binary', 'rdii-text', 'routing'):
            folder = self.case/kind
            folder.mkdir()
            body = native_source(folder, kind)
            code, expected, _ = self.run_model(body)
            self.assertEqual(code, 0, kind)
            found = 0
            for path in tuple(folder.iterdir()):
                if not path.is_file() or str(path) not in body:
                    continue
                found += 1
                long = self.sized(600, kind+'-'+path.name)
                long.write_bytes(path.read_bytes())
                body = body.replace(str(path), str(long))
                path.unlink()  # a successful run must use the relocated bytes
            self.assertGreater(found, 0, kind)
            with self.subTest(kind=kind):
                code, actual, _ = self.run_model(body)
                self.assertEqual((code, actual), (0, expected))

    def test_long_lid_report_matches_short_path_report(self):
        short = self.root/'lid.txt'
        code, expected, _ = self.run_model(lid_fixture(detail=str(short)))
        self.assertEqual(code, 0)
        detail = short.read_bytes()
        long = self.sized(600, 'lid.txt')
        code, actual, _ = self.run_model(lid_fixture(detail=str(long)))
        self.assertEqual((code, actual), (0, expected))
        self.assertEqual(long.read_bytes(), detail)

    def test_long_generated_rain_cache_can_be_consumed(self):
        folder = self.case/'rain'
        folder.mkdir()
        body = rain_fixture(folder, source='rain-file', co=True)
        short = self.case/'rain.bin'
        code, expected, _ = self.run_model(body+f'[FILES]\nSAVE RAINFALL "{short}"\n')
        self.assertEqual(code, 0)
        cache = short.read_bytes()
        long = self.sized(600, 'rain.bin')
        code, actual, _ = self.run_model(body+f'[FILES]\nSAVE RAINFALL "{long}"\n')
        self.assertEqual((code, actual), (0, expected))
        self.assertEqual(long.read_bytes(), cache)
        code, actual, _ = self.run_model(body+f'[FILES]\nUSE RAINFALL "{long}"\n')
        self.assertEqual((code, actual), (0, expected))


class StandardResources(ResourceChecks, unittest.TestCase):
    family = 'standard'


class CustomResources(ResourceChecks, unittest.TestCase):
    family = 'custom'
