"""Infiltration/surface state restoration; rain, snow, LID and GW owners stay live."""
import ctypes as c
from dataclasses import replace
from datetime import date, time, timedelta
import os
import re
import struct
import tempfile
import unittest

from easysewer.model.resources import SeriesPoint, Pattern
from test_quality_v2 import quality_model, ref
from test_hydrology_v2 import hydrology_model, snowpack, INFILTRATION
from test_native_v2_checkpoint_inlet import Engine as InletEngine
from test_native_v2_checkpoint_controls import fixture as controls
from test_native_v2_checkpoint_network import network, storage_fixture
from test_native_v2_checkpoint_clocks import clocks


def fixture(method="HORTON", *, mixed=False, route="OUTLET", units="CFS",
            build="POW", wash="EXP", lid=None, groundwater=False, snow=False,
            ignore=False, no_infil=False):
    if groundwater:
        from test_groundwater_v2 import groundwater_model
        model = groundwater_model()
    else: model = quality_model(build, wash)
    model.update_options(start_date=date(2020, 1, 31), start_time=time(23, 30),
                         end_date=date(2020, 2, 1), end_time=time(0, 30),
                         report_start_date=date(2020, 1, 31), report_start_time=time(23, 30),
                         routing_step=timedelta(seconds=30), wet_step=timedelta(seconds=60),
                         dry_step=timedelta(seconds=300), report_step=timedelta(seconds=60),
                         allow_ponding=True, ignore_quality=ignore)
    model.raingages.update("R", interval=timedelta(seconds=60))
    model.timeseries.update("Rain", points=tuple(SeriesPoint(time=timedelta(seconds=s), value=v)
        for s, v in ((0, 6), (300, 0), (1200, .4), (1500, 0), (2400, 4), (2700, 0), (3600, 0))))
    model.landuses.update("Land", sweep_interval=.005, days_since_sweeping=.004)
    model.patterns.add(Pattern(id="Monthly", kind="MONTHLY", factors=(.5, 2)))
    row = model.subcatchments["S"]
    model.subcatchments.update("S", infiltration=hydrology_model(method).subcatchments["S"].infiltration,
        subareas=replace(row.subareas, route_to=route, routed_percent=70 if route != "OUTLET" else None))
    if no_infil:
        model.subcatchments.update("S", impervious_percent=100, infiltration=None)
    if mixed:
        for i, name in enumerate(INFILTRATION):
            identifier = "A"+str(i)
            model.subcatchments.add(replace(model.subcatchments["S"], id=identifier,
                outlet=ref("subcatchments", "S"),
                infiltration=hydrology_model(name).subcatchments["S"].infiltration))
            model.coverages.add(replace(model.coverages[("S", "Land")], subcatchment=ref("subcatchments", identifier)))
    if lid:
        from test_lid_v2 import control, usage
        model.lid_controls.add(control(lid))
        model.lid_usage.add(usage(to_pervious=True))
    if snow:
        model.snowpacks.add(snowpack())
        model.subcatchments.update("S", snowpack=ref("snowpacks", "Snow"))
    if units != "CFS": model.convert_units(units)
    return model.to_document().text + (
        "[ADJUSTMENTS]\nCONDUCTIVITY .5 2 1 1 1 1 1 1 1 1 1 1\n"
        "N-PERV S Monthly\nDSTORE S Monthly\nINFIL S Monthly\n"
        "[EVAPORATION]\nCONSTANT .2\n[REPORT]\nSUBCATCHMENTS ALL\nNODES ALL\nLINKS ALL\n")


def catchment(raw):
    pos = raw.index(b"ESINF001"); start = pos; pos += 8
    floats, flags, bindings = [], [], []
    def ints(n=1, mutable=False):
        nonlocal pos
        result = struct.unpack_from("<"+"i"*n, raw, pos)
        (flags if mutable else bindings).extend(range(pos, pos+4*n, 4)); pos += 4*n
        return result
    def doubles(n=1, mutable=False):
        nonlocal pos
        (floats if mutable else bindings).extend(range(pos, pos+8*n, 8)); pos += 8*n
    def identity():
        nonlocal pos
        length = ints()[0]; bindings.append(pos); pos += length
    count = ints()[0]
    for _ in range(count):
        identity(); index, method = ints(2)
        if index < 0: continue
        if method in (0, 1): doubles(5); doubles(2, True)
        elif method in (2, 3): doubles(4); doubles(4, True); ints(mutable=True)
        else: assert method == 4; doubles(3); doubles(6, True)
    sub_start = pos; assert raw[pos:pos+8] == b"ESSUB001"; pos += 8
    areas, pollutants, lands = ints(3); assert areas == count
    for _ in range(pollutants): identity(); ints()
    for _ in range(lands): identity()
    for _ in range(areas):
        identity(); ints(13); doubles(6)
        for _ in range(3): ints(); doubles(5); doubles(3, True)
        doubles(8, True); doubles(pollutants); doubles(4*pollutants, True)
        for _ in range(lands): doubles(); doubles(1+pollutants, True)
    assert pos == len(raw), (pos, len(raw))
    return dict(start=start, sub_start=sub_start, areas=areas, pollutants=pollutants,
                lands=lands, floats=floats, flags=flags, bindings=bindings)


