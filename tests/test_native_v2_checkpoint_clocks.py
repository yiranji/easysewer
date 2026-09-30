"""Engine/routing clock + controls component tests; other state remains live."""
import ctypes as c
from datetime import date, time, timedelta
import os
from pathlib import Path
import re
import struct
import sys
import tempfile
import unittest

from easysewer.model.resources import SeriesPoint
from test_hydrology_v2 import hydrology_model
from test_native_v2_checkpoint_controls import Engine as ControlEngine


def fixture(*, events=True, averages=False, ignore_routing=False, units="CFS"):
    model = hydrology_model()
    model.update_options(start_date=date(2020, 1, 30), end_date=date(2020, 1, 30),
                         end_time=time(0, 6), report_start_time=time(0, 1),
                         wet_step=timedelta(seconds=60), dry_step=timedelta(seconds=120),
                         routing_step=timedelta(seconds=2), report_step=timedelta(seconds=30),
                         rule_step=timedelta(seconds=7), variable_step=.75,
                         allow_ponding=True, ignore_routing=ignore_routing)
    model.raingages.update("R", interval=timedelta(seconds=60))
    model.timeseries.update("Rain", points=tuple(SeriesPoint(time=timedelta(seconds=s), value=v)
                                                for s, v in ((0, .6), (120, 0), (240, .9), (360, 0))))
    if units != "CFS":
        model.convert_units(units)
    text = model.to_document().text
    text += "[REPORT]\nCONTROLS YES\nNODES ALL\nLINKS ALL\nAVERAGES " + ("YES" if averages else "NO") + "\n"
    text += "[CONTROLS]\nRULE R\nIF SIMULATION TIME >= 00:00:55\nTHEN OUTLET P SETTING = .3\nELSE OUTLET P SETTING = .9\n"
    if events:
        # Intentionally unsorted and overlapping: bind post-sort/trim layout,
        # and continue beyond the last real event into the terminal sentinel.
        text += "[EVENTS]\n01/30/2020 00:02:00 01/30/2020 00:03:00\n01/30/2020 00:00:30 01/30/2020 00:02:10\n"
    return text


def clocks(raw):
    """Independent wire reader for assertions and targeted corruption tests."""
    assert raw[:8] == b"ESCLK001"
    report, old_runoff, runoff, old_routing, routing, elapsed, route_step, courant = struct.unpack_from("<8d", raw, 76)
    total, reported, nonconverge, warnings = struct.unpack_from("<3Qi", raw, 140)
    pos = 168
    assert raw[pos:pos+8] == b"ESROUT01"
    active, = struct.unpack_from("<i", raw, pos+8)
    pos += 12
    result = dict(report=report, old_runoff=old_runoff, runoff=runoff, old_routing=old_routing,
                  routing=routing, elapsed=elapsed, route_step=route_step, courant=courant,
                  total=total, reported=reported, nonconverge=nonconverge, warnings=warnings, active=active)
    result["duration"] = struct.unpack_from("<d", raw, 48)[0]
    if active:
        route_model, rule_step, links = struct.unpack_from("<3i", raw, pos)
        pos += 12
        for _ in range(links):
            index, size = struct.unpack_from("<iI", raw, pos)
            pos += 8 + size
        events, = struct.unpack_from("<i", raw, pos)
        pos += 4
        if events:
            pos += 16 * (events+1)
        next_event, between, rule_time = struct.unpack_from("<iid", raw, pos)
        result.update(events=events, next_event=next_event, between=between,
                      rule_time=rule_time, routing_state_offset=pos)
        pos += 16
    assert raw[pos:pos+8] == b"ESCTRL01"
    result["controls_offset"] = pos
    return result


class Engine(ControlEngine):
    def __init__(self, *args):
        self.closed = False
        super().__init__(*args)
        for name, args, result in (
            ("es_test_clocks_save", [c.c_void_p, c.c_size_t, c.POINTER(c.c_size_t)], c.c_int),
            ("es_test_clocks_restore", [c.c_void_p, c.c_size_t, c.c_int], c.c_int),
            ("es_test_clocks_poison", [], None),
            ("es_test_clocks_boundary", [c.c_int], c.c_int),
            ("swmm_stride", [c.c_int, c.POINTER(c.c_double)], c.c_int),
            ("swmm_setValue", [c.c_int, c.c_int, c.c_double], None),
        ):
            f = getattr(self.lib, name)
            f.argtypes, f.restype = args, result
        if hasattr(self.lib, "swmm_execRouting"):
            self.lib.swmm_execRouting.argtypes = []
            self.lib.swmm_execRouting.restype = c.c_int
            self.lib.swmm_saveResults.argtypes = []
            self.lib.swmm_saveResults.restype = c.c_int

    def dump(self):
        size = c.c_size_t()
        self.check(self.lib.es_test_clocks_save(None, 0, c.byref(size)))
        data = c.create_string_buffer(size.value)
        self.check(self.lib.es_test_clocks_save(data, size.value, c.byref(size)))
        return data.raw

    def restore(self, raw, fail_at=-1):
        return self.lib.es_test_clocks_restore(c.create_string_buffer(raw), len(raw), fail_at)

    def close(self):
        if not self.closed:
            self.closed = True
            unwinding = sys.exc_info()[0] is not None
            error = self.lib.swmm_end()
            if not error:
                error = self.lib.swmm_report()
            close_error = self.lib.swmm_close()
            if not unwinding:
                self.check(error or close_error)


