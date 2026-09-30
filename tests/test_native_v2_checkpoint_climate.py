"""Climate numerical owner. Independent table cursors/streams remain live."""
import ctypes as c
from datetime import date, time, timedelta
import os
from pathlib import Path
import re
import struct
import tempfile
import unittest

from easysewer.model import Ref, FileReference
from easysewer.model import climate as cl
from easysewer.model.resources import Pattern, FileTimeSeries
from test_climate_v2 import climate_model, add_series
from test_climate_data_v2 import monthly_fixture
from test_native_v2_climate import ghcnd_weather, SNOW_CATCHMENT, DRY_CATCHMENT
from test_native_v2_checkpoint_runoff import Engine as RunoffEngine
from test_native_v2_checkpoint_controls import fixture as controls
from test_native_v2_checkpoint_clocks import clocks


def fixture(root, *, evaporation="FILE", format="USER", temperature="file", units="CFS",
            days=4, snow=False, file_series=False, shared=False, eof=False,
            missing=False, shifted=False, dry_only=False, no_catchment=False, leap=False, label="C10"):
    root = Path(root); model = climate_model()
    model.reinterpret_units(units)
    start = date(2020, 2, 27) if leap else date(2020, 1, 30)
    model.update_options(start_date=start, report_start_date=start, end_date=start+timedelta(days=days),
                         end_time=time(), routing_step=timedelta(hours=1), wet_step=timedelta(hours=1),
                         dry_step=timedelta(hours=3), report_step=timedelta(hours=3), allow_ponding=True)
    air_values = ((0, 20), (1.5, 80), (20, -10), (48, 50), (96, 40), (240, 30))
    pet_values = ((0, .1), (13, .5), (25, .2), (60, .8), (144, .4))
    air = add_series(model, "Air", air_values); pet = add_series(model, "PET", pet_values)
    if file_series:
        for name, values in (("Air", air_values), ("PET", pet_values)):
            path = root/(name+".txt"); path.write_text("".join(f"{s:.17g} {v:.17g}\n" for s, v in values), encoding="ascii")
            model.timeseries.replace(name, FileTimeSeries(id=name, file=FileReference(path=str(path))))
    needs_file = temperature == "file" or evaporation in ("FILE", "TEMPERATURE")
    if needs_file:
        path = root/"weather.dat"
        if format == "USER":
            rows = []
            for offset in range(78 if leap else (31 if eof else 48)):
                when = date(2020, 1, 1)+timedelta(days=offset)
                if missing and offset % 4 == 2: continue
                low = 20+(offset % 9)*3; high = low+5+(offset % 5)*4
                rows.append(f"Station {when.year} {when.month} {when.day} {high} {low} {.1+.02*(offset%7):.8g} {2+4*(offset%4)}")
            path.write_text("\n".join(rows), encoding="ascii")  # complete final record without LF
        elif format == "GHCND": ghcnd_weather(path, label)
        else: path.write_bytes(monthly_fixture(format))
        model.update_climate(file=cl.ClimateFile(file=FileReference(path=str(path)), units=label,
                                                start_date=date(2020, 1, 1) if shifted else None),
                             wind=cl.FileWind())
    else: model.update_climate(wind=cl.MonthlyWindSpeeds(values=(4, 12, *(8.,)*10)))
    if temperature == "series": model.update_climate(temperature=cl.SeriesTemperature(series=air))
    model.patterns.add(Pattern(id="Recovery", kind="MONTHLY", factors=(.5, 2)))
    sources = {"CONSTANT": cl.ConstantEvaporation(rate=.2),
               "MONTHLY": cl.MonthlyEvaporation(values=(.1, .8, *(.3,)*10)),
               "TIMESERIES": cl.SeriesEvaporation(series=pet),
               "TEMPERATURE": cl.TemperatureEvaporation(),
               "FILE": cl.FileEvaporation(pan_coefficients=cl.MonthlyFactors(values=(.7, .9, *(1.,)*10)))}
    model.update_climate(evaporation=cl.Evaporation(source=sources[evaporation], dry_only=dry_only,
                         recovery_pattern=Ref(collection="swmm:patterns", key="Recovery")),
                         adjustments=cl.ClimateAdjustments(
                             temperature=cl.MonthlyTemperatureChanges(values=(9, -9, *(0.,)*10)),
                             evaporation=cl.MonthlyEvaporation(values=(.01, -.005, *(0.,)*10)),
                             rainfall=cl.MonthlyFactors(values=(.5, 2, *(1.,)*10)),
                             conductivity=cl.MonthlyFactors(values=(.5, 2, *(1.,)*10))))
    text = model.to_document().text
    if shared: text = text.replace("TIMESERIES PET", "TIMESERIES Air")
    if not no_catchment: text += SNOW_CATCHMENT if snow else DRY_CATCHMENT
    return text+"[REPORT]\nSUBCATCHMENTS ALL\nNODES ALL\nLINKS ALL\n"