class Engine(InletEngine):
    def __init__(self, *args):
        super().__init__(*args)
        for name, args, result in (
            ("es_test_catchment_save", [c.c_void_p, c.c_size_t, c.POINTER(c.c_size_t)], c.c_int),
            ("es_test_catchment_restore", [c.c_void_p, c.c_size_t, c.c_int], c.c_int),
            ("es_test_catchment_bundle_poison", [], None),
            ("es_test_infiltration_poison", [c.c_int], None),
            ("es_test_subcatchment_poison", [c.c_int], None),
            ("es_test_subcatchment_observe", [c.c_int]*3, c.c_double),
            ("es_test_infiltration_observe", [c.c_int]*2, c.c_double),
        ):
            f = getattr(self.lib, name); f.argtypes, f.restype = args, result

    def dump(self):
        size = c.c_size_t(); self.check(self.lib.es_test_catchment_save(None, 0, c.byref(size)))
        data = c.create_string_buffer(size.value)
        self.check(self.lib.es_test_catchment_save(data, size.value, c.byref(size)))
        return data.raw

    def restore(self, raw, fail_at=-1):
        return self.lib.es_test_catchment_restore(c.create_string_buffer(bytes(raw)), len(raw), fail_at)


@unittest.skipUnless(os.environ.get("EASYSEWER_CHECKPOINT_STANDARD") and
                     os.environ.get("EASYSEWER_CHECKPOINT_CUSTOM"),
                     "Requires instrumented catchment-state libraries")
