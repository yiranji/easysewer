"""Dynamic-wave owner qualification; Node/Link and other solver state stays live."""
import ctypes as c
import os
import re
import struct
import tempfile
import unittest

from test_native_v2_checkpoint_clocks import Engine as ClockEngine, clocks, fixture as clock_fixture


def fixture(*, surcharge="EXTRAN", ponding=False, routing="DYNWAVE", fixed=False):
    return f"""[OPTIONS]
FLOW_UNITS CFS
FLOW_ROUTING {routing}
START_DATE 01/01/2020
END_DATE 01/01/2020
END_TIME 00:04:00
REPORT_STEP 00:00:30
ROUTING_STEP 10
VARIABLE_STEP {0 if fixed else .75}
MINIMUM_STEP .1
SURCHARGE_METHOD {surcharge}
ALLOW_PONDING {'YES' if ponding else 'NO'}
THREADS 1
[JUNCTIONS]
J 10 2 0 2 {100 if ponding else 0}
K 9.9 2 0 2 {100 if ponding else 0}
[OUTFALLS]
O 9.8 FREE NO
[CONDUITS]
P J K 50 .013 0 0 0 0
Q K O 50 .013 0 0 0 0
[XSECTIONS]
P CIRCULAR 1 0 0 0 1
Q CIRCULAR 1 0 0 0 1
[INFLOWS]
J FLOW Pulse FLOW 1 1
[TIMESERIES]
Pulse 00:00:00 0
Pulse 00:00:30 4
Pulse 00:01:00 8
Pulse 00:02:00 0
Pulse 00:04:00 0
[REPORT]
NODES ALL
LINKS ALL
"""


def dynamic(raw):
    """Wire reader independent of the C implementation."""
    start = raw.index(b"ESDYNW01", clocks(raw)["controls_offset"])
    active, route_model = struct.unpack_from("<ii", raw, start+8)
    result = dict(start=start, active=active, route_model=route_model, nodes=[], offsets=[])
    if not active:
        assert start+16 == len(raw)
        return result
    count, surcharge, trials = struct.unpack_from("<3i", raw, start+16)
    minimum, area, tolerance, crown, step = struct.unpack_from("<5d", raw, start+28)
    result.update(count=count, surcharge=surcharge, trials=trials, step=step,
                  step_offset=start+60)
    pos = start+68
    for _ in range(count):
        length, = struct.unpack_from("<I", raw, pos)
        pos += 4
        name = raw[pos:pos+length].decode(); pos += length
        kind, = struct.unpack_from("<i", raw, pos)
        elevation, old_area, rate = struct.unpack_from("<3d", raw, pos+4)
        result["nodes"].append((name, kind, elevation, old_area, rate))
        result["offsets"].append(dict(identity=pos-length, kind=pos, crown=pos+4,
                                      old_area=pos+12, rate=pos+20))
        pos += 28
    assert pos == len(raw)
    return result


class Engine(ClockEngine):
    def __init__(self, *args):
        super().__init__(*args)
        for name, args, result in (
            ("es_test_hydraulics_save", [c.c_void_p, c.c_size_t, c.POINTER(c.c_size_t)], c.c_int),
            ("es_test_hydraulics_restore", [c.c_void_p, c.c_size_t, c.c_int], c.c_int),
            ("es_test_hydraulics_poison", [], None),
            ("es_test_dynwave_poison", [c.c_int], None),
            ("es_test_dynwave_branches", [], c.c_int),
        ):
            f = getattr(self.lib, name)
            f.argtypes, f.restype = args, result

    def dump(self):
        size = c.c_size_t()
        self.check(self.lib.es_test_hydraulics_save(None, 0, c.byref(size)))
        data = c.create_string_buffer(size.value)
        self.check(self.lib.es_test_hydraulics_save(data, size.value, c.byref(size)))
        return data.raw

    def restore(self, raw, fail_at=-1):
        return self.lib.es_test_hydraulics_restore(c.create_string_buffer(raw), len(raw), fail_at)


