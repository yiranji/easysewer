"""Internal controls checkpoint qualification, not whole-solver resume tests.

Set EASYSEWER_CHECKPOINT_STANDARD/CUSTOM to the separately instrumented builds.
The installed production libraries intentionally do not export this test API.
"""
import ctypes as c
from dataclasses import replace
from datetime import timedelta
import os
from pathlib import Path
import re
import struct
import tempfile
import unittest

from test_regulators_v2 import regulator_model
from easysewer.model import Ref


def fixture(*, rule_step=7, variant="mixed"):
    model = regulator_model("SIDE")
    model.update_options(rule_step=timedelta(seconds=rule_step),
                         report_step=timedelta(seconds=10))
    model.nodes.add(replace(model.nodes["O"], id="O2"))
    model.links.add(replace(model.links["P"], id="Q", outlet=Ref(collection="swmm:nodes", key="O2")))
    if variant == "empty":
        return model.to_document().text
    common = """[CURVES]
Response CONTROL 0 .1 1 .7 3 .9
[TIMESERIES]
Settings 0 .2 .01 .8 .03 .1 .05 .6
[REPORT]
CONTROLS YES
"""
    # Multiple THEN/ELSE actions, inactive PID branches, changing capacity,
    # priority conflicts, curve and TS modulation, named expression, EQ/NE.
    program = """[CONTROLS]
VARIABLE Height = NODE J DEPTH
EXPRESSION Difference = Height - .2
RULE PID
IF Difference > .4
THEN ORIFICE P SETTING = PID .08 2 .03
ELSE ORIFICE P SETTING = PID .05 1 .02
PRIORITY 2
RULE Curve
IF NODE J DEPTH >= .6
THEN ORIFICE P SETTING = CURVE Response
ELSE ORIFICE P SETTING = .1
PRIORITY 1
RULE Delayed
IF SIMULATION TIME >= 00:00:14
THEN ORIFICE Q SETTING = TIMESERIES Settings
AND ORIFICE P SETTING = .25
PRIORITY 0
RULE Timed
IF SIMULATION TIME = 00:01:00
THEN ORIFICE Q SETTING = PID .07 1 .01
ELSE ORIFICE Q SETTING = .5
PRIORITY -1
"""
    if variant == "order":
        program = """[CONTROLS]
RULE Early
IF SIMULATION TIME < 00:00:14
THEN ORIFICE P SETTING = .1
RULE Both
IF SIMULATION TIME >= 00:00:14
AND SIMULATION TIME < 00:00:28
THEN ORIFICE Q SETTING = .2
AND ORIFICE P SETTING = .3
RULE Toggle
IF SIMULATION TIME >= 00:00:28
THEN ORIFICE P SETTING = .8
AND ORIFICE Q SETTING = .9
"""
    return model.to_document().text + common + program


class Engine:
    def __init__(self, libpath, root, text):
        self.lib = c.CDLL(str(libpath))
        self.root = Path(root)
        self.paths = [self.root / ("run" + ext) for ext in (".inp", ".rpt", ".out")]
        self.paths[0].write_text(text, encoding="utf-8")
        for name, args, result in (
            ("swmm_open", [c.c_char_p]*3, c.c_int),
            ("swmm_start", [c.c_int], c.c_int),
            ("swmm_step", [c.POINTER(c.c_double)], c.c_int),
            ("swmm_end", [], c.c_int), ("swmm_report", [], c.c_int),
            ("swmm_close", [], c.c_int),
            ("swmm_getValue", [c.c_int, c.c_int], c.c_double),
            ("es_test_controls_save", [c.c_void_p, c.c_size_t, c.POINTER(c.c_size_t)], c.c_int),
            ("es_test_controls_restore", [c.c_void_p, c.c_size_t, c.c_int], c.c_int),
            ("es_test_controls_poison", [], None),
            ("es_test_codec_overflow", [], c.c_int),
        ):
            f = getattr(self.lib, name)
            f.argtypes, f.restype = args, result
        try:
            self.check(self.lib.swmm_open(*(os.fsencode(p) for p in self.paths)))
            self.check(self.lib.swmm_start(1))
        except BaseException:
            self.lib.swmm_close()
            raise

    def check(self, error):
        if error:
            report = self.paths[1].read_text(errors="replace") if self.paths[1].exists() else "No report"
            raise AssertionError((error, report))

    def dump(self):
        size = c.c_size_t()
        self.check(self.lib.es_test_controls_save(None, 0, c.byref(size)))
        data = c.create_string_buffer(size.value)
        self.check(self.lib.es_test_controls_save(data, size.value, c.byref(size)))
        return data.raw

    def restore(self, raw, fail_at=-1):
        data = c.create_string_buffer(raw)
        return self.lib.es_test_controls_restore(data, len(raw), fail_at)

    def step(self):
        elapsed = c.c_double()
        self.check(self.lib.swmm_step(c.byref(elapsed)))
        values = tuple(self.lib.swmm_getValue(code, index)
                       for code, index in ((303, 0), (407, 0), (407, 1), (410, 0), (410, 1)))
        return (elapsed.value, *values)

    def close(self):
        try:
            self.check(self.lib.swmm_end())
            self.check(self.lib.swmm_report())
        finally:
            self.lib.swmm_close()


@unittest.skipUnless(os.environ.get("EASYSEWER_CHECKPOINT_STANDARD") and
                     os.environ.get("EASYSEWER_CHECKPOINT_CUSTOM"),
                     "Requires both internal checkpoint test builds")
