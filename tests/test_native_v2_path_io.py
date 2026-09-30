"""Real native file access from a CWD different from the INP directory."""
import ctypes
import os
from pathlib import Path
import tempfile
import unittest

from test_native_v2_routing_io import SOURCE
from test_native_v2_standard_io import direct_library, handles


class PathCase:
    @classmethod
    def setUpClass(cls):
        library = os.environ.get('EASYSEWER_PATH_TEST_'+cls.family.upper())
        if not library:
            raise unittest.SkipTest('Explicit candidate path library required')
        cls.lib, _ = direct_library(library, revision_symbol=(
            'swmm_getEasySewerStandardFixes' if cls.family == 'standard'
            else 'swmm_getEasySewerNativeIOFixes'))
        cls.lib.swmm_run.argtypes = [ctypes.c_char_p]*3
        cls.lib.swmm_run.restype = ctypes.c_int

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.case = self.root/'model'
        self.case.mkdir()
        self.old_cwd = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, self.old_cwd)
        self.addCleanup(self.lib.swmm_close)

    def run_model(self, source, *, main_style='absolute'):
        inp = self.case/'network.inp'
        inp.write_bytes(source if isinstance(source, bytes) else source.encode())
        name = str(inp)
        if main_style == 'relative':
            name = str(inp.relative_to(self.root))
        elif main_style == 'forward':
            name = inp.as_posix()
        elif main_style == 'drive-relative':
            name = self.root.drive+str(inp.relative_to(self.root))
        elif main_style == 'rooted':
            name = str(inp)[len(inp.drive):]
        rpt, out = self.root/'result.rpt', self.root/'result.out'
        out.write_bytes(b'old-output')
        code = self.lib.swmm_run(os.fsencode(name), os.fsencode(rpt), os.fsencode(out))
        return code, out.read_bytes(), rpt.read_bytes()

    def model_with_series(self, name):
        return SOURCE+f'[TIMESERIES]\nS FILE "{name}"\n[INFLOWS]\nJ FLOW S FLOW 1 1\n'


class PathChecks(PathCase):
    def test_external_resources_use_model_directory(self):
        data = b'0 .1\n0:05 .3\n0:10 .2\n'
        (self.case/'series.dat').write_bytes(data)
        code, expected, _ = self.run_model(self.model_with_series('series.dat'))
        self.assertEqual(code, 0)
        names = ['space dir/series.dat']
        if os.name != 'nt':
            names += ['rain:2026.dat', r'\rain.dat', r'sub\rain.dat']
        for name in names:
            path = self.case/name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            # A wrong CWD match must not mask an incorrect relative-path rule.
            wrong = self.root/name
            wrong.parent.mkdir(parents=True, exist_ok=True)
            wrong.write_bytes(b'0 0\n0:05 0\n0:10 0\n')
            with self.subTest(name=name):
                code, actual, _ = self.run_model(self.model_with_series(name))
                self.assertEqual(code, 0)
                self.assertEqual(actual, expected)

    def test_main_path_forms_resolve_relative_resources(self):
        (self.case/'series.dat').write_bytes(b'0 .1\n0:05 .3\n0:10 .2\n')
        body = self.model_with_series('series.dat')
        code, expected, _ = self.run_model(body)
        self.assertEqual(code, 0)
        styles = ['relative', 'forward']
        if os.name == 'nt':
            styles += ['drive-relative', 'rooted']
        for style in styles:
            with self.subTest(style=style):
                code, actual, _ = self.run_model(body, main_style=style)
                self.assertEqual(code, 0)
                self.assertEqual(actual, expected)

    def test_physical_record_boundaries_and_line_endings(self):
        code, expected, _ = self.run_model(SOURCE)
        self.assertEqual(code, 0)
        for length in (1022, 1023, 1024, 1025):
            for ending in (b'', b'\n', b'\r\n'):
                raw = SOURCE.encode()+b';'+b'x'*(length-1)+ending
                with self.subTest(length=length, ending=ending):
                    code, actual, _ = self.run_model(raw)
                    self.assertEqual(code, 0 if length <= 1023 else 200)
                    self.assertEqual(actual, expected if length <= 1023 else b'old-output')
        for ending in ('\n', '\r\n'):
            code, actual, _ = self.run_model(SOURCE.replace('\n', ending))
            self.assertEqual((code, actual), (0, expected))

    def test_control_bytes_are_rejected_and_same_process_recovers(self):
        code, expected, _ = self.run_model(SOURCE)
        self.assertEqual(code, 0)
        before = handles()
        for byte in (b'\0', b'\x1a'):
            for ending in (b'', b'\n', b'\r\n'):
                for prefix in (b'', b'; comment ', b';'+b'x'*1021):
                    raw = SOURCE.encode()+prefix+byte+b'hidden suffix'+ending
                    with self.subTest(byte=byte, ending=ending, prefix_length=len(prefix)):
                        code, actual, report = self.run_model(raw)
                        self.assertEqual(code, 200)
                        self.assertIn(b'ERROR 365', report)
                        self.assertEqual(actual, b'old-output')
                        code, actual, _ = self.run_model(SOURCE)
                        self.assertEqual((code, actual), (0, expected))
        self.assertLessEqual(handles(), before+1)


class StandardPaths(PathChecks, unittest.TestCase):
    family = 'standard'


class CustomPaths(PathChecks, unittest.TestCase):
    family = 'custom'
