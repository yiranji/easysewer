"""Solver-side OUT lifecycle, saved-value bounds and native report rereads."""

import ctypes as C
import os
from pathlib import Path
import struct
import tempfile
import unittest

from easysewer.io.output import OutputReader
from easysewer.io.output_metadata import OutputMetadata
from easysewer.utils import probe_library_path
from test_native_v2_routing_io import SOURCE as NETWORK

SOURCE = NETWORK.replace('[JUNCTIONS]', 'FLOW_ROUTING DYNWAVE\nALLOW_PONDING YES\n[JUNCTIONS]') + '''
[REPORT]
SUBCATCHMENTS ALL
NODES ALL
LINKS ALL
[RAINGAGES]
R INTENSITY 0:01 1 TIMESERIES Rain
[TIMESERIES]
Rain 0 .5 0:10 0
[SUBCATCHMENTS]
S R J 2 25 200 1 0
[SUBAREAS]
S .01 .1 .05 .05 25 OUTLET
[INFILTRATION]
S 3 .5 4 7 0
[POLLUTANTS]
Q MG/L 1 1 1 0 NO * 0 0 0
'''


def solver(family):
    path = os.environ.get('EASYSEWER_SOLVER_OUTPUT_TEST_' + family.upper())
    path = path or probe_library_path('swmm5' if family == 'standard' else 'flexible_ponding')
    if not path:
        raise unittest.SkipTest('Native library unavailable')
    lib = C.CDLL(str(path))
    if not hasattr(lib, 'swmm_getEasySewerSolverOutputIO'):
        raise unittest.SkipTest('Solver OUT I/O revision 1 is not installed')
    if lib.swmm_getEasySewerSolverOutputIO() != 1:
        raise unittest.SkipTest('Unsupported solver OUT I/O revision')
    for name, args, result in (
        ('open', [C.c_char_p] * 3, C.c_int), ('start', [C.c_int], C.c_int),
        ('step', [C.POINTER(C.c_double)], C.c_int), ('end', [], C.c_int),
        ('report', [], C.c_int), ('close', [], C.c_int),
        ('getSavedValue', [C.c_int, C.c_int, C.c_int], C.c_double),
        ('getError', [C.c_char_p, C.c_int], C.c_int),
    ):
        fn = getattr(lib, 'swmm_' + name)
        fn.argtypes, fn.restype = args, result
    return lib


