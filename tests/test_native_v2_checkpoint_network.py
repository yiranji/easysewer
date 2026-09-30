"""Network object checkpoint tests. Other process owners remain live."""
import ctypes as c
import os
import re
import struct
import tempfile
import unittest

from test_native_v2_checkpoint_dynwave import Engine as DynamicEngine, fixture as hydraulic
from test_native_v2_checkpoint_clocks import clocks, fixture as hydrology
from test_native_v2_checkpoint_controls import fixture as controls
from test_regulators_v2 import regulator_model
from test_nodes_v2 import SHAPES, CURVE, divider_model


QUALITY = """
[POLLUTANTS]
Mass MG/L 0 0 0 .02 NO * 0 0 5
Count #/L 0 0 0 0 NO * 0 0 7
[INFLOWS]
J Mass "" CONCEN 1 1 5
J Count "" CONCEN 1 1 7
"""


def storage_fixture(shape="FUNCTIONAL", *, seepage="3 4 .3"):
    return f"""[OPTIONS]
FLOW_UNITS CFS
FLOW_ROUTING DYNWAVE
START_DATE 01/01/2020
END_DATE 01/01/2020
END_TIME 00:05:00
REPORT_STEP 00:00:10
ROUTING_STEP 2
VARIABLE_STEP 0
[STORAGE]
J 10 5 2 {'PARABOLIC' if shape == 'PARABOLOID' else shape} {SHAPES[shape]} 0 .7 {seepage}
[OUTFALLS]
O 9 FREE NO
[CONDUITS]
P J O 100 .013 0 0 0 0
[XSECTIONS]
P CIRCULAR .3 0 0 0 2
[LOSSES]
P .1 .2 .3 NO .01
[EVAPORATION]
CONSTANT .2
[INFLOWS]
J FLOW Water FLOW 1 1
[TIMESERIES]
Water 00:00:00 .1
Water 00:02:00 0
Water 00:03:00 .2
Water 00:05:00 0
[REPORT]
NODES ALL
LINKS ALL
[TREATMENT]
J Mass R = 0.0001 * HRT
""" + (CURVE if shape == "TABULAR" else "") + QUALITY


def regulator_fixture(kind):
    model = regulator_model(kind)
    text = model.to_document().text + QUALITY + "\n[REPORT]\nNODES ALL\nLINKS ALL\nCONTROLS YES\n"
    if kind.startswith("PUMP") or kind == "IDEAL":
        word = "PUMP"
    elif kind in ("SIDE", "BOTTOM"):
        word = "ORIFICE"
    elif kind.startswith("FUNCTIONAL"):
        word = "OUTLET"
    else:
        word = "WEIR"
    return text + f"[CONTROLS]\nRULE Change\nIF SIMULATION TIME >= 00:00:40\nTHEN {word} P SETTING = .4\nELSE {word} P SETTING = .9\n"


def return_fixture():
    text = hydrology(events=False) + QUALITY
    # Locate actual subcatchment identity rather than assume its name.
    area = re.search(r"\[SUBCATCHMENTS\]\s*([^;\s]+)", text).group(1)
    text = re.sub(r"(?m)^(O[ \t]+\S+[ \t]+FREE)(?:[ \t]+NO)?[ \t]*$",
                  lambda m: m[1]+" NO "+area, text)
    return text