class NativeCheckpointControlTests(unittest.TestCase):
    def engines(self, text=None):
        for family in ("standard", "custom"):
            with tempfile.TemporaryDirectory() as root:
                engine = Engine(os.environ["EASYSEWER_CHECKPOINT_" + family.upper()], root,
                                fixture() if text is None else text)
                try:
                    yield engine
                finally:
                    engine.close()

    def test_wire_format_pid_history_all_branches_and_poison_restore(self):
        for e in self.engines():
            initial = e.dump()
            self.assertEqual(initial[:8], b"ESCTRL01")
            self.assertEqual(struct.unpack_from("<IIII", initial, 8), (4, 1, 1, 0))
            for _ in range(31):
                e.step()
            raw = e.dump()
            self.assertNotEqual(raw, initial)
            self.assertEqual(struct.unpack_from("<I", raw, 20)[0], 2)
            e.lib.es_test_controls_poison()
            self.assertNotEqual(e.dump(), raw)
            self.assertEqual(e.restore(raw), 0)
            self.assertEqual(e.dump(), raw)
            self.assertEqual(e.restore(raw), 0)  # repeated restore owns/replaces lists
            self.assertEqual(e.dump(), raw)

    def test_each_truncation_extra_bytes_unknown_schema_rejected_without_mutation(self):
        for e in self.engines():
            for _ in range(15):
                e.step()
            raw = e.dump()
            damaged = [raw[:n] for n in range(len(raw))]
            damaged += [raw + b"\x00", b"ESCTRL02" + raw[8:]]
            for value in damaged:
                self.assertNotEqual(e.restore(value), 0)
                self.assertEqual(e.dump(), raw)
            self.assertEqual(e.restore(raw), 0)

    def test_bad_counts_identity_action_binding_and_nonfinite_rejected(self):
        for e in self.engines():
            e.step()
            raw = e.dump()
            for offset in (8, 12, 16, 20, 56):
                value = bytearray(raw)
                struct.pack_into("<I", value, offset, 0xffffffff)
                self.assertNotEqual(e.restore(bytes(value)), 0)
                self.assertEqual(e.dump(), raw)
            for offset in (24, 32, 40, 48):
                for number in (float("nan"), float("inf"), -float("inf")):
                    value = bytearray(raw)
                    struct.pack_into("<d", value, offset, number)
                    self.assertNotEqual(e.restore(bytes(value)), 0)
                    self.assertEqual(e.dump(), raw)
            value = bytearray(raw)
            value[60] ^= 1  # first rule ID byte
            self.assertNotEqual(e.restore(bytes(value)), 0)
            first_action = 60 + len("PID") + 8 + 4
            for offset in (first_action, first_action+4, first_action+8, first_action+12, first_action+16):
                value = bytearray(raw)
                value[offset] ^= 1
                self.assertNotEqual(e.restore(bytes(value)), 0)
                self.assertEqual(e.dump(), raw)

    def test_staged_allocation_failures_leave_original_state_and_allow_retry(self):
        for e in self.engines():
            for _ in range(20):
                e.step()
            raw = e.dump()
            capacity = struct.unpack_from("<I", raw, 20)[0]
            self.assertEqual(capacity, 2)
            for _ in range(3):
                for fail_at in range(capacity):
                    self.assertEqual(e.restore(raw, fail_at), 6)
                    self.assertEqual(e.dump(), raw)
                self.assertEqual(e.restore(raw), 0)

    def test_empty_rules_and_signed_zero_have_canonical_finite_encoding(self):
        for e in self.engines(fixture(variant="empty")):
            raw = e.dump()
            self.assertEqual(len(raw), 56)
            self.assertEqual(struct.unpack_from("<IIII", raw, 8), (0, 0, 0, 0))
            negative = bytearray(raw)
            struct.pack_into("<d", negative, 24, -0.0)
            self.assertEqual(e.restore(bytes(negative)), 0)
            self.assertEqual(e.dump(), bytes(negative))
            self.assertEqual(e.lib.es_test_codec_overflow(), 2)

    def test_restoring_controls_at_many_boundaries_keeps_full_native_outputs(self):
        for family in ("standard", "custom"):
            for variant in ("mixed", "order"):
                for rule_step in (0, 7, 20):
                    with self.subTest(family=family, variant=variant, rule_step=rule_step):
                        outputs = []
                        # Poison/reconstitute this module in-place; all other
                        # native state remains live. This is NOT full resume.
                        for restore in (False, True):
                            with tempfile.TemporaryDirectory() as root:
                                e = Engine(os.environ["EASYSEWER_CHECKPOINT_" + family.upper()], root,
                                           fixture(rule_step=rule_step, variant=variant))
                                history = []
                                try:
                                    for i in range(10000):
                                        row = e.step()
                                        history.append(row)
                                        if row[0] == 0:
                                            break
                                        if restore and (i < 3 or i % 3 == 0):
                                            raw = e.dump()
                                            e.lib.es_test_controls_poison()
                                            self.assertEqual(e.restore(raw), 0)
                                            self.assertEqual(e.dump(), raw)
                                    else:
                                        self.fail("Fixture did not finish")
                                finally:
                                    e.close()
                                report = e.paths[1].read_bytes()
                                report = re.sub(rb"(?m)^[ \t]*(?:Analysis (?:begun|ended)|Total elapsed time)[^\r\n]*", b"", report)
                                outputs.append((history, e.paths[2].read_bytes(), report))
                        self.assertEqual(outputs[0], outputs[1])


if __name__ == "__main__":
    unittest.main()
