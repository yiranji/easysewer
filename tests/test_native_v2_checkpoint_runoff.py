"""Runoff scheduling/replay owner and independent ODE scratch qualification."""
import ctypes as c
from dataclasses import replace
from datetime import timedelta
import os
from pathlib import Path
import re
import struct
import tempfile
import unittest

from easysewer.io.inp import InpDocument
from easysewer.model import Model
from test_native_v2_checkpoint_gage import Engine as GageEngine, fixture as rain_fixture
from test_native_v2_checkpoint_catchment import fixture as catchment_fixture
from test_native_v2_checkpoint_controls import fixture as controls
from test_native_v2_checkpoint_clocks import clocks
from test_native_v2_checkpoint_network import network


def fixture(root, *, dry=False, cold=False, save=False, lid=None, groundwater=False,
            snow=False, units="CFS", zero_area=False, ignore_routing=False):
    model = Model.from_document(InpDocument.from_text(catchment_fixture(lid=lid, groundwater=groundwater,
                                                                       snow=snow, units=units)), strict=True)
    if dry:
        model.timeseries.update("Rain", points=tuple(replace(p, value=0) for p in model.timeseries["Rain"].points))
    if zero_area: model.subcatchments.update("S", area=0)
    if ignore_routing: model.update_options(ignore_routing=True)
    text = model.to_document().text
    if cold: text += "[TEMPERATURE]\nTIMESERIES Cold\n[TIMESERIES]\nCold 0 20 1 20\n"
    if save: text += f'[FILES]\nSAVE RUNOFF "{Path(root)/"saved-runoff.bin"}"\n'
    return text


def runoff(raw):
    start = raw.index(b"ESRUN001"); pos = start+8
    bindings, flags, floats = [], [], []
    def ints(n=1, mutable=False):
        nonlocal pos
        values = struct.unpack_from("<"+"i"*n, raw, pos)
        (flags if mutable else bindings).extend(range(pos, pos+4*n, 4)); pos += 4*n
        return values
    def floating(n=1):
        nonlocal pos
        floats.extend(range(pos, pos+8*n, 8)); pos += 8*n
    active = ints()[0]; mode = None; replay = 0; max_steps = None
    if active:
        mode, _, areas, pollutants, gages, replay = ints(6)
        if mode in (2, 3):
            ints(2)
            if mode == 2: max_steps = ints()[0]
        ints(4, True)
        if replay:
            floating()
            for _ in range(gages):
                length = ints()[0]
                if length: bindings.append(pos)
                pos += length; floating()
    assert pos == len(raw), (pos, len(raw), active, mode)
    return dict(start=start, active=active, mode=mode, max_steps=max_steps,
                replay=replay, bindings=bindings, flags=flags, floats=floats)


class Engine(GageEngine):
    def __init__(self, *args):
        super().__init__(*args)
        for name, args, result in (
            ("es_test_runoff_save", [c.c_void_p, c.c_size_t, c.POINTER(c.c_size_t)], c.c_int),
            ("es_test_runoff_restore", [c.c_void_p, c.c_size_t, c.c_int], c.c_int),
            ("es_test_runoff_poison", [c.c_int], None),
            ("es_test_runoff_bundle_poison", [], None),
            ("es_test_runoff_observe", [c.c_int], c.c_double),
            ("es_test_ode_poison", [], None),
            ("es_test_ode_reset_counts", [], None),
            ("es_test_ode_calls", [c.c_int], c.c_int),
        ):
            f = getattr(self.lib, name); f.argtypes, f.restype = args, result
        self.lib.es_test_ode_reset_counts()

    def dump(self):
        size = c.c_size_t(); self.check(self.lib.es_test_runoff_save(None, 0, c.byref(size)))
        data = c.create_string_buffer(size.value)
        self.check(self.lib.es_test_runoff_save(data, size.value, c.byref(size)))
        return data.raw

    def restore(self, raw, fail_at=-1):
        return self.lib.es_test_runoff_restore(c.create_string_buffer(bytes(raw)), len(raw), fail_at)