def network(raw):
    """Independent wire layout and mutable/config offsets for corruption tests."""
    pos = raw.index(b"ESNET001"); start = pos; pos += 8
    floats, integers, bindings, outfalls, full_states = [], [], [], [], []
    def ints(n=1, mutable=False):
        nonlocal pos
        values = struct.unpack_from("<"+"i"*n, raw, pos)
        (integers if mutable else bindings).extend(range(pos, pos+4*n, 4)); pos += 4*n
        return values
    def doubles(n=1, mutable=False):
        nonlocal pos
        values = struct.unpack_from("<"+"d"*n, raw, pos)
        (floats if mutable else bindings).extend(range(pos, pos+8*n, 8)); pos += 8*n
        return values
    def identity():
        nonlocal pos
        size = ints()[0]; name = raw[pos:pos+size].decode()
        bindings.append(pos); pos += size
        return name
    custom, route, nodes, links, pollutants = ints(5)
    for _ in range(pollutants):
        identity(); ints()
        if custom: doubles(mutable=True)
    for _ in range(nodes):
        identity(); kind = ints(5)[0]; doubles(6); ints(mutable=True)
        doubles(14 + 5*custom, mutable=True)
        doubles(2*pollutants, mutable=True)
        if kind == 2:
            ints(2); doubles(4); doubles(3, mutable=True)
            if ints()[0]:
                doubles(4)
                for _ in range(2):
                    doubles(4); doubles(4, mutable=True); ints(mutable=True)
        elif kind == 1:
            outfalls.append(pos)
            ints(mutable=True); doubles(mutable=True); target = ints(4)[-1]
            if target >= 0: doubles(1+pollutants, mutable=True)
    for _ in range(links):
        identity(); kind = ints(6)[0]; doubles(3); shape = ints()[0]
        doubles(2, mutable=shape == 2)
        doubles(13, mutable=True); ints(4, mutable=True); doubles(3*pollutants, mutable=True)
        if kind == 0:
            doubles(2); ints(); doubles(5); ints(2)
            doubles(8, mutable=True); full_states.append(pos+4); ints(2, mutable=True)
        elif kind == 2:
            ints(2); doubles(3); doubles(4, mutable=True)
        elif kind == 3:
            ints(); doubles(2); doubles(2, mutable=True)
    assert pos == len(raw), (pos, len(raw))
    return dict(start=start, custom=custom, nodes=nodes, links=links, pollutants=pollutants,
                floats=floats, integers=integers, bindings=bindings, outfalls=outfalls, full_states=full_states)


class Engine(DynamicEngine):
    def __init__(self, *args):
        super().__init__(*args)
        for name, args, result in (
            ("es_test_network_save", [c.c_void_p, c.c_size_t, c.POINTER(c.c_size_t)], c.c_int),
            ("es_test_network_restore", [c.c_void_p, c.c_size_t, c.c_int], c.c_int),
            ("es_test_network_bundle_poison", [], None),
            ("es_test_network_poison", [c.c_int], None),
            ("es_test_network_observe", [c.c_int, c.c_int], c.c_double),
        ):
            f = getattr(self.lib, name); f.argtypes, f.restype = args, result

    def dump(self):
        size = c.c_size_t()
        self.check(self.lib.es_test_network_save(None, 0, c.byref(size)))
        data = c.create_string_buffer(size.value)
        self.check(self.lib.es_test_network_save(data, size.value, c.byref(size)))
        return data.raw

    def restore(self, raw, fail_at=-1):
        return self.lib.es_test_network_restore(c.create_string_buffer(raw), len(raw), fail_at)


@unittest.skipUnless(os.environ.get("EASYSEWER_CHECKPOINT_STANDARD") and
                     os.environ.get("EASYSEWER_CHECKPOINT_CUSTOM"),
                     "Requires both instrumented network-state libraries")
