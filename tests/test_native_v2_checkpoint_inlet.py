"""Private inlet owner and quality scratch audit; other owners remain live."""
import ctypes as c
from dataclasses import replace
from datetime import time, timedelta
import os
import re
import struct
import tempfile
import unittest

from easysewer.model import Ref
from easysewer.model.surface import (
    CombinationInlet, CurbInlet, CustomInlet, GenericGrate, GrateInlet,
    SlottedInlet, StandardGrate,
)
from test_native_v2_surface import surface_network
from test_native_v2_checkpoint_network import Engine as NetworkEngine, network, QUALITY, storage_fixture
from test_native_v2_checkpoint_controls import fixture as controls
from test_native_v2_checkpoint_clocks import clocks


def fixture(design=None, *, shape="STREET", placement="ON_GRADE", sides=2,
            routing="DYNWAVE", backflow=False, shared=False, quality=True, ponding=False,
            mixed_sides=False):
    design = design or GrateInlet(kind="GRATE", length=2, width=1,
                                 grate=StandardGrate(kind="P_BAR-50"))
    model = surface_network(design, shape=shape, placement=placement, sides=sides)
    model.update_options(end_time=time(0, 3), report_step=timedelta(seconds=10),
                         flow_routing=routing, allow_ponding=ponding)
    if backflow:
        model.nodes.update("C", max_depth=.05, initial_depth=.05)
        from easysewer.model.geometry import Circular, CrossSection
        model.links.update("D", section=CrossSection(geometry=Circular(diameter=.05)))
    if shared:
        model.nodes.add(replace(model.nodes["O"], id="O2"))
        model.links.add(replace(model.links["P"], id="P2", length=120,
                                outlet=Ref(collection="swmm:nodes", key="O2")))
        model.inlet_usage.add(replace(model.inlet_usage["P"],
                                     link=Ref(collection="swmm:links", key="P2"), count=3))
        if mixed_sides:
            from easysewer.model.geometry import CrossSection, Street
            model.streets.add(replace(model.streets["Road"], id="Road2", sides=1))
            model.links.update("P2", section=CrossSection(geometry=Street(
                street=Ref(collection="swmm:streets", key="Road2"))))
    text = model.to_document().text + "[DWF]\nJ FLOW .5\n"
    if backflow: text += "C FLOW 4\n"
    if quality:
        text += QUALITY + "[TREATMENT]\nJ Mass R = .5 * R_Count\nJ Count R = .2\nC Mass R = .2\nC Count C = Count * (1 - R_Mass)\n"
    return text


def inlet(raw):
    """Independent parser for the inlet chunk; preserve offsets for mutations."""
    pos = raw.index(b"ESINL001"); start = pos; pos += 8
    floats, integers, bindings = [], [], []
    def ints(n=1, mutable=False):
        nonlocal pos
        result = struct.unpack_from("<"+"i"*n, raw, pos)
        (integers if mutable else bindings).extend(range(pos, pos+4*n, 4)); pos += 4*n
        return result
    def doubles(n=1, mutable=False):
        nonlocal pos
        (floats if mutable else bindings).extend(range(pos, pos+8*n, 8)); pos += 8*n
    def identity():
        nonlocal pos
        length = ints()[0]; bindings.append(pos); pos += length
    uses, designs, nodes, links, count = ints(5)
    for _ in range(designs):
        identity(); ints(3); doubles(8); ints()
    for _ in range(count):
        ints(); identity(); ints(); identity(); ints(3)
        doubles(6); doubles(2, True); ints(3, True); doubles(4, True)
    assert pos == len(raw), (pos, len(raw))
    return dict(start=start, count=count, floats=floats, integers=integers, bindings=bindings)