class SolverOutputChecks:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.inp, self.rpt, self.out = (self.root / ('model' + ext) for ext in ('.inp', '.rpt', '.out'))
        self.addCleanup(self.lib.swmm_close)

    def open(self, source=SOURCE, scratch=False):
        self.inp.write_text(source)
        self.assertEqual(self.lib.swmm_open(os.fsencode(self.inp), os.fsencode(self.rpt),
                                           b'' if scratch else os.fsencode(self.out)), 0)

    def finish(self):
        elapsed = C.c_double()
        for _ in range(20000):
            self.assertEqual(self.lib.swmm_step(C.byref(elapsed)), 0)
            if not elapsed.value:
                return
        self.fail('Simulation did not finish')

    def test_saved_queries_match_out_and_invalid_indexes_do_not_read_model_memory(self):
        self.open()
        self.assertEqual(self.lib.swmm_start(1), 0)
        self.assertEqual(self.lib.swmm_getSavedValue(303, 0, 1), 0)
        self.finish()
        self.assertEqual(self.lib.swmm_end(), 0)
        with OutputReader(self.out) as reader:
            from easysewer.model import Ref
            for property_code, namespace, object_name, variable in (
                (202, 'subcatchments', 'S', 'swmm:rainfall'),
                (303, 'nodes', 'J', 'swmm:depth'), (410, 'links', 'C', 'swmm:flow'),
            ):
                expected = reader.series(Ref(collection='swmm:' + namespace, key=object_name), variable).values
                for period, value in enumerate(expected, 1):
                    self.assertEqual(self.lib.swmm_getSavedValue(property_code, 0, period), value)
            for code, count in ((202, 1), (303, 2), (410, 1)):
                for index in (-2147483648, -1, count, 2147483647):
                    self.assertEqual(self.lib.swmm_getSavedValue(code, index, 1), 0)
                for period in (-1, 0, 11, 2147483647):
                    self.assertEqual(self.lib.swmm_getSavedValue(code, 0, period), 0)
        self.assertEqual(self.lib.swmm_report(), 0)
        self.assertEqual(self.lib.swmm_close(), 0)
        self.assertIn('Node Time Series Results', self.rpt.read_text())
        self.open()  # A previous run's Nperiods must not permit stale reads.
        self.assertEqual(self.lib.swmm_getSavedValue(303, 0, 1), 0)
        self.assertEqual(self.lib.swmm_close(), 0)

    def test_restart_averages_selection_no_save_and_scratch(self):
        first_saved_date = None
        for averages in (False, True):
            for selection in ('ALL', 'NONE', 'J'):
                with self.subTest(averages=averages, selection=selection):
                    source = SOURCE.replace('NODES ALL', 'NODES ' + selection)
                    source = source.replace('LINKS ALL', 'LINKS NONE' if selection == 'NONE' else 'LINKS ALL')
                    source = source.replace('[REPORT]', '[REPORT]\nAVERAGES ' + ('YES' if averages else 'NO'))
                    self.open(source)
                    snapshots = []
                    for _ in range(2):
                        self.assertEqual(self.lib.swmm_start(1), 0)
                        self.finish()
                        self.assertEqual(self.lib.swmm_end(), 0)
                        snapshots.append(self.out.read_bytes())
                        metadata = OutputMetadata.read(self.out)
                        first_saved_date = struct.unpack_from('<d', snapshots[-1], metadata.output_offset)[0]
                        self.assertEqual(metadata.periods, 10)
                        self.assertEqual(len(metadata.names('swmm:nodes')), 2 if selection == 'ALL' else 0 if selection == 'NONE' else 1)
                        self.assertEqual(self.lib.swmm_report(), 0)
                    self.assertEqual(snapshots[0], snapshots[1])
                    self.assertEqual(self.lib.swmm_close(), 0)
        self.open()
        self.assertEqual(self.lib.swmm_start(0), 0)
        self.finish()
        self.assertEqual(self.lib.swmm_end(), 0)
        self.assertEqual(OutputMetadata.read(self.out).periods, 0)
        self.assertEqual(self.lib.swmm_report(), 0)
        self.assertEqual(self.lib.swmm_close(), 0)
        self.open(scratch=True)
        self.assertEqual(self.lib.swmm_start(1), 0)
        self.finish()
        self.assertEqual(self.lib.swmm_end(), 0)
        self.assertEqual(self.lib.swmm_getSavedValue(1, 0, 1), first_saved_date)
        self.assertEqual(self.lib.swmm_report(), 0)
        self.assertEqual(self.lib.swmm_close(), 0)
        self.assertEqual(self.lib.swmm_close(), 0)

    def test_report_lifecycle_rejects_unsafe_stages(self):
        self.assertEqual(self.lib.swmm_close(), 0)
        self.assertEqual(self.lib.swmm_report(), 501)
        self.open()
        self.assertEqual(self.lib.swmm_report(), 502)
        self.assertEqual(self.lib.swmm_close(), 0)
        self.open()
        self.assertEqual(self.lib.swmm_start(1), 0)
        elapsed = C.c_double()
        for _ in range(125):
            self.assertEqual(self.lib.swmm_step(C.byref(elapsed)), 0)
        self.assertEqual(self.lib.swmm_report(), 503)
        self.assertEqual(self.lib.swmm_end(), 503)
        self.assertEqual(self.lib.swmm_close(), 0)

    def test_truncated_results_abort_report_and_allow_next_project(self):
        for complete_periods in (0, 8):
            with self.subTest(complete_periods=complete_periods):
                self.open()
                self.assertEqual(self.lib.swmm_start(1), 0)
                self.finish()
                self.assertEqual(self.lib.swmm_end(), 0)
                metadata = OutputMetadata.read(self.out)
                # Preserve the next date but remove its object results. This
                # exercises a real short read without a fault-injection DLL.
                width = (self.out.stat().st_size - metadata.output_offset - 24) // metadata.periods
                with self.out.open('r+b') as stream:
                    stream.truncate(metadata.output_offset + complete_periods * width + 8)
                self.assertEqual(self.lib.swmm_report(), 311)
                self.assertEqual(self.lib.swmm_getSavedValue(303, 0, 1), 0)
                self.assertEqual(self.lib.swmm_close(), 0)
                message = C.create_string_buffer(512)
                self.assertEqual(self.lib.swmm_getError(message, len(message)), 311)
                self.open()
                self.assertEqual(self.lib.swmm_start(1), 0)
                self.finish()
                self.assertEqual(self.lib.swmm_end(), 0)
                self.assertEqual(self.lib.swmm_report(), 0)
                self.assertEqual(self.lib.swmm_close(), 0)


class StandardSolverOutputTests(SolverOutputChecks, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.lib = solver('standard')


class CustomSolverOutputTests(SolverOutputChecks, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.lib = solver('custom')


if __name__ == '__main__':
    unittest.main()