class NativeCheckpointCatchmentTests(unittest.TestCase):
    def run_case(self, family, text, action=None, mode="step"):
        with tempfile.TemporaryDirectory() as root:
            e = Engine(os.environ["EASYSEWER_CHECKPOINT_"+family.upper()], root, text)
            history, observations = [], []
            try:
                raw = e.dump(); layout = catchment(raw); net = network(raw[:raw.index(b"ESINL001")])
                for index in range(10000):
                    raw = e.dump()
                    if action == "restore":
                        e.lib.es_test_catchment_bundle_poison()
                        self.assertEqual(e.restore(raw), 0); self.assertEqual(e.dump(), raw)
                    elif action == "scratch":
                        e.lib.es_test_infiltration_poison(1); e.lib.es_test_subcatchment_poison(4)
                    elif action == "infiltration" and index: e.lib.es_test_infiltration_poison(0)
                    elif isinstance(action, int) and index: e.lib.es_test_subcatchment_poison(action)
                    if mode == "stride":
                        elapsed = c.c_double(); e.check(e.lib.swmm_stride(47, c.byref(elapsed))); now = elapsed.value
                    elif mode == "split":
                        e.check(e.lib.swmm_execRouting()); self.assertEqual(e.restore(raw), 5)
                        e.check(e.lib.swmm_saveResults()); frame = clocks(e.dump())
                        now = frame["routing"]/86400000 if frame["routing"] < frame["duration"] else 0
                    else: now = e.step()[0]
                    observations.append(tuple((
                        *(e.lib.es_test_infiltration_observe(j, k) for k in range(6)),
                        *(e.lib.es_test_subcatchment_observe(j, 0, k) for k in range(3)),
                        e.lib.es_test_subcatchment_observe(j, 1, 0),
                        *(e.lib.es_test_subcatchment_observe(j, 2, k) for k in range(layout["pollutants"])),
                        *(e.lib.es_test_subcatchment_observe(j, 3, k) for k in range(layout["lands"])),
                        *(e.lib.es_test_subcatchment_observe(j, 4, k) for k in range(layout["lands"])),
                    ) for j in range(layout["areas"])))
                    history.append((now, *(e.lib.swmm_getValue(code, i) for i in range(net["nodes"])
                                          for code in (303, 305, 306, 307, 308)),
                                    *(e.lib.swmm_getValue(code, i) for i in range(net["links"])
                                      for code in (407, 410, 411))))
                    if not now:
                        if action == "restore":
                            raw = e.dump(); e.lib.es_test_catchment_bundle_poison()
                            self.assertEqual(e.restore(raw), 0); self.assertEqual(e.dump(), raw)
                        break
                else: self.fail("Catchment fixture did not finish")
            finally: e.close()
            report = re.sub(rb"(?m)^[ \t]*(?:Analysis (?:begun|ended)|Total elapsed time)[^\r\n]*", b"", e.paths[1].read_bytes())
            return (history, e.paths[2].read_bytes(), report), observations

    def test_infiltration_variants_surface_quality_and_combined_processes(self):
        cases = [(method, fixture(method)) for method in INFILTRATION]
        cases += [(route, fixture(route=route)) for route in ("PERVIOUS", "IMPERVIOUS")]
        cases += [("mixed", fixture(mixed=True)), ("si", fixture("GREEN_AMPT", units="CMS")),
                  ("ignore-quality", fixture(ignore=True)), ("no-infil", fixture(no_infil=True)),
                  ("groundwater", fixture(groundwater=True)), ("snow", fixture(snow=True)),
                  ("no-subcatchments", controls()), ("storage-exfil", storage_fixture())]
        cases += [("lid-"+kind, fixture(lid=kind)) for kind in ("BC", "RG", "GR", "IT", "PP", "RB", "RD", "VS")]
        cases += [("quality-"+build, fixture(build=build, wash=wash)) for build, wash in
                  (("NONE", "EMC"), ("EXP", "RC"), ("SAT", "EXP"), ("EXT", "EMC"))]
        for family in ("standard", "custom"):
            for name, text in cases:
                with self.subTest(family=family, case=name):
                    expected = self.run_case(family, text)
                    self.assertEqual(self.run_case(family, text, "restore"), expected)
                    if name in INFILTRATION:
                        self.assertGreater(max(row[0][9] for row in expected[1]), 0)
                        self.assertGreater(max(row[0][10] for row in expected[1]), 0)

    def test_scratch_and_omission_are_causally_detected(self):
        for family in ("standard", "custom"):
            for method in INFILTRATION:
                text = fixture(method); expected = self.run_case(family, text)
                self.assertEqual(self.run_case(family, text, "scratch"), expected)
                self.assertNotEqual(self.run_case(family, text, "infiltration")[0], expected[0])
            for text in (fixture(lid="BC", groundwater=True), storage_fixture()):
                self.assertEqual(self.run_case(family, text, "scratch"), self.run_case(family, text))
            text = fixture(mixed=True); expected = self.run_case(family, text)
            for group in (1, 2, 3, 5):
                self.assertNotEqual(self.run_case(family, text, group)[0], expected[0])

    def test_stride_and_custom_split(self):
        for family, mode in (("standard", "stride"), ("custom", "stride"), ("custom", "split")):
            text = fixture(mixed=True)
            with self.subTest(family=family, mode=mode):
                self.assertEqual(self.run_case(family, text, "restore", mode), self.run_case(family, text, mode=mode))

    def test_late_invalid_blocks_and_allocation_failure_are_atomic(self):
        for family in ("standard", "custom"):
            with tempfile.TemporaryDirectory() as root:
                e = Engine(os.environ["EASYSEWER_CHECKPOINT_"+family.upper()], root, fixture(mixed=True))
                try:
                    for _ in range(6): e.step()
                    raw = e.dump(); layout = catchment(raw)
                    def reject(bad):
                        self.assertNotEqual(e.restore(bad), 0); self.assertEqual(e.dump(), raw)
                    for size in range(layout["start"], len(raw)): reject(raw[:size])
                    reject(raw+b"x")
                    for offset in layout["bindings"]:
                        bad = bytearray(raw); bad[offset] ^= 1; reject(bad)
                    for offset in layout["floats"]:
                        for value in (float("nan"), float("inf"), -float("inf")):
                            bad = bytearray(raw); struct.pack_into("<d", bad, offset, value); reject(bad)
                    for offset in layout["flags"]:
                        for value in (-1, 2):
                            bad = bytearray(raw); struct.pack_into("<i", bad, offset, value); reject(bad)
                finally: e.close()
            with tempfile.TemporaryDirectory() as root:
                e = Engine(os.environ["EASYSEWER_CHECKPOINT_"+family.upper()], root, controls())
                try:
                    for _ in range(6): e.step()
                    raw = e.dump(); self.assertEqual(e.restore(raw, 0), 6); self.assertEqual(e.dump(), raw)
                finally: e.close()


if __name__ == "__main__": unittest.main()