class Engine(NetworkEngine):
    def __init__(self, *args):
        super().__init__(*args)
        for name, args, result in (
            ("es_test_inlet_save", [c.c_void_p, c.c_size_t, c.POINTER(c.c_size_t)], c.c_int),
            ("es_test_inlet_restore", [c.c_void_p, c.c_size_t, c.c_int], c.c_int),
            ("es_test_inlet_bundle_poison", [], None),
            ("es_test_inlet_poison", [c.c_int], None),
            ("es_test_inlet_observe", [c.c_int, c.c_int], c.c_double),
            ("es_test_treatment_poison", [], None),
        ):
            f = getattr(self.lib, name); f.argtypes, f.restype = args, result

    def dump(self):
        size = c.c_size_t()
        self.check(self.lib.es_test_inlet_save(None, 0, c.byref(size)))
        data = c.create_string_buffer(size.value)
        self.check(self.lib.es_test_inlet_save(data, size.value, c.byref(size)))
        return data.raw

    def restore(self, raw, fail_at=-1):
        return self.lib.es_test_inlet_restore(c.create_string_buffer(bytes(raw)), len(raw), fail_at)


@unittest.skipUnless(os.environ.get("EASYSEWER_CHECKPOINT_STANDARD") and
                     os.environ.get("EASYSEWER_CHECKPOINT_CUSTOM"),
                     "Requires instrumented inlet-state libraries")
