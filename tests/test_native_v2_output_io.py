"""Direct SMO ABI oracles; candidate libraries are selected explicitly for QA."""

import ctypes as C
import os
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

from easysewer.utils import probe_library_path
from test_output_v2 import binary_output

P = C.POINTER


def output_library():
    path = os.environ.get('EASYSEWER_TEST_OUTPUT_LIBRARY') or probe_library_path('swmm-output')
    if not path:
        raise unittest.SkipTest('Native OUT library unavailable')
    lib = C.CDLL(str(path))
    if not hasattr(lib, 'swmm_getEasySewerOutputIO'):
        raise unittest.SkipTest('Bounded OUT reader is not installed')
    if lib.swmm_getEasySewerOutputIO() != 1:
        raise unittest.SkipTest('Unsupported OUT reader profile')
    signatures = {
        'init': [P(C.c_void_p)], 'close': [P(C.c_void_p)],
        'open': [C.c_void_p, C.c_char_p],
        'getVersion': [C.c_void_p, P(C.c_int)],
        'getFlowUnits': [C.c_void_p, P(C.c_int)],
        'getStartDate': [C.c_void_p, P(C.c_double)],
        'getTimes': [C.c_void_p, C.c_int, P(C.c_int)],
        'getElementName': [C.c_void_p, C.c_int, C.c_int, P(C.c_void_p), P(C.c_int)],
        'checkError': [C.c_void_p, P(C.c_void_p)],
        'clearError': [C.c_void_p], 'free': [P(C.c_void_p)],
    }
    for suffix in ('ProjectSize', 'Units', 'PollutantUnits'):
        signatures['get' + suffix] = [C.c_void_p, P(C.c_void_p), P(C.c_int)]
    for group in ('Subcatch', 'Node', 'Link', 'System'):
        for mode, n in (('Series', 3 if group == 'System' else 4), ('Attribute', 2), ('Result', 2)):
            signatures['get' + group + mode] = [C.c_void_p, *([C.c_int] * n), P(C.c_void_p), P(C.c_int)]
    for name, args in signatures.items():
        fn = getattr(lib, 'SMO_' + name)
        fn.argtypes = args
        fn.restype = None if name in ('free', 'clearError') else C.c_int
    return lib


def regions(raw):
    """Independent offsets for controlled corruption/reordering fixtures."""
    ns, nn, nl, np = struct.unpack_from('<4i', raw, 12)
    _, position, output, *_ = struct.unpack_from('<6i', raw, len(raw) - 24)
    properties = []
    for count in (ns, nn, nl):
        n = struct.unpack_from('<i', raw, position)[0]
        properties.append(position)
        position += 4 + 4 * n * (count + 1)
    variables = []
    for _ in range(4):
        n = struct.unpack_from('<i', raw, position)[0]
        variables.append(position)
        position += 4 + n * 4
    assert position == output - 12
    return properties, variables, output


class NativeOutputIOTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.lib = output_library()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'input.out'
        self.path.write_bytes(binary_output())
        self.handle = C.c_void_p()
        self.assertEqual(self.lib.SMO_init(C.byref(self.handle)), 0)
        self.addCleanup(self.close)

    def close(self):
        self.assertEqual(self.lib.SMO_close(C.byref(self.handle)), 0)
        self.assertFalse(self.handle.value)

    def open(self, expected=0, raw=None):
        if raw is not None:
            # Close the FILE first, but deliberately retain/reuse its handle.
            self.lib.SMO_open(self.handle, os.fsencode(self.path.with_name('absent.out')))
            self.path.write_bytes(raw)
        self.assertEqual(self.lib.SMO_open(self.handle, os.fsencode(self.path)), expected)
        self.assertTrue(self.handle.value)

    def array(self, name, *args, expected=0, kind=C.c_float, handle='default'):
        value = C.c_void_p(1)
        length = C.c_int(-9)
        handle = self.handle if handle == 'default' else handle
        result = getattr(self.lib, 'SMO_' + name)(handle, *args, C.byref(value), C.byref(length))
        self.assertEqual(result, expected, (name, args))
        if expected:
            self.assertFalse(value.value, (name, args))
            self.assertEqual(length.value, 0)
            return ()
        self.assertGreaterEqual(length.value, 0)
        self.assertLess(length.value, 1000)
        self.assertEqual(bool(value.value), bool(length.value))
        try:
            return tuple(C.cast(value, P(kind))[i] for i in range(length.value))
        finally:
            self.lib.SMO_free(C.byref(value))
            self.assertFalse(value.value)
            self.lib.SMO_free(C.byref(value))

    def test_every_normal_query_and_allocated_array_ownership(self):
        self.open()
        self.assertEqual(self.array('getProjectSize', kind=C.c_int), (1, 2, 1, 1, 3))
        self.assertEqual(self.array('getUnits', kind=C.c_int), (0, 0, 2, 1, 0))
        self.assertEqual(self.array('getPollutantUnits', kind=C.c_int), (2, 1, 0))
        for group, name, count, variables in ((0, 'Subcatch', 1, 11), (1, 'Node', 2, 9),
                                               (2, 'Link', 1, 8), (3, 'System', 1, 15)):
            for index in range(count):
                for code in range(variables):
                    args = () if group == 3 else (index,)
                    self.assertEqual(self.array('get' + name + 'Series', *args, code, 1, 7),
                                     tuple(p * 1000 + group * 100 + index * 10 + code for p in range(2, 8)))
                for period in (0, 6):
                    self.assertEqual(self.array('get' + name + 'Result', period, index),
                                     tuple((period + 1) * 1000 + group * 100 + index * 10 + c for c in range(variables)))
            for code in range(variables):
                self.assertEqual(self.array('get' + name + 'Attribute', 2, code),
                                 tuple(3000 + group * 100 + index * 10 + code for index in range(count)))
        for group, names in ((0, ('S',)), (1, ('Earlier', 'J')), (2, ('P',)), (4, ('Count', 'Micro', 'Mass'))):
            for index, name in enumerate(names):
                value, size = C.c_void_p(), C.c_int()
                self.assertEqual(self.lib.SMO_getElementName(self.handle, group, index, C.byref(value), C.byref(size)), 0)
                self.assertEqual(size.value, len(name))
                self.assertEqual(C.string_at(value), name.encode())
                self.lib.SMO_free(C.byref(value))
        for name, args, wanted, kind in (('getVersion', (), 52004, C.c_int),
                ('getFlowUnits', (), 0, C.c_int), ('getTimes', (0,), 60, C.c_int),
                ('getTimes', (1,), 7, C.c_int), ('getStartDate', (), 43831., C.c_double)):
            out = kind()
            self.assertEqual(getattr(self.lib, 'SMO_' + name)(self.handle, *args, C.byref(out)), 0)
            self.assertEqual(out.value, wanted)

    def test_all_group_boundaries_and_exclusive_period_end(self):
        self.open()
        for name, count, variables in (('Subcatch', 1, 11), ('Node', 2, 9), ('Link', 1, 8), ('System', 1, 15)):
            prefix = () if name == 'System' else (0,)
            for start, end in ((-1, 1), (0, 8), (7, 8), (1, 1), (2, 1), (0, 2147483647)):
                self.array('get' + name + 'Series', *prefix, 0, start, end, expected=422)
            for attr in (-1, variables, 2147483647):
                self.array('get' + name + 'Series', *prefix, attr, 0, 1, expected=421)
                self.array('get' + name + 'Attribute', 0, attr, expected=421)
            for period in (-2147483648, -1, 7, 2147483647):
                self.array('get' + name + 'Attribute', period, 0, expected=422)
                self.array('get' + name + 'Result', period, 0, expected=422)
            if name != 'System':
                for index in (-1, count, 2147483647):
                    self.array('get' + name + 'Series', index, 0, 0, 1, expected=423)
                    self.array('get' + name + 'Result', 0, index, expected=423)
        self.assertEqual(self.array('getSystemResult', 0, -999), tuple(range(1300, 1315)))

    def test_null_unopened_closed_and_output_arguments(self):
        calls = [('getProjectSize', (), C.c_int), ('getUnits', (), C.c_int), ('getPollutantUnits', (), C.c_int)]
        for name in ('Subcatch', 'Node', 'Link', 'System'):
            calls.extend((('get' + name + 'Series', (0, 0, 1) if name == 'System' else (0, 0, 0, 1), C.c_float),
                          ('get' + name + 'Attribute', (0, 0), C.c_float),
                          ('get' + name + 'Result', (0, 0), C.c_float)))
        for name, args, kind in calls:
            self.array(name, *args, expected=434, kind=kind)
            self.array(name, *args, expected=-1, kind=kind, handle=None)
        for name, args, kind in (('getVersion', (), C.c_int), ('getFlowUnits', (), C.c_int),
                ('getStartDate', (), C.c_double), ('getTimes', (0,), C.c_int)):
            fn = getattr(self.lib, 'SMO_' + name)
            out = kind(123)
            self.assertEqual(fn(None, *args, C.byref(out)), -1)
            self.assertEqual(out.value, 0)
            self.assertEqual(fn(self.handle, *args, C.byref(out)), 434)
            self.assertEqual(fn(self.handle, *args, None), 424)
        self.open()
        for name, args, _ in calls:
            fn = getattr(self.lib, 'SMO_' + name)
            out, length = C.c_void_p(1), C.c_int(-1)
            self.assertEqual(fn(self.handle, *args, None, C.byref(length)), 424)
            self.assertEqual(length.value, 0)
            self.assertEqual(fn(self.handle, *args, C.byref(out), None), 424)
            self.assertFalse(out.value)
        self.assertEqual(self.lib.SMO_init(None), -1)
        self.assertEqual(self.lib.SMO_open(None, None), -1)
        self.assertEqual(self.lib.SMO_open(self.handle, None), 421)
        self.assertEqual(self.lib.SMO_close(None), -1)
        self.lib.SMO_clearError(None)
        self.lib.SMO_free(None)
        for handle, group, index, expected in ((None, 0, 0, -1), (self.handle, -1, 0, 421),
                (self.handle, 3, 0, 421), (self.handle, 5, 0, 421),
                (self.handle, 1, -1, 423), (self.handle, 1, 2, 423)):
            self.array('getElementName', group, index, expected=expected, handle=handle)
        out, length = C.c_void_p(1), C.c_int(-1)
        self.assertEqual(self.lib.SMO_getElementName(self.handle, 0, 0, None, C.byref(length)), 424)
        self.assertEqual(length.value, 0)
        self.assertEqual(self.lib.SMO_getElementName(self.handle, 0, 0, C.byref(out), None), 424)
        self.assertFalse(out.value)
        self.assertEqual(self.lib.SMO_getTimes(self.handle, -1, C.byref(length)), 421)
        self.assertEqual(length.value, 0)
        self.assertEqual(self.lib.SMO_checkError(None, C.byref(out)), -1)
        self.assertFalse(out.value)
        self.assertEqual(self.lib.SMO_checkError(self.handle, None), 424)
        # A later successful query must not erase the caller's pending error.
        self.array('getSystemAttribute', 0, 0)
        self.assertEqual(self.lib.SMO_checkError(self.handle, C.byref(out)), 424)
        self.lib.SMO_free(C.byref(out))
        self.lib.SMO_clearError(self.handle)
        self.assertEqual(self.lib.SMO_checkError(self.handle, C.byref(out)), 0)
        self.assertFalse(out.value)
        self.close()
        self.close()
        for name, args, kind in calls:
            self.array(name, *args, expected=-1, kind=kind)

    def test_malformed_layout_failure_retry_and_reopen(self):
        raw = binary_output()
        props, codes, output = regions(raw)
        cases = [raw[:n] for n in (0, 1, 4, 20, 24, 28, 100, len(raw) - 24, len(raw) - 1)]
        for offset, value in ((0, 123), (4, 52003), (8, 6), (12, -1), (16, 2147483647),
                (20, -1), (24, -1), (28, 0), (28, 65537), (len(raw) - 24, 0),
                (len(raw) - 20, 28), (len(raw) - 16, 2147483647), (len(raw) - 12, -1),
                (len(raw) - 12, 8), (len(raw) - 8, -1), (len(raw) - 4, 123),
                (props[0], 65), (codes[0], 0), (codes[0], 2147483647),
                (codes[0] + 4, -1), (codes[0] + 8, 0), (output - 4, 0)):
            data = bytearray(raw)
            struct.pack_into('<i', data, offset, value)
            cases.append(bytes(data))
        data = bytearray(raw)
        struct.pack_into('<i', data, 0, 123)
        struct.pack_into('<i', data, len(raw) - 4, 123)
        cases.append(bytes(data))
        cases.extend((raw[:-24] + b'junk' + raw[-24:], raw[:-224] + raw[-24:],
                      binary_output(names=(('S',), ('same', 'SAME'), ('P',), ())),
                      binary_output(names=(('S',), ('A\x00B',), ('P',), ()))))
        width = (len(raw) - 24 - output) // 7
        for offset in (output - 12, output, output + (7 - 1) * width):
            data = bytearray(raw)
            struct.pack_into('<d', data, offset, float('nan'))
            cases.append(bytes(data))
        # Known float input property, not a property type integer.
        data = bytearray(raw)
        struct.pack_into('<f', data, props[0] + 8, float('inf'))
        cases.append(bytes(data))
        for n, data in enumerate(cases):
            with self.subTest(case=n):
                self.open(expected=435, raw=data)
                self.array('getNodeSeries', 0, 0, 0, 1, expected=434)
                self.open(raw=raw)
                self.assertEqual(self.array('getNodeSeries', 0, 0, 0, 1), (1100.,))
        self.assertEqual(self.lib.SMO_open(self.handle, os.fsencode(self.path.with_name('missing'))), 434)
        self.array('getProjectSize', expected=434, kind=C.c_int)
        self.open(raw=raw)

    def test_header_code_mapping_future_code_and_byte_names(self):
        raw = bytearray(binary_output(extra=True))
        _, codes, _ = regions(raw)
        struct.pack_into('<2i', raw, codes[1] + 4, 1, 0)
        self.open(raw=raw)
        self.assertEqual(self.array('getNodeSeries', 0, 0, 0, 1), (1101.,))
        self.assertEqual(self.array('getNodeSeries', 0, 1, 0, 1), (1100.,))
        self.assertEqual(self.array('getNodeResult', 0, 0), tuple(range(1100, 1109)))
        self.assertEqual(self.array('getSystemSeries', 99, 0, 2), (1399., 2399.))
        self.open(raw=binary_output(names=(('流域',), ('é', 'É'), ('管道',), ())))
        value, length = C.c_void_p(), C.c_int()
        self.assertEqual(self.lib.SMO_getElementName(self.handle, 2, 0, C.byref(value), C.byref(length)), 0)
        self.assertEqual(C.string_at(value, length.value), '管道'.encode())
        self.lib.SMO_free(C.byref(value))

    def test_empty_groups_zero_periods_units_and_diagnostic_footer(self):
        for units in range(6):
            self.open(raw=binary_output(flow_units=units, names=((), (), (), ())))
            self.assertEqual(self.array('getUnits', kind=C.c_int), (int(units >= 3), units, 3))
            self.assertEqual(self.array('getPollutantUnits', kind=C.c_int), ())
            for name in ('Subcatch', 'Node', 'Link'):
                self.assertEqual(self.array('get' + name + 'Attribute', 0, 0), ())
                self.array('get' + name + 'Result', 0, 0, expected=423)
        self.open(expected=436, raw=binary_output(periods=0))
        self.array('getSystemSeries', 0, 0, 1, expected=434)
        data = bytearray(binary_output())
        struct.pack_into('<i', data, len(data) - 8, 200)
        self.open(expected=10, raw=data)
        self.assertEqual(self.array('getSystemAttribute', 0, 0), (1300.,))
        message = C.c_void_p()
        self.assertEqual(self.lib.SMO_checkError(self.handle, C.byref(message)), 10)
        self.assertIn(b'simulation error', C.string_at(message))
        self.lib.SMO_free(C.byref(message))
        self.lib.SMO_clearError(self.handle)
        self.assertEqual(self.lib.SMO_checkError(self.handle, C.byref(message)), 0)
        self.assertFalse(message.value)

    def test_nonfinite_results_and_backwards_middle_time_publish_no_partial_array(self):
        raw = binary_output()
        _, _, offset = regions(raw)
        width = (len(raw) - 24 - offset) // 7
        for position, fmt, value in ((offset + width * 2, '<d', 43831.),
                (offset + width * 2 + 8 + 11 * 4, '<f', float('inf')),
                (offset + width * 2 + 8 + 11 * 4, '<f', float('nan'))):
            data = bytearray(raw)
            struct.pack_into(fmt, data, position, value)
            self.open(raw=data)
            self.array('getNodeSeries', 0, 0, 0, 7, expected=435)
            self.assertEqual(self.array('getNodeSeries', 0, 0, 0, 1), (1100.,))
        self.open(raw=raw)
        with self.path.open('ab') as stream:
            stream.write(b'x')
        self.array('getNodeSeries', 0, 0, 0, 7, expected=435)

    def test_unicode_paths_and_legacy_wrapper_lifecycle(self):
        from easysewer.runtime._output_api import SWMMOutputAPI, SWMMOutputError
        self.path = self.path.with_name('流域计算结果.out')
        self.path.write_bytes(binary_output())
        self.open()
        library_path = os.environ.get('EASYSEWER_TEST_OUTPUT_LIBRARY') or probe_library_path('swmm-output')
        with patch('easysewer.runtime._output_api.require_native_capability', return_value=str(library_path)):
            reader = SWMMOutputAPI()
        handle = reader.handle.value
        with reader as opened:
            self.assertIs(opened, reader)
            self.assertEqual(reader.handle.value, handle)
            reader.open(self.path)
            self.assertEqual(reader.get_node_series(0, 0, 0, 2), [1100., 2100.])
            for bad in (2**32, -(2**32), True, 1.0):
                with self.assertRaises(ValueError):
                    reader.get_node_series(bad, 0, 0, 2)
            with self.assertRaises(ValueError):
                reader.open(str(self.path) + '\0suffix')
            with self.assertRaises(SWMMOutputError) as caught:
                reader.open(self.path.with_name('missing.out'))
            self.assertEqual(caught.exception.code, 434)
            self.assertFalse(reader.closed)
            reader.open(self.path)
            self.assertEqual(reader.get_system_series(14, 0, 1), [1314.])
            # Python conversion failure must still release the native array.
            real_free = reader.lib.SMO_free
            with patch.object(reader.lib, 'SMO_free', wraps=real_free) as freed:
                with patch('easysewer.runtime._output_api.ctypes.cast', side_effect=RuntimeError('copy failed')):
                    with self.assertRaisesRegex(RuntimeError, 'copy failed'):
                        reader.get_node_series(0, 0, 0, 2)
                self.assertEqual(freed.call_count, 1)
        self.assertTrue(reader.closed)
        reader.close()
        with self.assertRaisesRegex(ValueError, 'closed'):
            reader.get_times(1)
        with self.assertRaisesRegex(ValueError, 'closed'):
            reader.__enter__()
        with patch.object(reader, 'close', side_effect=SWMMOutputError('close', 435)):
            self.assertFalse(reader.__exit__(RuntimeError, RuntimeError('primary'), None))
            with self.assertRaises(SWMMOutputError):
                reader.__exit__(None, None, None)


if __name__ == '__main__':
    unittest.main()
