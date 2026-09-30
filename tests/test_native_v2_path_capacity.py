"""Real files at the native byte limit, beyond synthetic buffer checks."""
import os
from pathlib import Path
import unittest

from test_native_v2_path_io import PathCase


class CapacityChecks(PathCase):
    def sized_path(self, length, name):
        directory = self.case
        remaining = length-len(os.fsencode(directory))-2-len(os.fsencode(name))
        while remaining > 180:
            directory /= 'd'*150
            remaining -= 151
        path = directory/('e'*remaining)/name
        path.parent.mkdir(parents=True, exist_ok=True)
        self.assertEqual(len(os.fsencode(path)), length)
        return path

    def test_actual_main_and_external_file_at_4094_and_4095_bytes(self):
        (self.case/'series.dat').write_bytes(b'0 .1\n0:05 .3\n0:10 .2\n')
        code, expected, _ = self.run_model(self.model_with_series('series.dat'))
        self.assertEqual(code, 0)
        for length in (4094, 4095):
            with self.subTest(length=length):
                inp = self.sized_path(length, 'model-near-capacity.inp')
                count = length-len(os.fsencode(inp.parent))-1
                name = 's'*(count-4)+'.dat'
                data = inp.parent/name
                data.write_bytes((self.case/'series.dat').read_bytes())
                self.assertEqual(len(os.fsencode(data)), length)
                inp.write_text(self.model_with_series(name), encoding='utf-8')
                rpt, out = self.root/'capacity.rpt', self.root/'capacity.out'
                code = self.lib.swmm_run(*[os.fsencode(p) for p in (inp, rpt, out)])
                self.assertEqual(code, 0)
                self.assertEqual(out.read_bytes(), expected)
                # A one-byte extension past the native limit is rejected
                # during parsing, even though the existing prefix is valid.
                if length == 4095:
                    inp.write_text(self.model_with_series(name+'x'), encoding='utf-8')
                    out.write_bytes(b'original-output')
                    self.assertEqual(self.lib.swmm_run(*[os.fsencode(p) for p in (inp, rpt, out)]), 200)
                    self.assertIn(b'ERROR 364', rpt.read_bytes())
                    self.assertEqual(out.read_bytes(), b'original-output')
                    inp.write_text(self.model_with_series(name), encoding='utf-8')
                    self.assertEqual(self.lib.swmm_run(*[os.fsencode(p) for p in (inp, rpt, out)]), 0)
                    self.assertEqual(out.read_bytes(), expected)


class StandardCapacity(CapacityChecks, unittest.TestCase):
    family = 'standard'


class CustomCapacity(CapacityChecks, unittest.TestCase):
    family = 'custom'