class NativeCheckpointNetworkTests(unittest.TestCase):
    def run_case(self, family, text, action=None, mode="step", setter=False):
        with tempfile.TemporaryDirectory() as root:
            e = Engine(os.environ["EASYSEWER_CHECKPOINT_"+family.upper()], root, text)
            history, observations = [], []
            try:
                layout = network(e.dump())
                for index in range(10000):
                    if setter and index == 3:
                        e.lib.swmm_setValue(306, 0, .17)
                        # Outfall is last in the two-junction hydraulic fixture.
                        e.lib.swmm_setValue(304, layout["nodes"]-1, 10.7)
                    raw = e.dump()
                    if action == "restore":
                        e.lib.es_test_network_bundle_poison()
                        self.assertEqual(e.restore(raw), 0)
                        self.assertEqual(e.dump(), raw)
                    elif isinstance(action, int) and index:
                        e.lib.es_test_network_poison(action)
                    if mode == "stride":
                        elapsed = c.c_double(); e.check(e.lib.swmm_stride(13, c.byref(elapsed)))
                        now = elapsed.value
                    elif mode in ("split", "remove"):
                        e.check(e.lib.swmm_execRouting())
                        # No capture may see this half-finished custom step.
                        self.assertEqual(e.restore(raw), 5)
                        if mode == "remove":
                            e.lib.swmm_getIndex.argtypes = [c.c_int, c.c_char_p]
                            node = e.lib.swmm_getIndex(2, b"J")
                            self.assertGreaterEqual(node, 0)
                            e.lib.swmm_getPondingStep.restype = c.c_double
                            dt = e.lib.swmm_getPondingStep()
                            overflow = e.lib.swmm_getValue(308, node)
                            volume = e.lib.swmm_getValue(305, node)
                            depth = e.lib.swmm_getValue(310, node)
                            removed_flow = overflow * .5
                            kept = volume - removed_flow * dt
                            e.lib.swmm_setValue(311, node, removed_flow)
                            if removed_flow > 0:
                                for code, value in ((310, depth-removed_flow*dt/100),
                                                    (308, overflow-removed_flow), (305, kept)):
                                    e.lib.swmm_setValue(code, node, value)
                        e.check(e.lib.swmm_saveResults())
                        frame = clocks(e.dump())
                        now = frame["routing"]/86400000 if frame["routing"] < frame["duration"] else 0
                    else:
                        now = e.step()[0]
                    observation = tuple(e.lib.es_test_network_observe(group, 0) for group in range(6))
                    if mode == "remove":
                        observation += tuple(e.lib.swmm_getValue(501, p) for p in range(layout["pollutants"]))
                    observations.append(observation)
                    history.append((now, *(e.lib.swmm_getValue(code, n) for n in range(layout["nodes"])
                                           for code in (303, 305, 306, 307, 308)),
                                    *(e.lib.swmm_getValue(code, n) for n in range(layout["links"])
                                      for code in (407, 410, 411))))
                    if now == 0:
                        raw = e.dump()
                        if action == "restore":
                            e.lib.es_test_network_bundle_poison()
                            self.assertEqual(e.restore(raw), 0)
                            self.assertEqual(e.dump(), raw)
                        break
                else:
                    self.fail("Network fixture did not finish")
            finally:
                e.close()
            report = re.sub(rb"(?m)^[ \t]*(?:Analysis (?:begun|ended)|Total elapsed time)[^\r\n]*", b"", e.paths[1].read_bytes())
            return (history, e.paths[2].read_bytes(), report), observations

    def test_network_and_subtypes_preserve_full_solution(self):
        cases = [("dynamic", hydraulic()+QUALITY), ("kinematic", hydraulic(routing="KINWAVE")+QUALITY),
                 ("filled-circle", hydraulic().replace("CIRCULAR 1 0 0 0 1", "FILLED_CIRCULAR 1.3 .1 0 0 1")+QUALITY),
                 ("steady", hydraulic(routing="STEADY")+QUALITY), ("slot", hydraulic(surcharge="SLOT")+QUALITY),
                 ("inactive", hydrology(ignore_routing=True)), ("return", return_fixture()),
                 ("controls", controls()+QUALITY)]
        cases += [
            ("tidal", hydraulic().replace("O 9.8 FREE NO", "O 9.8 TIDAL Tide NO")+
             "[CURVES]\nTide TIDAL 0 9.9\nTide 24 10.7\n"),
            ("stage-series", hydraulic().replace("O 9.8 FREE NO", "O 9.8 TIMESERIES Stage NO")+
             "[TIMESERIES]\nStage 00:00:00 9.9\nStage 00:04:00 10.7\n"),
            ("constant-exfil", storage_fixture(seepage="4")),
            ("no-exfil", storage_fixture(seepage="0")),
        ]
        cases += [("storage-"+shape, storage_fixture(shape)) for shape in SHAPES]
        cases += [(kind, regulator_fixture(kind)) for kind in
                  ("SIDE", "BOTTOM", "TRANSVERSE", "V-NOTCH", "ROADWAY", "PUMP1", "PUMP3", "IDEAL", "FUNCTIONAL/DEPTH")]
        cases += [("divider-"+kind, divider_model(kind).to_document().text+QUALITY)
                  for kind in ("OVERFLOW", "CUTOFF", "TABULAR", "WEIR")]
        for family in ("standard", "custom"):
            for name, text in cases:
                with self.subTest(family=family, case=name):
                    expected, observations = self.run_case(family, text)
                    actual, restored_observations = self.run_case(family, text, "restore")
                    self.assertEqual(expected, actual)
                    self.assertEqual(observations, restored_observations)
                    if name.startswith("storage-"):
                        self.assertGreater(max(row[0] for row in observations), 0)
                        self.assertGreater(max(row[1] for row in observations), 0)
                        if name != "storage-CYLINDRICAL":
                            self.assertGreater(max(row[2] for row in observations), 0)
                        self.assertGreater(max(row[4] for row in observations), 0)
                    if name == "return":
                        self.assertGreater(max(row[3] for row in observations), 0)

    def test_stride_split_and_live_inflow_outfall_setters(self):
        for family, mode in (("standard", "step"), ("custom", "step"),
                             ("standard", "stride"), ("custom", "stride"), ("custom", "split")):
            with self.subTest(family=family, mode=mode):
                text = hydraulic(ponding=mode == "split") + QUALITY
                expected, _ = self.run_case(family, text, mode=mode, setter=True)
                actual, _ = self.run_case(family, text, "restore", mode=mode, setter=True)
                self.assertEqual(expected, actual)

    def test_custom_discrete_removal_retains_native_volume_and_pollutant_ledgers(self):
        from test_native_v2_ponding_accounting import sealed_model
        text = sealed_model(with_pump=True).to_document().text
        expected, observations = self.run_case("custom", text, mode="remove")
        actual, restored = self.run_case("custom", text, "restore", mode="remove")
        self.assertEqual(expected, actual)
        self.assertEqual(observations, restored)
        self.assertTrue(all(v > 0 for v in observations[-1][-3:]))

    def test_late_corruption_and_staged_allocation_failure_do_not_commit(self):
        for family in ("standard", "custom"):
            with tempfile.TemporaryDirectory() as root:
                e = Engine(os.environ["EASYSEWER_CHECKPOINT_"+family.upper()], root, storage_fixture()+
                           "[CONTROLS]\nRULE Hold\nIF SIMULATION TIME > 00:00:01\nTHEN CONDUIT P STATUS = OPEN\n")
                try:
                    for _ in range(10): e.step()
                    raw = e.dump(); layout = network(raw)
                    damaged = [raw[:n] for n in range(layout["start"], len(raw))] + [raw+b"x"]
                    for offset in layout["bindings"]:
                        value = bytearray(raw); value[offset] ^= 1; damaged.append(bytes(value))
                    for offset in layout["floats"]:
                        for number in (float("nan"), float("inf"), -float("inf")):
                            value = bytearray(raw); struct.pack_into("<d", value, offset, number); damaged.append(bytes(value))
                    for offset in layout["integers"]:
                        value = bytearray(raw); struct.pack_into("<i", value, offset, 999); damaged.append(bytes(value))
                    # In-range enums still need the corresponding resources or
                    # a real full-state enumerator, not just a min/max check.
                    for offset, values in [(o, (3, 4)) for o in layout["outfalls"]] + [
                            (o, range(1, 8)) for o in layout["full_states"]]:
                        for number in values:
                            value = bytearray(raw); struct.pack_into("<i", value, offset, number); damaged.append(bytes(value))
                    for value in damaged:
                        self.assertNotEqual(e.restore(value), 0)
                        self.assertEqual(e.dump(), raw)
                    self.assertEqual(e.restore(raw, 0), 6)
                    self.assertEqual(e.dump(), raw)
                finally:
                    e.close()

    def test_omitted_network_and_exfiltration_state_changes_solution(self):
        for family in ("standard", "custom"):
            for group, text in ((1, hydraulic()), (2, storage_fixture()), (4, storage_fixture()),
                                (6, hydraulic()), (7, hydraulic()), (8, regulator_fixture("SIDE"))):
                with self.subTest(family=family, group=group):
                    expected, _ = self.run_case(family, text)
                    actual, _ = self.run_case(family, text, group)
                    self.assertNotEqual(expected, actual)


if __name__ == "__main__":
    unittest.main()