@unittest.skipUnless(os.environ.get("EASYSEWER_CHECKPOINT_STANDARD") and
                     os.environ.get("EASYSEWER_CHECKPOINT_CUSTOM"),
                     "Requires both instrumented dynamic-wave builds")
class NativeCheckpointDynamicWaveTests(unittest.TestCase):
    def engines(self, text=None):
        for family in ("standard", "custom"):
            with tempfile.TemporaryDirectory() as root:
                e = Engine(os.environ["EASYSEWER_CHECKPOINT_"+family.upper()], root,
                           fixture() if text is None else text)
                try:
                    yield e
                finally:
                    e.close()

    def run_case(self, family, text, action=None, mode="step"):
        with tempfile.TemporaryDirectory() as root:
            e = Engine(os.environ["EASYSEWER_CHECKPOINT_"+family.upper()], root, text)
            trace, states, flags = [], [], 0
            try:
                initial = e.dump()
                self.assertEqual(e.restore(initial), 0)
                for index in range(10000):
                    if mode == "stride":
                        elapsed = c.c_double()
                        e.check(e.lib.swmm_stride(17, c.byref(elapsed)))
                        now = elapsed.value
                    elif mode == "split":
                        e.check(e.lib.swmm_execRouting()); e.check(e.lib.swmm_saveResults())
                        state = clocks(e.dump())
                        now = state["routing"]/86400000 if state["routing"] < state["duration"] else 0
                    else:
                        now = e.step()[0]
                    raw = e.dump()
                    states.append(dynamic(raw))
                    flags |= e.lib.es_test_dynwave_branches()
                    trace.append((now, *(e.lib.swmm_getValue(code, i)
                                         for code, i in ((303, 0), (303, 1), (410, 0), (410, 1)))))
                    if action == "restore":
                        e.lib.es_test_hydraulics_poison()
                        self.assertEqual(e.restore(raw), 0)
                        self.assertEqual(e.dump(), raw)
                    elif action == "scratch":
                        e.lib.es_test_dynwave_poison(4)
                    elif isinstance(action, int):
                        e.lib.es_test_dynwave_poison(action)
                    if now == 0:
                        break
                else:
                    self.fail("Hydraulic fixture did not finish")
            finally:
                e.close()
            report = re.sub(rb"(?m)^[ \t]*(?:Analysis (?:begun|ended)|Total elapsed time)[^\r\n]*", b"", e.paths[1].read_bytes())
            return (trace, e.paths[2].read_bytes(), report), states, flags

    def test_every_step_restore_matches_full_output_and_scratch_is_discardable(self):
        cases = [fixture(), fixture(surcharge="SLOT"), fixture(ponding=True),
                 fixture(fixed=True), clock_fixture(events=True),
                 fixture(routing="KINWAVE"), fixture(routing="STEADY"),
                 clock_fixture(ignore_routing=True)]
        for family in ("standard", "custom"):
            for number, text in enumerate(cases):
                with self.subTest(family=family, case=number):
                    baseline, states, flags = self.run_case(family, text)
                    restored, _, _ = self.run_case(family, text, "restore")
                    scratch, _, _ = self.run_case(family, text, "scratch")
                    self.assertEqual(baseline, restored)
                    self.assertEqual(baseline, scratch)
                    if number == 0:
                        self.assertEqual(flags, 3, "Must exercise surcharge and node-limited steps")
                        self.assertGreater(len({s["step"] for s in states}), 3)
                        self.assertTrue(any(n[3] > 0 for s in states for n in s["nodes"]))
                        self.assertTrue(any(n[4] > 0 for s in states for n in s["nodes"]))
                    if number >= 5:
                        self.assertTrue(all(s["active"] == 0 for s in states))

    def test_stride_and_custom_split_step_restore(self):
        for family, mode in (("standard", "stride"), ("custom", "stride"), ("custom", "split")):
            with self.subTest(family=family, mode=mode):
                text = fixture(ponding=mode == "split")
                expected, _, _ = self.run_case(family, text, mode=mode)
                actual, _, _ = self.run_case(family, text, "restore", mode=mode)
                self.assertEqual(expected, actual)

    def test_missing_each_persistent_field_changes_following_solution(self):
        for family in ("standard", "custom"):
            baseline, _, _ = self.run_case(family, fixture())
            for field in (1, 2, 3):
                with self.subTest(family=family, field=field):
                    damaged, _, _ = self.run_case(family, fixture(), field)
                    self.assertNotEqual(baseline[0], damaged[0], "Fixture must detect omitted state")

    def test_truncated_mismatched_and_invalid_late_block_is_atomic(self):
        for e in self.engines(clock_fixture()):
            for _ in range(25):
                e.step()
            raw = e.dump(); state = dynamic(raw); start = state["start"]
            damaged = [raw[:n] for n in range(len(raw))] + [raw+b"\x00"]
            for offset in (start, start+8, start+12, start+16, start+20, start+24,
                           start+28, start+36, start+44, start+52):
                value = bytearray(raw); value[offset] ^= 1; damaged.append(bytes(value))
            for node in state["offsets"]:
                for key in ("identity", "kind", "crown"):
                    value = bytearray(raw); value[node[key]] ^= 1; damaged.append(bytes(value))
            floats = [state["step_offset"]] + [n[k] for n in state["offsets"] for k in ("old_area", "rate")]
            for offset in floats:
                for number in (-1.0, float("nan"), float("inf"), -float("inf")):
                    value = bytearray(raw); struct.pack_into("<d", value, offset, number); damaged.append(bytes(value))
            for value in damaged:
                self.assertNotEqual(e.restore(value), 0)
                self.assertEqual(e.dump(), raw)
            self.assertEqual(e.restore(raw, 0), 6)
            self.assertEqual(e.dump(), raw)
            e.lib.es_test_hydraulics_poison()
            self.assertEqual(e.restore(raw), 0)
            self.assertEqual(e.dump(), raw)

    def test_initial_zero_live_step_change_and_closed_boundary(self):
        for e in self.engines():
            raw = e.dump()
            self.assertEqual(dynamic(raw)["step"], 0)
            self.assertTrue(all(n[3:] == (0.0, 0.0) for n in dynamic(raw)["nodes"]))
            e.step(); e.step()
            raw = e.dump()
            e.lib.swmm_setValue(3, 0, .01)
            smaller_step = e.dump()
            self.assertGreater(dynamic(smaller_step)["step"], .01)
            self.assertEqual(e.restore(smaller_step), 0)
            self.assertEqual(e.restore(raw), 0)
            e.close()
            size = c.c_size_t(999)
            self.assertEqual(e.lib.es_test_hydraulics_save(None, 0, c.byref(size)), 5)
            self.assertEqual(size.value, 0)
            self.assertEqual(e.restore(raw), 5)

    def test_inactive_routing_after_prior_project_has_valid_interpolation_clock(self):
        for family in ("standard", "custom"):
            self.run_case(family, fixture(routing="KINWAVE").replace("ROUTING_STEP 10", "ROUTING_STEP 30"))
            with tempfile.TemporaryDirectory() as root:
                e = Engine(os.environ["EASYSEWER_CHECKPOINT_"+family.upper()], root,
                           clock_fixture(ignore_routing=True))
                try:
                    self.assertEqual(clocks(e.dump())["old_routing"], 0)
                    for _ in range(1000):
                        now = e.step()[0]
                        state = clocks(e.dump())
                        self.assertLess(state["old_routing"], state["routing"])
                        if not now:
                            break
                    else:
                        self.fail("Inactive routing fixture did not finish")
                finally:
                    e.close()


if __name__ == "__main__":
    unittest.main()
