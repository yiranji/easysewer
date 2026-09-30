"""Rain owner qualification; table/runoff/file handles remain alive in these tests."""
import ctypes as c
from dataclasses import replace
from datetime import datetime, timedelta
import os
from pathlib import Path
import re
import struct
import tempfile
import unittest

from test_native_v2_checkpoint_catchment import Engine as CatchmentEngine
from test_native_v2_checkpoint_controls import fixture as controls
from test_native_v2_checkpoint_clocks import clocks
from test_native_v2_runoff_rain_clock import rain_model, controlled_source, cache_bytes


def fixture(root, *, form="INTENSITY", source="inline", units="CFS", co=False,
            late=False, ignore=False, unused=False, replay=False, long=False, reset=False, ponding=False):
    duration = (50 if long else 4)*3600
    schedule = [(i*1800, (1., 3., 0., 2., 4., 0.)[i % 6]) for i in range(duration//1800+1)]
    if late: schedule = [(s+900, v) for s, v in schedule[:4]]
    model = rain_model(Path(root), schedule, duration=duration, form=form, source=source,
                       units=units, co_gage=co, start=datetime(2020, 1, 31, 22), route_step=300)
    model.update_options(wet_step=timedelta(seconds=300), dry_step=timedelta(seconds=600),
                         ignore_rainfall=ignore)
    if ponding: model.update_options(allow_ponding=True)
    if reset:
        points = model.timeseries["Rain"].points
        base = points[3].value
        model.timeseries.update("Rain", points=tuple(replace(p, value=p.value-base) if i >= 4 else p
                                                     for i, p in enumerate(points)))
    if unused: model.raingages.add(replace(model.raingages["R"], id="Unused"))
    text = controlled_source(model, gage="R2" if co else "R")
    text += "[ADJUSTMENTS]\nRAINFALL .5 2 1 1 1 1 1 1 1 1 1 1\n"
    if replay:
        path = Path(root)/"runoff.bin"
        path.write_bytes(cache_bytes(model, (17.5, 4000.25, duration-4017.75)))
        text += f'[FILES]\nUSE RUNOFF "{path}"\n'
    return text


def gages(raw):
    start = raw.index(b"ESGAG001"); pos = start+8
    bindings, floats, flags, positions = [], [], [], []
    def integers(n=1):
        nonlocal pos
        values = struct.unpack_from("<"+"i"*n, raw, pos)
        bindings.extend(range(pos, pos+4*n, 4)); pos += 4*n
        return values
    def doubles(n=1, mutable=False):
        nonlocal pos
        (floats if mutable else bindings).extend(range(pos, pos+8*n, 8)); pos += 8*n
    def identity():
        nonlocal pos
        length = integers()[0]
        if length: bindings.append(pos)
        pos += length
    count, hours, _, _ = integers(4)
    for _ in range(count):
        identity(); source = integers(7)[0]; doubles(2)
        if source == 1:  # RAIN_FILE
            identity(); identity(); doubles(2); low, high = integers(2)
            positions.append((pos, low, high)); pos += 8
        doubles(8+hours+1, True); flags.append(pos); pos += 4
    assert pos == len(raw), (pos, len(raw))
    return dict(start=start, count=count, bindings=bindings, floats=floats, flags=flags, positions=positions)


class Engine(CatchmentEngine):
    def __init__(self, *args):
        super().__init__(*args)
        for name, args, result in (
            ("es_test_gage_save", [c.c_void_p, c.c_size_t, c.POINTER(c.c_size_t)], c.c_int),
            ("es_test_gage_restore", [c.c_void_p, c.c_size_t, c.c_int], c.c_int),
            ("es_test_gage_poison", [c.c_int], None),
            ("es_test_gage_bundle_poison", [], None),
            ("es_test_gage_observe", [c.c_int, c.c_int], c.c_double),
            ("swmm_getIndex", [c.c_int, c.c_char_p], c.c_int),
            ("swmm_setValue", [c.c_int, c.c_int, c.c_double], None),
        ):
            f = getattr(self.lib, name); f.argtypes, f.restype = args, result

    def dump(self):
        size = c.c_size_t(); self.check(self.lib.es_test_gage_save(None, 0, c.byref(size)))
        data = c.create_string_buffer(size.value)
        self.check(self.lib.es_test_gage_save(data, size.value, c.byref(size)))
        return data.raw

    def restore(self, raw, fail_at=-1):
        return self.lib.es_test_gage_restore(c.create_string_buffer(bytes(raw)), len(raw), fail_at)


@unittest.skipUnless(os.environ.get("EASYSEWER_CHECKPOINT_STANDARD") and
                     os.environ.get("EASYSEWER_CHECKPOINT_CUSTOM"),
                     "Requires instrumented gage-state libraries")
class NativeCheckpointGageTests(unittest.TestCase):
    def run_case(self, family, text, action=None, *, api=False, mode="step"):
        with tempfile.TemporaryDirectory() as root:
            e = Engine(os.environ["EASYSEWER_CHECKPOINT_"+family.upper()], root, text)
            history, observations = [], []
            try:
                count = gages(e.dump())["count"]
                indices = [e.lib.swmm_getIndex(3, f"Probe{i}".encode()) for i in range(49)] if count else []
                for index in range(10000):
                    if api: e.lib.swmm_setValue(100, 0, (2., 5., 0.)[(index//3) % 3])
                    raw = e.dump()
                    if action == "restore":
                        e.lib.es_test_gage_bundle_poison()
                        self.assertEqual(e.restore(raw), 0); self.assertEqual(e.dump(), raw)
                    elif action == "scratch": e.lib.es_test_gage_poison(6)
                    elif isinstance(action, int) and index == 8: e.lib.es_test_gage_poison(action)
                    if mode == "stride":
                        elapsed = c.c_double(); e.check(e.lib.swmm_stride(347, c.byref(elapsed))); now = elapsed.value
                    elif mode == "split":
                        e.check(e.lib.swmm_execRouting()); self.assertEqual(e.restore(raw), 5)
                        e.check(e.lib.swmm_saveResults()); frame = clocks(e.dump())
                        now = frame["routing"]/86400000 if frame["routing"] < frame["duration"] else 0
                    else: now = e.step()[0]
                    history.append((now, *(e.lib.swmm_getValue(407, i) for i in indices)))
                    observations.append(tuple(tuple(e.lib.es_test_gage_observe(j, k) for k in range(56))
                                              for j in range(count)))
                    if not now:
                        if action == "restore":
                            raw = e.dump(); e.lib.es_test_gage_bundle_poison()
                            self.assertEqual(e.restore(raw), 0); self.assertEqual(e.dump(), raw)
                        break
                else: self.fail("Rain fixture did not finish")
            finally: e.close()
            report = re.sub(rb"(?m)^[ \t]*(?:Analysis (?:begun|ended)|Total elapsed time)[^\r\n]*", b"", e.paths[1].read_bytes())
            return (history, e.paths[2].read_bytes(), report), observations

    def test_variants_histories_shared_gages_files_and_replay(self):
        cases = [dict(form=form) for form in ("INTENSITY", "VOLUME", "CUMULATIVE")]
        cases += [dict(form="CUMULATIVE", reset=True), dict(co=True), dict(late=True),
                  dict(ignore=True), dict(unused=True), dict(units="CMS"),
                  dict(source="timeseries-file"), dict(source="rain-file", co=True),
                  dict(source="rain-file", units="CMS"), dict(replay=True, co=True),
                  dict(replay=True, source="rain-file"), dict(long=True)]
        for family in ("standard", "custom"):
            for options in cases:
                with self.subTest(family=family, **options), tempfile.TemporaryDirectory() as root:
                    text = fixture(root, **options); expected = self.run_case(family, text)
                    self.assertEqual(self.run_case(family, text, "restore"), expected)
                    if options.get("long"):
                        self.assertGreater(max(row[0][48] for row in expected[1]), 0)
                        self.assertTrue(any(row[0][49] == 3600 for row in expected[1]))
            with tempfile.TemporaryDirectory() as root:
                text = fixture(root, co=True)
                expected = self.run_case(family, text, api=True)
                self.assertEqual(self.run_case(family, text, "restore", api=True), expected)
            self.assertEqual(self.run_case(family, controls(), "restore"), self.run_case(family, controls()))

    def test_omission_changes_scientific_results_and_scratch_does_not(self):
        for family in ("standard", "custom"):
            for group, options, api in ((1, {}, False), (2, dict(form="CUMULATIVE"), False),
                                       (3, {}, False), (4, {}, True), (5, dict(source="rain-file"), False)):
                with self.subTest(family=family, group=group), tempfile.TemporaryDirectory() as root:
                    text = fixture(root, **options); expected = self.run_case(family, text, api=api)
                    self.assertNotEqual(self.run_case(family, text, group, api=api)[0], expected[0])
                    self.assertEqual(self.run_case(family, text, "scratch", api=api), expected)

    def test_stride_and_custom_split(self):
        for family, mode in (("standard", "stride"), ("custom", "stride"), ("custom", "split")):
            with tempfile.TemporaryDirectory() as root:
                text = fixture(root, co=True, ponding=mode == "split")
                self.assertEqual(self.run_case(family, text, "restore", mode=mode),
                                 self.run_case(family, text, mode=mode))

    def test_corruption_and_failed_staging_leave_all_owners_unchanged(self):
        for family in ("standard", "custom"):
            for source in ("inline", "rain-file"):
                with tempfile.TemporaryDirectory() as root:
                    e = Engine(os.environ["EASYSEWER_CHECKPOINT_"+family.upper()], root,
                               fixture(root, source=source, co=True))
                    try:
                        for _ in range(8): e.step()
                        raw = e.dump(); layout = gages(raw)
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
                            for value in (-1, 3601):
                                bad = bytearray(raw); struct.pack_into("<i", bad, offset, value); reject(bad)
                        for offset, low, high in layout["positions"]:
                            for value in (low-1, low+1, high+12, 2**64-1):
                                bad = bytearray(raw); struct.pack_into("<Q", bad, offset, value); reject(bad)
                        self.assertEqual(e.restore(raw, 0), 6); self.assertEqual(e.dump(), raw)
                        self.assertEqual(e.restore(raw), 0); self.assertEqual(e.dump(), raw)
                    finally: e.close()
