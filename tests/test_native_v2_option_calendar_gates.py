"""Actual native admission and full output for partial report calendars."""
import hashlib, os, struct, tempfile, unittest
from pathlib import Path
from easysewer import get_native_capabilities
from easysewer.model import Model
from test_option_calendar_gates_v2 import BASE, CALENDARS, load
from test_native_v2_regulator_fields import FAMILIES, library
from test_native_v2_title_report_gates import solve

EVIDENCE = []


@unittest.skipUnless(get_native_capabilities()['swmm_solver'] and get_native_capabilities()['flexible_ponding'],
                     'Both native engines required')
class NativeOptionCalendarGateTests(unittest.TestCase):
    def test_partial_calendar_admission_and_full_output_roundtrip(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for family, name, symbol in FAMILIES:
                lib, path = library(name, symbol)
                for tail, expected_date, valid in CALENDARS:
                    with self.subTest(family=family, tail=tail):
                        m = load(tail)
                        source = BASE + '[OPTIONS]\n' + tail
                        if valid:
                            result = solve(self, lib, root, source)
                            restored = Model.from_json_document(m.to_json_document(), strict=True)
                            actual = solve(self, lib, root, restored.to_document(normalize=True).text)
                            self.assertEqual(actual, result)
                            EVIDENCE.append(dict(family=family, tail=tail, native_code=0,
                                out_sha256=hashlib.sha256(result['out']).hexdigest(),
                                report_sha256=hashlib.sha256(result['report']).hexdigest(),
                                library_sha256=hashlib.sha256(Path(path).read_bytes()).hexdigest()))
                        else:
                            for text in (source, m.to_document(normalize=True).text):
                                inp, rpt, out = (root/('model'+s) for s in ('.inp', '.rpt', '.out'))
                                inp.write_text(text, encoding='utf-8')
                                try:
                                    code = lib.swmm_open(os.fsencode(inp), os.fsencode(rpt), os.fsencode(out))
                                    self.assertEqual(code, 193)
                                finally: self.assertEqual(lib.swmm_close(), 0)
                            self.assertFalse(m.validate(for_run=True).is_valid)
                            EVIDENCE.append(dict(family=family, tail=tail, native_code=193))

    def test_partial_decimal_clock_matches_explicit_calendar_and_output_times(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for family, name, symbol in FAMILIES:
                lib, _ = library(name, symbol)
                for token, literal, first_minute in (
                    ('17698200', '00:00', 1),
                    ('17698200.083333332', '00:05', 5),
                    ('17698200.1', '00:06', 6),
                    ('17698200.166388888', '00:09:59', 10)):
                    with self.subTest(family=family, token=token):
                        source = BASE+'[OPTIONS]\nREPORT_START_TIME '+token+'\n'
                        result = solve(self, lib, root, source)
                        explicit = BASE+'[OPTIONS]\nREPORT_START_DATE 01/01/2020\nREPORT_START_TIME '+literal+'\n'
                        reference = solve(self, lib, root, explicit)
                        m = load('REPORT_START_TIME '+token+'\n')
                        self.assertEqual(solve(self, lib, root, m.to_document(normalize=True).text), result)
                        raw = result['out']
                        _, _, pos, count, error, _ = struct.unpack_from('<6i', raw, len(raw)-24)
                        if token == '17698200.1':
                            # Sentinel cancellation changes floor(report delay /
                            # step) in the engine's advertised start, although
                            # every saved timestamp and result is identical.
                            advertised = struct.unpack_from('<d', raw, pos-12)[0]
                            reference_start = struct.unpack_from('<d', reference['out'], pos-12)[0]
                            self.assertAlmostEqual((advertised-43831)*86400, 300, places=5)
                            self.assertAlmostEqual((reference_start-43831)*86400, 240, places=5)
                            self.assertEqual(raw[:pos-12], reference['out'][:pos-12])
                            self.assertEqual(raw[pos-4:], reference['out'][pos-4:])
                            self.assertEqual(result['report'], reference['report'])
                        else:
                            self.assertEqual(reference, result)
                        self.assertEqual(error, 0)
                        self.assertEqual(count, 11-first_minute)
                        stride = (len(raw)-24-pos)//count
                        seconds = [(struct.unpack_from('<d', raw, pos+i*stride)[0]-43831)*86400 for i in range(count)]
                        # Native output timestamps deliberately include a 1 ms offset.
                        for actual, minute in zip(seconds, range(first_minute, 11)):
                            self.assertAlmostEqual(actual, 60*minute+.001, places=5)
                        EVIDENCE.append(dict(family=family, token=token, periods=count,
                            seconds=seconds, out_sha256=hashlib.sha256(raw).hexdigest()))


if __name__ == '__main__': unittest.main()