@unittest.skipUnless(os.environ.get("EASYSEWER_CHECKPOINT_STANDARD") and
                     os.environ.get("EASYSEWER_CHECKPOINT_CUSTOM"),
                     "Requires both internal clock test builds")
class NativeCheckpointClockTests(unittest.TestCase):
    def engines(self, text=None):
        for family in ("standard", "custom"):
            with tempfile.TemporaryDirectory() as root:
                e = Engine(os.environ["EASYSEWER_CHECKPOINT_"+family.upper()], root,
                           fixture() if text is None else text)
                e.family = family
                try:
                    yield e
                finally:
                    e.close()

    def test_initial_frame_hydrology_ahead_events_sentinel_and_repeated_restore(self):
        for e in self.engines():
            initial = e.dump()
            self.assertEqual(clocks(initial)["routing"], 0)
            self.assertEqual(clocks(initial)["next_event"], 0)
            self.assertEqual(e.restore(initial), 0)
            ahead = False
            seen = set()
            for _ in range(3000):
                row = e.step()
                raw = e.dump()
                state = clocks(raw)
                ahead |= state["runoff"] > state["routing"]
                seen.add(state["next_event"])
                e.lib.es_test_clocks_poison()
                self.assertEqual(e.restore(raw), 0)
                self.assertEqual(e.dump(), raw)
                if row[0] == 0:
                    break
            else:
                self.fail("Fixture did not finish")
            self.assertTrue(ahead)
            self.assertEqual(seen, {0, 1, 2})

    def test_truncation_bad_clock_counts_indices_and_late_module_preserve_all_state(self):
        for e in self.engines():
            for _ in range(23):
                e.step()
            raw = e.dump()
            state = clocks(raw)
            damaged = [raw[:n] for n in range(len(raw))] + [raw+b"\x00"]
            for offset in (0, 8, 24, 56, 168, state["controls_offset"]):
                value = bytearray(raw); value[offset] ^= 1; damaged.append(bytes(value))
            for offset in range(76, 140, 8):
                for number in (float("nan"), float("inf")):
                    value = bytearray(raw); struct.pack_into("<d", value, offset, number); damaged.append(bytes(value))
            for offset in (76, 92, 108, 116, 124, 132):
                value = bytearray(raw); struct.pack_into("<d", value, offset, -1); damaged.append(bytes(value))
            for offset in (140, 148, 156):
                value = bytearray(raw); struct.pack_into("<Q", value, offset, 2**63); damaged.append(bytes(value))
            for offset in (148, 156):
                value = bytearray(raw); struct.pack_into("<Q", value, offset, state["total"]+1); damaged.append(bytes(value))
            for offset in (92, 108, 116):
                value = bytearray(raw); struct.pack_into("<d", value, offset, 1e20); damaged.append(bytes(value))
            value = bytearray(raw); struct.pack_into("<i", value, 164, -1); damaged.append(bytes(value))
            for offset, number in ((state["routing_state_offset"], 3), (state["routing_state_offset"]+4, 2)):
                value = bytearray(raw); struct.pack_into("<i", value, offset, number); damaged.append(bytes(value))
            value = bytearray(raw)
            struct.pack_into("<d", value, state["routing_state_offset"]+8, state["routing"]+7000)
            damaged.append(bytes(value))
            for value in damaged:
                self.assertNotEqual(e.restore(value), 0)
                self.assertEqual(e.dump(), raw)
            # Failure allocating a controls-stage list occurs AFTER all clock
            # and control validation; none of the clocks may be committed yet.
            self.assertEqual(e.restore(raw, 0), 6)
            self.assertEqual(e.dump(), raw)
            self.assertEqual(e.restore(raw), 0)

    def test_native_long_width_is_checked_without_truncation(self):
        for e in self.engines():
            e.step()
            raw = e.dump()
            value = bytearray(raw)
            struct.pack_into("<Q", value, 140, 2**31+19)
            error = e.restore(bytes(value))
            if os.name == "nt":
                self.assertNotEqual(error, 0)
                self.assertEqual(e.dump(), raw)
            else:
                self.assertEqual(error, 0)
                self.assertEqual(clocks(e.dump())["total"], 2**31+19)
                self.assertEqual(e.restore(raw), 0)

    def test_boundary_rejects_closed_failed_exception_and_partial_stride(self):
        for e in self.engines():
            raw = e.dump()
            self.assertEqual(e.lib.es_test_clocks_boundary(0), 0)
            for fault in (1, 2, 3):
                self.assertEqual(e.lib.es_test_clocks_boundary(fault), 5)
                self.assertEqual(e.dump(), raw)
            e.close()
            data = c.create_string_buffer(b"Z"*len(raw))
            size = c.c_size_t(999)
            self.assertEqual(e.lib.es_test_clocks_save(data, len(raw), c.byref(size)), 5)
            self.assertEqual(data.raw[:-1], b"Z"*len(raw))
            self.assertEqual(size.value, 0)
            self.assertEqual(e.restore(raw), 5)

    def test_custom_half_step_refused_and_complete_split_step_restores(self):
        path = os.environ["EASYSEWER_CHECKPOINT_CUSTOM"]
        with tempfile.TemporaryDirectory() as root:
            e = Engine(path, root, fixture())
            try:
                for _ in range(30):
                    raw = e.dump()
                    e.check(e.lib.swmm_execRouting())
                    self.assertEqual(e.lib.es_test_clocks_boundary(0), 5)
                    self.assertEqual(e.restore(raw), 5)
                    size = c.c_size_t()
                    self.assertEqual(e.lib.es_test_clocks_save(None, 0, c.byref(size)), 5)
                    e.check(e.lib.swmm_saveResults())
                    raw = e.dump()
                    e.lib.es_test_clocks_poison()
                    self.assertEqual(e.restore(raw), 0)
                    self.assertEqual(e.dump(), raw)
            finally:
                e.close()

    def test_complete_output_equivalence_with_events_averages_stride_and_disabled_routing(self):
        cases = [dict(events=True), dict(events=False, averages=True),
                 dict(events=True, averages=True, units="CMS"), dict(ignore_routing=True)]
        for family in ("standard", "custom"):
            for args in cases:
                modes = ("step", "stride", "split") if family == "custom" else ("step", "stride")
                for mode in modes:
                    with self.subTest(family=family, args=args, mode=mode):
                        # The custom external-ponding API requires active routing.
                        if mode == "split" and args.get("ignore_routing"):
                            continue
                        outputs = []
                        for restore in (False, True):
                            with tempfile.TemporaryDirectory() as root:
                                e = Engine(os.environ["EASYSEWER_CHECKPOINT_"+family.upper()], root, fixture(**args))
                                history = []
                                try:
                                    for i in range(4000):
                                        # Live API changes both RouteStep and CourantFactor.
                                        if i == 5:
                                            e.lib.swmm_setValue(3, 0, 3.75)
                                        if mode == "stride":
                                            elapsed = c.c_double()
                                            e.check(e.lib.swmm_stride((11, 17, 23)[i%3], c.byref(elapsed)))
                                            now = elapsed.value
                                        elif mode == "split":
                                            e.check(e.lib.swmm_execRouting())
                                            e.check(e.lib.swmm_saveResults())
                                            frame = clocks(e.dump())
                                            now = frame["routing"]/86400000 if frame["routing"] < frame["duration"] else 0
                                        else:
                                            now = e.step()[0]
                                        state = clocks(e.dump())
                                        history.append((now, state["runoff"], state["routing"], state["report"],
                                                        e.lib.swmm_getValue(303, 0), e.lib.swmm_getValue(407, 0)))
                                        if restore:
                                            raw = e.dump(); e.lib.es_test_clocks_poison()
                                            self.assertEqual(e.restore(raw), 0)
                                            self.assertEqual(e.dump(), raw)
                                        if now == 0:
                                            break
                                    else:
                                        self.fail("Fixture did not finish")
                                finally:
                                    e.close()
                                report = re.sub(rb"(?m)^[ \t]*(?:Analysis (?:begun|ended)|Total elapsed time)[^\r\n]*", b"", e.paths[1].read_bytes())
                                outputs.append((history, e.paths[2].read_bytes(), report))
                        self.assertEqual(outputs[0], outputs[1])


if __name__ == "__main__":
    unittest.main()
