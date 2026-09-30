"""Actual scratch owners must use TEMPDIR and fail when it is unusable."""
import ctypes
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from easysewer.runtime._checkpoint_native import NativeCheckpoint
from test_native_v2_path_io import PathCase
from test_native_v2_routing_io import SOURCE
from test_native_v2_checkpoint_gage import fixture as rain_fixture
from test_native_v2_rdii_io import SOURCE as RDII_SOURCE
from test_native_v2_standard_io import handles


class ScratchChecks(PathCase):
    def source(self, kind):
        if kind == 'out':
            return SOURCE
        if kind == 'rdii':
            return RDII_SOURCE
        return rain_fixture(self.case, source='rain-file', co=True)

    def start(self, body, directory):
        (self.case/'scratch.inp').write_text(
            body+f'\n[OPTIONS]\nTEMPDIR "{directory}"\n', encoding='utf-8')
        code = self.lib.swmm_open(os.fsencode(self.case/'scratch.inp'),
                                 os.fsencode(self.root/'scratch.rpt'), b'')
        return code or self.lib.swmm_start(1)

    def finish(self, output):
        elapsed = ctypes.c_double()
        for _ in range(20000):
            self.assertEqual(self.lib.swmm_step(ctypes.byref(elapsed)), 0)
            if elapsed.value == 0:
                break
        else:
            self.fail('Simulation did not finish')
        self.assertEqual(self.lib.swmm_end(), 0)
        raw = output.read_bytes()
        self.assertEqual(self.lib.swmm_close(), 0)
        return raw

    def test_scratch_owners_use_explicit_directory_and_close_cleanly(self):
        decoy = self.root/'environment-temp'
        decoy.mkdir()
        for kind in ('out', 'rain', 'rdii'):
            body = self.source(kind)
            code, expected, _ = self.run_model(body)
            self.assertEqual(code, 0)
            for relative in (False, True):
                selected = self.case/(kind+('-relative' if relative else '-absolute'))
                reference = selected.name if relative else str(selected)
                # The engine creates the final directory if its parent exists.
                with self.subTest(kind=kind, relative=relative), patch.dict(os.environ, {'TMP': str(decoy), 'TEMP': str(decoy)}):
                    try:
                        self.assertEqual(self.start(body, reference), 0)
                        api = NativeCheckpoint(SimpleNamespace(lib=self.lib), b't'*32)
                        output = Path(next(item.path for item in api.outputs() if item.role == 1))
                        self.assertEqual(output.resolve().parent, selected.resolve())
                        self.assertTrue(selected.is_dir())
                        self.assertEqual(len(tuple(selected.iterdir())), 1 if kind == 'out' else 2)
                        self.assertEqual(self.finish(output), expected)
                        self.assertEqual(tuple(selected.iterdir()), ())
                        self.assertEqual(tuple(decoy.iterdir()), ())
                    finally:
                        self.lib.swmm_close()

    def test_unusable_directory_fails_without_fallback_and_retry_succeeds(self):
        regular = self.case/'regular-file'
        regular.write_bytes(b'original input')
        missing = self.case/'missing-parent'/'scratch'
        for kind, expected_code in (('out', 307), ('rain', 313), ('rdii', 341)):
            body = self.source(kind)
            code, expected, _ = self.run_model(body)
            self.assertEqual(code, 0)
            before = handles()
            for target in (regular, missing):
                with self.subTest(kind=kind, target=target.name):
                    try:
                        self.assertEqual(self.start(body, str(target)), expected_code)
                    finally:
                        self.assertEqual(self.lib.swmm_close(), 0)
                    self.assertEqual(regular.read_bytes(), b'original input')
                    self.assertFalse(missing.exists())
                    code, actual, _ = self.run_model(body)
                    self.assertEqual((code, actual), (0, expected))
            self.assertLessEqual(handles(), before+1)

    def test_long_scratch_directory_uses_full_path(self):
        selected = self.case
        for index in range(4):
            selected /= ('long'+str(index)+'x'*125)
        selected.mkdir(parents=True)
        self.assertGreater(len(os.fsencode(selected)), 500)
        before = handles()
        for kind in ('out', 'rain', 'rdii'):
            with self.subTest(kind=kind):
                body = self.source(kind)
                code, expected, _ = self.run_model(body)
                self.assertEqual(code, 0)
                try:
                    self.assertEqual(self.start(body, str(selected)), 0)
                    api = NativeCheckpoint(SimpleNamespace(lib=self.lib), b't'*32)
                    output = Path(next(item.path for item in api.outputs() if item.role == 1))
                    self.assertEqual(output.resolve().parent, selected.resolve())
                    self.assertEqual(self.finish(output), expected)
                    self.assertEqual(tuple(selected.iterdir()), ())
                finally:
                    self.lib.swmm_close()
        self.assertLessEqual(handles(), before+1)


class StandardScratch(ScratchChecks, unittest.TestCase):
    family = 'standard'


class CustomScratch(ScratchChecks, unittest.TestCase):
    family = 'custom'