def climate(raw):
    start = raw.index(b"ESCLM001"); pos = start+8
    bindings, floats, flags = [], [], []
    def ints(n=1):
        nonlocal pos
        values = struct.unpack_from("<"+"i"*n, raw, pos)
        bindings.extend(range(pos, pos+4*n, 4)); pos += 4*n
        return values
    def number(low, high):
        nonlocal pos
        value = struct.unpack_from("<i", raw, pos)[0]
        flags.append((pos, low, high)); pos += 4; return value
    def doubles(n=1, mutable=False):
        nonlocal pos
        (floats if mutable else bindings).extend(range(pos, pos+8*n, 8)); pos += 8*n
    _, _, file_mode = ints(3); temp, _ = ints(2); doubles(6)
    _, evap, _, _, _ = ints(5); doubles(3+20+84)
    state_start = pos; doubles(15, True)
    if temp == 2: doubles(10, True)
    count = None
    if evap == 3:
        assert raw[pos:pos+8] == b"ESCMA001"; bindings.append(pos); pos += 8
        ints(); count = number(0, 7); number(0, 6); doubles(2+2*count, True)
    if file_mode == 2:
        assert raw[pos:pos+8] == b"ESCLF001"; bindings.append(pos); pos += 8
        size = ints()[0]; bindings.append(pos); pos += size
        format = ints()[0]
        if format == 2: ints(14)
        for low, high in ((1, 9999), (1, 12), (1, 31), (28, 31), (0, 2147483647),
                          (0, 1), (0, 1), (13, 120000), (1, 9999), (1, 12)):
            number(low, high)
        doubles(4*(1+64), True)
    assert pos == len(raw), (pos, len(raw), temp, evap, file_mode)
    return dict(start=start, bindings=bindings, floats=floats, flags=flags,
                last_state=state_start+14*8, count=count)


class Engine(RunoffEngine):
    def __init__(self, *args):
        super().__init__(*args)
        for name, args, result in (
            ("es_test_climate_save", [c.c_void_p, c.c_size_t, c.POINTER(c.c_size_t)], c.c_int),
            ("es_test_climate_restore", [c.c_void_p, c.c_size_t, c.c_int], c.c_int),
            ("es_test_climate_poison", [c.c_int], None),
            ("es_test_climate_bundle_poison", [], None),
            ("es_test_climate_observe", [c.c_int], c.c_double),
        ):
            f = getattr(self.lib, name); f.argtypes, f.restype = args, result

    def dump(self):
        size = c.c_size_t(); self.check(self.lib.es_test_climate_save(None, 0, c.byref(size)))
        data = c.create_string_buffer(size.value)
        self.check(self.lib.es_test_climate_save(data, size.value, c.byref(size)))
        return data.raw

    def restore(self, raw, fail_at=-1):
        return self.lib.es_test_climate_restore(c.create_string_buffer(bytes(raw)), len(raw), fail_at)


@unittest.skipUnless(os.environ.get("EASYSEWER_CHECKPOINT_STANDARD") and
                     os.environ.get("EASYSEWER_CHECKPOINT_CUSTOM"),
                     "Requires instrumented climate-state libraries")