class NativeCheckpointInletTests(unittest.TestCase):
    def run_case(self, family, text, action=None, mode="step"):
        with tempfile.TemporaryDirectory() as root:
            e = Engine(os.environ["EASYSEWER_CHECKPOINT_"+family.upper()], root, text)
            history, observations = [], []
            try:
                raw = e.dump(); layout = inlet(raw); net = network(raw[:layout["start"]])
                for index in range(10000):
                    raw = e.dump()
                    if action == "restore":
                        e.lib.es_test_inlet_bundle_poison()
                        e.lib.es_test_treatment_poison()
                        self.assertEqual(e.restore(raw), 0)
                        self.assertEqual(e.dump(), raw)
                    elif action == "scratch":
                        e.lib.es_test_inlet_poison(2)
                        e.lib.es_test_treatment_poison()
                    elif action == "omit" and index:
                        e.lib.es_test_inlet_poison(1)
                    if mode == "stride":
                        elapsed = c.c_double(); e.check(e.lib.swmm_stride(13, c.byref(elapsed)))
                        now = elapsed.value
                    elif mode == "split":
                        e.check(e.lib.swmm_execRouting())
                        self.assertEqual(e.restore(raw), 5)
                        e.check(e.lib.swmm_saveResults())
                        frame = clocks(e.dump())
                        now = frame["routing"]/86400000 if frame["routing"] < frame["duration"] else 0
                    else: now = e.step()[0]
                    observations.append(tuple(tuple(e.lib.es_test_inlet_observe(i, k) for k in range(10))
                                              for i in range(layout["count"])))
                    history.append((now, *(e.lib.swmm_getValue(code, i) for i in range(net["nodes"])
                                           for code in (303, 305, 306, 307, 308)),
                                    *(e.lib.swmm_getValue(code, i) for i in range(net["links"])
                                      for code in (407, 410, 411))))
                    if not now:
                        if action == "restore":
                            raw = e.dump(); e.lib.es_test_inlet_bundle_poison()
                            self.assertEqual(e.restore(raw), 0); self.assertEqual(e.dump(), raw)
                        break
                else: self.fail("Inlet fixture did not finish")
            finally: e.close()
            report = re.sub(rb"(?m)^[ \t]*(?:Analysis (?:begun|ended)|Total elapsed time)[^\r\n]*", b"", e.paths[1].read_bytes())
            return (history, e.paths[2].read_bytes(), report), observations

    def test_designs_placements_quality_and_shared_backflow(self):
        grate = GrateInlet(kind="GRATE", length=2, width=1, grate=StandardGrate(kind="P_BAR-50"))
        curb = CurbInlet(kind="CURB", length=4, height=.5, throat="INCLINED")
        designs = [(grate, "STREET"), (replace(grate, grate=GenericGrate(open_fraction=.7, splash_velocity=4)), "STREET"),
                   (curb, "STREET"), (CombinationInlet(grate=grate, curb=curb), "STREET"),
                   (SlottedInlet(length=2, width=.1), "STREET"),
                   (replace(grate, kind="DROP_GRATE"), "RECT_OPEN"),
                   (CurbInlet(kind="DROP_CURB", length=4, height=.5), "TRAPEZOIDAL"),
                   (CustomInlet(curve=Ref(collection="swmm:curves", key="Rating")), "CIRCULAR"),
                   (CustomInlet(curve=Ref(collection="swmm:curves", key="Diversion")), "STREET")]
        cases = [(f"{i}-{placement}", fixture(design, shape=shape, placement=placement, sides=1+i%2))
                 for i, (design, shape) in enumerate(designs)
                 for placement in ("AUTOMATIC", "ON_GRADE", "ON_SAG")]
        cases += [("backflow", fixture(backflow=True, shared=True)),
                  ("mixed-sides", fixture(shared=True, mixed_sides=True, placement="ON_SAG")),
                  ("invalid-placement", re.sub(r"(?m)^P STREET[^\r\n]*", "P CIRCULAR 2 0 0 0 1", fixture())),
                  ("no-quality", fixture(quality=False)),
                  ("kinematic", fixture(routing="KINWAVE", placement="ON_SAG")),
                  ("steady", fixture(routing="STEADY", placement="ON_SAG")),
                  ("events", fixture()+"[EVENTS]\n01/01/2020 00:00:30 01/01/2020 00:01:00\n01/01/2020 00:02:00 01/01/2020 00:02:30\n"),
                  ("storage-treatment", storage_fixture()),
                  ("no-inlets", controls())]
        for family in ("standard", "custom"):
            for name, text in cases:
                with self.subTest(family=family, case=name):
                    expected = self.run_case(family, text)
                    self.assertEqual(self.run_case(family, text, "restore"), expected)
                    if name == "backflow":
                        obs = expected[1]
                        self.assertTrue(any(row[0][0] > 0 for row in obs))
                        self.assertTrue(any(row[0][1] > 0 and row[1][1] > 0 for row in obs))
                        self.assertAlmostEqual(obs[-1][0][9]+obs[-1][1][9], 1)

    def test_scratch_values_are_overwritten_and_statistics_are_necessary(self):
        for family in ("standard", "custom"):
            for text in (fixture(backflow=True, shared=True), storage_fixture()):
                expected = self.run_case(family, text)
                self.assertEqual(self.run_case(family, text, "scratch"), expected)
            expected, _ = self.run_case(family, fixture())
            omitted, _ = self.run_case(family, fixture(), "omit")
            self.assertEqual(omitted[:2], expected[:2])
            self.assertNotEqual(omitted[2], expected[2])

    def test_stride_and_custom_split(self):
        for family, mode in (("standard", "stride"), ("custom", "stride"), ("custom", "split")):
            with self.subTest(family=family, mode=mode):
                text = fixture(backflow=True, shared=True, ponding=mode == "split")
                self.assertEqual(self.run_case(family, text, "restore", mode), self.run_case(family, text, mode=mode))

    def test_bad_final_chunk_cannot_partly_restore_earlier_modules(self):
        for family in ("standard", "custom"):
            with tempfile.TemporaryDirectory() as root:
                e = Engine(os.environ["EASYSEWER_CHECKPOINT_"+family.upper()], root, fixture(shared=True))
                try:
                    for _ in range(6): e.step()
                    raw = e.dump(); layout = inlet(raw)
                    def reject(bad):
                        self.assertNotEqual(e.restore(bad), 0)
                        self.assertEqual(e.dump(), raw)
                    for size in range(layout["start"], len(raw)): reject(raw[:size])
                    reject(raw+b"x")
                    for offset in layout["bindings"]:
                        bad = bytearray(raw); bad[offset] ^= 1; reject(bad)
                    for offset in layout["floats"]:
                        for value in (float("nan"), float("inf"), -float("inf")):
                            bad = bytearray(raw); struct.pack_into("<d", bad, offset, value); reject(bad)
                    for offset in layout["integers"]:
                        for value in (-1, 2147483647):
                            bad = bytearray(raw); struct.pack_into("<i", bad, offset, value); reject(bad)
                    for offset in layout["floats"][2::6]:
                        bad = bytearray(raw); struct.pack_into("<d", bad, offset, -1); reject(bad)
                finally: e.close()
            with tempfile.TemporaryDirectory() as root:
                e = Engine(os.environ["EASYSEWER_CHECKPOINT_"+family.upper()], root, controls())
                try:
                    for _ in range(6): e.step()
                    raw = e.dump(); self.assertEqual(e.restore(raw, 0), 6); self.assertEqual(e.dump(), raw)
                finally: e.close()


if __name__ == "__main__": unittest.main()