@unittest.skipUnless(os.environ.get("EASYSEWER_CHECKPOINT_STANDARD") and
                     os.environ.get("EASYSEWER_CHECKPOINT_CUSTOM"),
                     "Requires instrumented runoff-state libraries")
class NativeCheckpointRunoffTests(unittest.TestCase):
    def run_case(self, family, text, action=None, *, mode="step", api=False):
        with tempfile.TemporaryDirectory() as root:
            e = Engine(os.environ["EASYSEWER_CHECKPOINT_"+family.upper()], root, text)
            history, observations = [], []
            try:
                net = network(e.dump()[:e.dump().index(b"ESINL001")])
                for index in range(10000):
                    if api: e.lib.swmm_setValue(100, 0, (2., 5., 0.)[(index//3) % 3])
                    raw = e.dump()
                    if action == "restore":
                        e.lib.es_test_runoff_bundle_poison()
                        self.assertEqual(e.restore(raw), 0); self.assertEqual(e.dump(), raw)
                    elif action == "scratch":
                        e.lib.es_test_runoff_poison(7); e.lib.es_test_ode_poison()
                    elif isinstance(action, int): e.lib.es_test_runoff_poison(action)
                    if mode == "stride":
                        elapsed = c.c_double(); e.check(e.lib.swmm_stride(47, c.byref(elapsed))); now = elapsed.value
                    elif mode == "split":
                        e.check(e.lib.swmm_execRouting()); self.assertEqual(e.restore(raw), 5)
                        e.check(e.lib.swmm_saveResults()); frame = clocks(e.dump())
                        now = frame["routing"]/86400000 if frame["routing"] < frame["duration"] else 0
                    else: now = e.step()[0]
                    state = clocks(e.dump())
                    history.append((now, state["old_runoff"], state["runoff"],
                                    *(e.lib.swmm_getValue(code, i) for i in range(net["nodes"])
                                      for code in (303, 305, 306, 307, 308)),
                                    *(e.lib.swmm_getValue(code, i) for i in range(net["links"])
                                      for code in (407, 410, 411))))
                    observations.append(tuple(e.lib.es_test_runoff_observe(k) for k in range(7)))
                    if not now:
                        if action == "restore":
                            raw = e.dump(); e.lib.es_test_runoff_bundle_poison()
                            self.assertEqual(e.restore(raw), 0); self.assertEqual(e.dump(), raw)
                        break
                else: self.fail("Runoff fixture did not finish")
                ode_calls = tuple(e.lib.es_test_ode_calls(n) for n in (1, 2))
            finally: e.close()
            report = re.sub(rb"(?m)^[ \t]*(?:Analysis (?:begun|ended)|Total elapsed time)[^\r\n]*", b"", e.paths[1].read_bytes())
            saved = re.search(r'(?m)^SAVE RUNOFF "([^"]+)"', text)
            cache = Path(saved[1]).read_bytes() if saved else None
            return (history, e.paths[2].read_bytes(), report, cache), observations, ode_calls

    def test_wet_dry_processes_and_caches_restore_full_results(self):
        cases = [dict(), dict(dry=True), dict(zero_area=True), dict(ignore_routing=True),
                 dict(groundwater=True), dict(snow=True), dict(dry=True, snow=True, cold=True),
                 dict(units="CMS"), dict(save=True), dict(save=True, groundwater=True)]
        cases += [dict(lid=kind) for kind in ("BC", "RG", "GR", "IT", "PP", "RB", "RD", "VS")]
        cases += [dict(lid="BC", groundwater=True), dict(lid="RB", dry=True)]
        for family in ("standard", "custom"):
            for options in cases:
                with self.subTest(family=family, **options), tempfile.TemporaryDirectory() as root:
                    text = fixture(root, **options); expected = self.run_case(family, text)
                    self.assertEqual(self.run_case(family, text, "restore"), expected)
            self.assertEqual(self.run_case(family, controls(), "restore"), self.run_case(family, controls()))

    def test_replay_rain_and_independent_consumption_clocks(self):
        for family in ("standard", "custom"):
            for options in (dict(), dict(co=True), dict(source="rain-file"), dict(source="timeseries-file"),
                            dict(units="CMS"), dict(ignore=True), dict(late=True), dict(long=True)):
                with self.subTest(family=family, **options), tempfile.TemporaryDirectory() as root:
                    text = rain_fixture(root, replay=True, **options); expected = self.run_case(family, text)
                    self.assertEqual(self.run_case(family, text, "restore"), expected)
            with tempfile.TemporaryDirectory() as root:
                text = rain_fixture(root, replay=True, co=True)
                self.assertEqual(self.run_case(family, text, "restore", api=True), self.run_case(family, text, api=True))

    def test_scratch_and_each_persistent_group_are_causally_exercised(self):
        for family in ("standard", "custom"):
            for options in (dict(), dict(groundwater=True), dict(lid="BC", groundwater=True), dict(save=True)):
                with tempfile.TemporaryDirectory() as root:
                    text = fixture(root, **options); expected = self.run_case(family, text)
                    self.assertEqual(self.run_case(family, text, "scratch"), expected)
                    self.assertGreater(expected[2][0], 0)
                    if options.get("groundwater"): self.assertGreater(expected[2][1], 0)
            for group, options in ((1, {}), (2, dict(snow=True, dry=True, cold=True)),
                                   (3, dict(lid="RB", dry=True)), (4, dict(save=True))):
                with self.subTest(family=family, group=group), tempfile.TemporaryDirectory() as root:
                    text = fixture(root, **options)
                    self.assertNotEqual(self.run_case(family, text, group)[0], self.run_case(family, text)[0])
            with tempfile.TemporaryDirectory() as root:
                text = rain_fixture(root, replay=True, co=True); expected = self.run_case(family, text)
                self.assertEqual(self.run_case(family, text, "scratch"), expected)
                for group in (5, 6): self.assertNotEqual(self.run_case(family, text, group)[0], expected[0])

    def test_stride_and_custom_split(self):
        for family, mode in (("standard", "stride"), ("custom", "stride"), ("custom", "split")):
            with tempfile.TemporaryDirectory() as root:
                for text in (fixture(root, lid="BC", groundwater=True), rain_fixture(root, replay=True, ponding=True)):
                    self.assertEqual(self.run_case(family, text, "restore", mode=mode),
                                     self.run_case(family, text, mode=mode))

    def test_final_corruption_and_allocation_failure_are_atomic(self):
        for family in ("standard", "custom"):
            with tempfile.TemporaryDirectory() as root:
                for text in (fixture(root), fixture(root, save=True), rain_fixture(root, replay=True, co=True), controls()):
                    e = Engine(os.environ["EASYSEWER_CHECKPOINT_"+family.upper()], root, text)
                    try:
                        for _ in range(6): e.step()
                        raw = e.dump(); layout = runoff(raw)
                        def reject(bad):
                            self.assertNotEqual(e.restore(bad), 0); self.assertEqual(e.dump(), raw)
                        for size in range(layout["start"], len(raw)): reject(raw[:size])
                        reject(raw+b"x")
                        for offset in layout["bindings"]:
                            bad = bytearray(raw); bad[offset] ^= 1; reject(bad)
                        for offset in layout["flags"][:3]:
                            for value in (-1, 2):
                                bad = bytearray(raw); struct.pack_into("<i", bad, offset, value); reject(bad)
                        if layout["flags"]:
                            for value in (-1, *((layout["max_steps"]+1,) if layout["max_steps"] is not None else ())):
                                bad = bytearray(raw); struct.pack_into("<i", bad, layout["flags"][3], value); reject(bad)
                        for offset in layout["floats"]:
                            for value in (float("nan"), float("inf"), -float("inf")):
                                bad = bytearray(raw); struct.pack_into("<d", bad, offset, value); reject(bad)
                        if layout["floats"]:
                            for value in (-1., clocks(raw)["duration"]+1.):
                                bad = bytearray(raw); struct.pack_into("<d", bad, layout["floats"][0], value); reject(bad)
                        if "[CONTROLS]" in text:
                            self.assertEqual(e.restore(raw, 0), 6); self.assertEqual(e.dump(), raw)
                        self.assertEqual(e.restore(raw), 0); self.assertEqual(e.dump(), raw)
                    finally: e.close()