class NativeCheckpointClimateTests(unittest.TestCase):
    def run_case(self, family, text, action=None, *, mode="step"):
        with tempfile.TemporaryDirectory() as root:
            e = Engine(os.environ["EASYSEWER_CHECKPOINT_"+family.upper()], root, text)
            history, observations = [], []
            try:
                climate(e.dump())
                for index in range(10000):
                    raw = e.dump()
                    if action == "restore":
                        e.lib.es_test_climate_bundle_poison()
                        self.assertEqual(e.restore(raw), 0); self.assertEqual(e.dump(), raw)
                    elif isinstance(action, int): e.lib.es_test_climate_poison(action)
                    if mode == "stride":
                        elapsed = c.c_double(); e.check(e.lib.swmm_stride(7501, c.byref(elapsed))); now = elapsed.value
                    elif mode == "split":
                        e.check(e.lib.swmm_execRouting()); self.assertEqual(e.restore(raw), 5)
                        e.check(e.lib.swmm_saveResults()); frame = clocks(e.dump())
                        now = frame["routing"]/86400000 if frame["routing"] < frame["duration"] else 0
                    else: now = e.step()[0]
                    history.append((now, *(e.lib.swmm_getValue(code, 0) for code in (303, 305, 306, 307, 308, 407, 410))))
                    observations.append(tuple(e.lib.es_test_climate_observe(k) for k in range(27)))
                    if not now:
                        if action == "restore":
                            raw = e.dump(); e.lib.es_test_climate_bundle_poison()
                            self.assertEqual(e.restore(raw), 0); self.assertEqual(e.dump(), raw)
                        break
                else: self.fail("Climate fixture did not finish")
            finally: e.close()
            report = re.sub(rb"(?m)^[ \t]*(?:Analysis (?:begun|ended)|Total elapsed time)[^\r\n]*", b"", e.paths[1].read_bytes())
            return (history, e.paths[2].read_bytes(), report), observations

    def test_sources_calendar_buffers_and_moving_average(self):
        cases = [dict(evaporation=kind, temperature="series" if kind not in ("FILE", "TEMPERATURE") else "file")
                 for kind in ("CONSTANT", "MONTHLY", "TIMESERIES", "FILE", "TEMPERATURE")]
        cases += [dict(format=f) for f in ("GHCND", "TD3200", "DLY0204")]
        cases += [dict(format="GHCND", label=label) for label in ("C", "F")]
        cases += [dict(evaporation="TEMPERATURE", days=10), dict(eof=True), dict(missing=True),
                  dict(shifted=True), dict(leap=True), dict(units="CMS"), dict(snow=True),
                  dict(dry_only=True), dict(temperature="series"), dict(no_catchment=True),
                  dict(evaporation="TIMESERIES", temperature="series", file_series=True),
                  dict(evaporation="TIMESERIES", temperature="series", file_series=True, shared=True)]
        for family in ("standard", "custom"):
            for options in cases:
                with self.subTest(family=family, **options), tempfile.TemporaryDirectory() as root:
                    text = fixture(root, **options); expected = self.run_case(family, text)
                    self.assertEqual(self.run_case(family, text, "restore"), expected)
                    if options.get("days") == 10:
                        self.assertEqual(max(row[17] for row in expected[1]), 7)
                        self.assertEqual(set(row[18] for row in expected[1] if row[17] == 7), {0., 1., 2., 3.})
            self.assertEqual(self.run_case(family, controls(), "restore"), self.run_case(family, controls()))

    def test_omission_changes_scientific_results_and_scratch_is_rewritten(self):
        cases = [(1, dict()), (2, dict(evaporation="TIMESERIES", temperature="series")),
                 (3, dict(evaporation="TEMPERATURE", days=10)), (4, dict()), (5, dict())]
        for family in ("standard", "custom"):
            for group, options in cases:
                with self.subTest(family=family, group=group), tempfile.TemporaryDirectory() as root:
                    text = fixture(root, **options); expected = self.run_case(family, text)
                    self.assertNotEqual(self.run_case(family, text, group)[0], expected[0])
                    self.assertEqual(self.run_case(family, text, 6), expected)

    def test_stride_and_custom_split(self):
        for family, mode in (("standard", "stride"), ("custom", "stride"), ("custom", "split")):
            with tempfile.TemporaryDirectory() as root:
                for options in (dict(evaporation="TEMPERATURE", days=10),
                                dict(evaporation="TIMESERIES", temperature="series", file_series=True)):
                    text = fixture(root, **options)
                    self.assertEqual(self.run_case(family, text, "restore", mode=mode), self.run_case(family, text, mode=mode))

    def test_finite_but_inconsistent_calendar_and_window_are_rejected(self):
        for family in ("standard", "custom"):
            with tempfile.TemporaryDirectory() as root:
                e = Engine(os.environ["EASYSEWER_CHECKPOINT_"+family.upper()], root,
                           fixture(root, evaporation="TEMPERATURE", days=10))
                try:
                    raw = e.dump(); layout = climate(raw)
                    def reject_at(offset, value, format="<i"):
                        bad = bytearray(raw); struct.pack_into(format, bad, offset, value)
                        self.assertNotEqual(e.restore(bad), 0)
                        self.assertEqual(e.dump(), raw)
                    for value in (-1000000., 1000000.):
                        reject_at(layout["last_state"], value, "<d")
                    self.assertLess(layout["count"], 7)
                    reject_at(layout["flags"][1][0], (layout["count"]+1) % 7)
                    file_flags = layout["flags"][-10:]
                    # January 30 cannot be changed to February 30; even valid
                    # numbers must agree with the file's calendar and counters.
                    reject_at(file_flags[1][0], 2)
                    reject_at(file_flags[3][0], 30)
                    reject_at(file_flags[4][0], 1)
                    last_month = struct.unpack_from("<i", raw, file_flags[7][0])[0]
                    reject_at(file_flags[7][0], last_month+1)
                    bad = bytearray(raw)
                    for index in (5, 6): struct.pack_into("<i", bad, file_flags[index][0], 1)
                    self.assertNotEqual(e.restore(bad), 0)
                    self.assertEqual(e.dump(), raw)
                    self.assertEqual(e.restore(raw), 0)
                finally: e.close()

    def test_late_corruption_cannot_modify_earlier_owners(self):
        for family in ("standard", "custom"):
            with tempfile.TemporaryDirectory() as root:
                cases = [dict(evaporation="TEMPERATURE", days=10), dict(format="GHCND"),
                         dict(evaporation="TIMESERIES", temperature="series", file_series=True), None]
                for options in cases:
                    text = controls() if options is None else fixture(root, **options)
                    e = Engine(os.environ["EASYSEWER_CHECKPOINT_"+family.upper()], root, text)
                    try:
                        for _ in range(30): e.step()
                        raw = e.dump(); layout = climate(raw)
                        def reject(bad):
                            self.assertNotEqual(e.restore(bad), 0); self.assertEqual(e.dump(), raw)
                        for size in range(layout["start"], len(raw)): reject(raw[:size])
                        reject(raw+b"x")
                        for offset in layout["bindings"]:
                            bad = bytearray(raw); bad[offset] ^= 1; reject(bad)
                        for offset in layout["floats"]:
                            for value in (float("nan"), float("inf"), -float("inf")):
                                bad = bytearray(raw); struct.pack_into("<d", bad, offset, value); reject(bad)
                        for offset, low, high in layout["flags"]:
                            for value in (low-1, *((high+1,) if high < 2147483647 else ())):
                                bad = bytearray(raw); struct.pack_into("<i", bad, offset, value); reject(bad)
                        if "[CONTROLS]" in text:
                            self.assertEqual(e.restore(raw, 0), 6); self.assertEqual(e.dump(), raw)
                        self.assertEqual(e.restore(raw), 0); self.assertEqual(e.dump(), raw)
                    finally: e.close()
