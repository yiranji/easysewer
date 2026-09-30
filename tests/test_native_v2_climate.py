"""Climate trajectory checks using native solver and independently exposed OUT data."""

from dataclasses import fields, replace
from datetime import date, timedelta
from itertools import product
from pathlib import Path
import tempfile
import unittest

from easysewer import get_native_capabilities
from easysewer.io.inp import InpDocument
from easysewer.model import Model, Ref
from easysewer.model import climate as c
from easysewer.model.resources import Pattern
from easysewer.model.values import FileReference
from test_climate_v2 import climate_model, add_series


def rebuild(model):
    result = Model()
    result.update_options(**{field.name: getattr(model.options, field.name) for field in fields(model.options)})
    result.update_climate(**{field.name: getattr(model.climate, field.name) for field in fields(model.climate)})
    for name in ("nodes", "links", "timeseries", "patterns"):
        for record in getattr(model, name).values():
            getattr(result, name).add(record)
    return result


def user_weather(path, *, units="CFS", minimum=30, maximum=50, evaporation=.2, wind=8):
    rows = []
    for day in range(45):
        when = date(2020, 1, 1) + timedelta(days=day)
        low, high, evap = minimum, maximum, evaporation
        if units in ("CMS", "LPS", "MLD"):
            low, high, evap = (low - 32) * 5 / 9, (high - 32) * 5 / 9, evap * 25.4
        # Native USER_PREPARED wind is read as mph even in SI. Separate tests
        # check this deviation rather than concealing it in a conversion helper.
        rows.append(f"Station {when.year} {when.month} {when.day} {high!r} {low!r} {evap!r} {wind!r}")
    Path(path).write_text("\n".join(rows) + "\n", encoding="ascii")


def ghcnd_weather(path, units, *, minimum=30, maximum=50, evap=.2, wind=8):
    labels = ("STATION", "DATE", "TMAX", "TMIN", "EVAP", "AWND")
    widths = (18, 12, 12, 12, 12, 12)
    text = "".join(label.ljust(width) for label, width in zip(labels, widths)) + "\n"
    text += "".join(("-" * len(label)).ljust(width) for label, width in zip(labels, widths)) + "\n"
    high, low = maximum, minimum
    if units != "F":
        high, low, evap, wind = (high - 32) * 5 / 9, (low - 32) * 5 / 9, evap * 25.4, wind / .62137 / 3.6
        if units == "C10":
            high, low, evap, wind = high * 10, low * 10, evap * 10, wind * 10
    for day in range(45):
        when = date(2020, 1, 1) + timedelta(days=day)
        values = ("Station", when.strftime("%Y%m%d"), *(f"{value:.5g}" for value in (high, low, evap, wind)))
        text += "".join(value.ljust(width) for value, width in zip(values, widths)) + "\n"
    Path(path).write_text(text, encoding="ascii")


SNOW_CATCHMENT = """[RAINGAGES]
R INTENSITY 1:00 1 TIMESERIES Rain
[TIMESERIES]
Rain 0:00 0
Rain 200:00 0
[SUBCATCHMENTS]
S R J 2 30 100 1 0 Snow
[SUBAREAS]
S .01 .2 .01 .1 25 OUTLET
[INFILTRATION]
S 3 .5 4 7 0
[SNOWPACKS]
Snow PLOWABLE .001 .004 32 .1 1 0 .5
Snow IMPERVIOUS .001 .004 32 .1 1 0 4
Snow PERVIOUS .001 .004 32 .1 1 0 4
"""

DRY_CATCHMENT = """[RAINGAGES]
R INTENSITY 1:00 1 TIMESERIES Rain
[TIMESERIES]
Rain 0:00 0
Rain 200:00 0
[SUBCATCHMENTS]
S R J 2 100 100 1 0
[SUBAREAS]
S .01 .2 .01 .1 25 OUTLET
[INFILTRATION]
S 3 .5 4 7 0
"""


@unittest.skipUnless(get_native_capabilities()["swmm_solver"] and get_native_capabilities()["swmm_output"], "Native solver/output unavailable")
class NativeClimateTests(unittest.TestCase):
    def solve(self, directory, name, source, *, allow_error=False, snow=False):
        from easysewer.runtime._solver_api import SWMMSolverAPI
        from easysewer.runtime._output_api import SWMMOutputAPI
        base = Path(directory) / name
        inp, rpt, out = (base.with_suffix(suffix) for suffix in (".inp", ".rpt", ".out"))
        # Native writes temperature/PET while saving runoff results. A dry
        # catchment enables that actual path without inventing nonzero rainfall.
        if "[SUBCATCHMENTS]" not in source:
            source += DRY_CATCHMENT
        inp.write_text(source + "[REPORT]\nSUBCATCHMENTS ALL\nNODES ALL\nLINKS ALL\n", encoding="utf-8")
        solver = SWMMSolverAPI()
        if solver.get_version() != 52004:
            self.skipTest("Climate fixtures require SWMM 5.2.4")
        started = False
        try:
            error = solver.open(str(inp), str(rpt), str(out))
            if not error:
                error = solver.start(1)
                started = not error
            if error:
                solver.close()
                if allow_error:
                    return error, rpt.read_text(errors="replace")
                self.fail(f"SWMM error {error}: {rpt.read_text(errors='replace')}")
            history = []
            for _ in range(15000):
                error, elapsed = solver.step()
                self.assertEqual(error, 0)
                if elapsed == 0:
                    break
                history.append((elapsed, solver.get_value(303, solver.get_index(2, "J")),
                                solver.get_value(305, solver.get_index(2, "J"))))
            else:
                self.fail("Climate fixture exceeded step bound")
            self.assertEqual(solver.end(), 0)
            started = False
            balance = solver.get_mass_bal_err()
        finally:
            if started:
                solver.end()
            solver.close()
        with SWMMOutputAPI() as output:
            output.open(str(out))
            periods = output.get_times(1)
            self.assertGreater(periods, 40)
            # API endPeriod is exclusive. Solver output.c defines PET as 14;
            # the older outfile enum ends at actual evaporation (13).
            temperature = output.get_system_series(0, 0, periods)
            evaporation = output.get_system_series(14, 0, periods)
            snow_depth = output.get_system_series(2, 0, periods)
            rainfall = output.get_system_series(1, 0, periods)
            actual_evaporation = output.get_system_series(13, 0, periods)
            runoff = output.get_subcatch_series(0, 4, 0, periods) if snow else []
            self.assertEqual(len(temperature), periods)
        return dict(history=history, balance=balance, temperature=temperature, evaporation=evaporation,
                    actual_evaporation=actual_evaporation, rainfall=rainfall, snow=snow_depth, runoff=runoff)

    def test_temperature_series_negative_values_monthly_adjustments_and_unit_conversion(self):
        with tempfile.TemporaryDirectory() as directory:
            model = climate_model()
            air = add_series(model, "Air", ((0, -30), (24, 40), (48, 50), (72, 20)))
            model.update_climate(temperature=c.SeriesTemperature(series=air), evaporation=c.Evaporation(source=c.ConstantEvaporation(rate=.2)),
                adjustments=c.ClimateAdjustments(temperature=c.MonthlyTemperatureChanges(values=(9, -9, *(0.0,) * 10))))
            expected = self.solve(directory, "us", model.to_document().text)
            self.assertLess(min(expected["temperature"]), 0)
            self.assertGreater(max(expected["temperature"]), 40)
            for units in ("GPM", "MGD", "CMS", "LPS", "MLD"):
                converted = model.copy()
                converted.convert_units(units)
                actual = self.solve(directory, "converted", converted.to_document().text)
                for before, after in zip(expected["temperature"], actual["temperature"]):
                    self.assertAlmostEqual(after, (before - 32) * 5 / 9 if units in ("CMS", "LPS", "MLD") else before, delta=4e-6)
                for before, after in zip(expected["evaporation"], actual["evaporation"]):
                    self.assertAlmostEqual(after, before * 25.4 if units in ("CMS", "LPS", "MLD") else before, delta=1e-6)

    def test_five_evaporation_sources_and_month_changes_match_rebuilt_model(self):
        with tempfile.TemporaryDirectory() as directory:
            for units, kind in product(("CFS", "CMS"), ("CONSTANT", "MONTHLY", "TIMESERIES", "TEMPERATURE", "FILE")):
                with self.subTest(units=units, source=kind):
                    model = climate_model()
                    model.reinterpret_units(units)
                    weather = Path(directory) / "weather data.dat"
                    user_weather(weather, units=units)
                    model.update_climate(temperature=c.FileTemperature(), file=c.ClimateFile(file=FileReference(path=str(weather))))
                    rate = .2 if units == "CFS" else 5.08
                    series = add_series(model, "Evap", ((0, rate), (12, rate * 2), (36, rate / 2), (72, rate)))
                    model.patterns.add(Pattern(id="Recovery", kind="MONTHLY", factors=(.8, .6)))
                    sources = {"CONSTANT": c.ConstantEvaporation(rate=rate), "MONTHLY": c.MonthlyEvaporation(values=(rate, rate * 2, *(rate,) * 10)),
                        "TIMESERIES": c.SeriesEvaporation(series=series), "TEMPERATURE": c.TemperatureEvaporation(),
                        "FILE": c.FileEvaporation(pan_coefficients=c.MonthlyFactors(values=(.7, .9, *(1.0,) * 10)))}
                    model.update_climate(evaporation=c.Evaporation(source=sources[kind], recovery_pattern=Ref(collection="swmm:patterns", key="Recovery"), dry_only=False),
                        adjustments=c.ClimateAdjustments(evaporation=c.MonthlyEvaporation(values=(0.01 if units == "CFS" else .254,) * 12)))
                    source = model.to_document().text
                    parsed = Model.from_document(InpDocument.from_text(source), strict=True)
                    expected = self.solve(directory, "original", source)
                    actual = self.solve(directory, "rebuilt", rebuild(parsed).to_document().text)
                    self.assertEqual(actual, expected)
                    self.assertGreater(max(expected["evaporation"]), 0)
                    self.assertLess(expected["history"][-1][2], expected["history"][0][2])

    def test_temperature_file_and_series_order_retains_evaporation_file(self):
        with tempfile.TemporaryDirectory() as directory:
            weather = Path(directory) / "weather.dat"
            user_weather(weather, minimum=25, maximum=25)
            model = climate_model()
            air = add_series(model, "Air", ((0, 50), (72, 50)))
            model.update_climate(file=c.ClimateFile(file=FileReference(path=str(weather))), temperature=c.SeriesTemperature(series=air),
                                 wind=c.FileWind(), evaporation=c.Evaporation(source=c.FileEvaporation()))
            source = model.to_document().text
            rows = InpDocument.from_text(source).records("TEMPERATURE")
            file_row, series_row = rows[0].content, rows[1].content
            for selected, text in ((50, source), (25, source.replace(file_row + "\n" + series_row, series_row + "\n" + file_row))):
                parsed = Model.from_document(InpDocument.from_text(text), strict=True)
                original = self.solve(directory, "original", text)
                self.assertEqual(self.solve(directory, "rebuilt", rebuild(parsed).to_document().text), original)
                self.assertTrue(all(abs(value - selected) < 1e-5 for value in original["temperature"]))
                self.assertTrue(all(abs(value - .2) < 1e-6 for value in original["evaporation"]))

    def test_bare_file_evaporation_native_error_and_explicit_default_repair(self):
        with tempfile.TemporaryDirectory() as directory:
            weather = Path(directory) / "weather.dat"
            user_weather(weather)
            model = climate_model()
            source = model.to_document().text + f'[TEMPERATURE]\nFILE "{weather}"\n[EVAPORATION]\nFILE\n'
            parsed = Model.from_document(InpDocument.from_text(source), strict=True)
            error, report = self.solve(directory, "bare", source, allow_error=True)
            self.assertNotEqual(error, 0)
            self.assertIn("ERROR 203", report)
            self.assertIn("climate.native_file_evap_syntax", {d.code for d in parsed.validate(for_run=True).errors})
            self.assertTrue(parsed.validate(for_run=True, normalize=True).is_valid)
            repaired = self.solve(directory, "fixed", parsed.to_document(normalize=True).text)
            self.assertTrue(all(abs(value - .2) < 1e-6 for value in repaired["evaporation"]))

    def test_ghcnd_file_units_and_model_dependent_default(self):
        with tempfile.TemporaryDirectory() as directory:
            for flow_units, file_units in product(("CFS", "CMS"), (None, "F", "C", "C10")):
                with self.subTest(model=flow_units, file=file_units):
                    effective = file_units or ("F" if flow_units == "CFS" else "C")
                    path = Path(directory) / "ghcnd.dat"
                    ghcnd_weather(path, effective, minimum=32, maximum=32, evap=.2)
                    model = climate_model()
                    model.reinterpret_units(flow_units)
                    model.update_climate(file=c.ClimateFile(file=FileReference(path=str(path)), units=file_units),
                        wind=c.FileWind(), evaporation=c.Evaporation(source=c.FileEvaporation()))
                    source = model.to_document().text
                    expected = self.solve(directory, "ghcnd", source)
                    self.assertTrue(all(abs(value - (32 if flow_units == "CFS" else 0)) < 1e-6 for value in expected["temperature"]))
                    self.assertTrue(all(abs(value - (.2 if flow_units == "CFS" else 5.08)) < 1e-6 for value in expected["evaporation"]))
                    parsed = Model.from_document(InpDocument.from_text(source), strict=True)
                    self.assertEqual(self.solve(directory, "rebuilt", rebuild(parsed).to_document().text), expected)

    def test_wind_snowmelt_and_depletion_change_snow_processes(self):
        with tempfile.TemporaryDirectory() as directory:
            model = climate_model()
            air = add_series(model, "Air", ((0, 45), (72, 45)))
            model.update_climate(temperature=c.SeriesTemperature(series=air), wind=c.MonthlyWindSpeeds(values=(8.0,) * 12),
                snowmelt=c.Snowmelt(snowfall_temperature=34, antecedent_weight=.5, negative_melt_ratio=.6, elevation=150, latitude=35, solar_time_correction=-10),
                impervious_depletion=c.ArealDepletion(fractions=tuple(i / 10 for i in range(10))),
                pervious_depletion=c.ArealDepletion(fractions=tuple(i / 10 for i in range(10))))
            source = model.to_document().text
            expected = self.solve(directory, "snow", source + SNOW_CATCHMENT, snow=True)
            parsed = Model.from_document(InpDocument.from_text(source), strict=True)
            self.assertEqual(self.solve(directory, "rebuilt", rebuild(parsed).to_document().text + SNOW_CATCHMENT, snow=True), expected)
            self.assertGreater(max(expected["runoff"]), 0)
            model.update_climate(impervious_depletion=None, pervious_depletion=None)
            full = self.solve(directory, "full_cover", model.to_document().text + SNOW_CATCHMENT, snow=True)
            self.assertNotEqual(full["snow"], expected["snow"])

    def test_file_start_date_and_missing_days_follow_native_calendar(self):
        with tempfile.TemporaryDirectory() as directory:
            weather = Path(directory) / "dated.dat"
            rows = []
            for day in range(45):
                when = date(2020, 1, 1) + timedelta(days=day)
                rows.append(f"Station {when.year} {when.month} {when.day} 40 40 {(day + 1) / 100} 8")
            weather.write_text("\n".join(rows) + "\n", encoding="ascii")
            model = climate_model()
            model.update_climate(file=c.ClimateFile(file=FileReference(path=str(weather))),
                                 evaporation=c.Evaporation(source=c.FileEvaporation()))
            default = self.solve(directory, "default_date", model.to_document().text)
            for hour, expected in ((11, .30), (35, .31), (59, .32)):
                self.assertAlmostEqual(default["evaporation"][hour], expected, delta=1e-6)
            model.update_climate(file=replace(model.climate.file, start_date=date(2020, 1, 1)))
            explicit = self.solve(directory, "explicit_date", model.to_document().text)
            for hour, expected in ((11, .01), (35, .02), (59, .03)):
                self.assertAlmostEqual(explicit["evaporation"][hour], expected, delta=1e-6)
            parsed = Model.from_document(model.to_document(), strict=True)
            self.assertEqual(self.solve(directory, "rebuilt_date", rebuild(parsed).to_document().text), explicit)
            weather.write_text("\n".join(row for row in rows if not row.startswith("Station 2020 1 31 ")) + "\n", encoding="ascii")
            model.update_climate(file=replace(model.climate.file, start_date=None))
            missing = self.solve(directory, "missing_date", model.to_document().text)
            self.assertAlmostEqual(missing["evaporation"][35], .30, delta=1e-6)
            self.assertAlmostEqual(missing["evaporation"][59], .32, delta=1e-6)

    def test_user_file_wind_is_mph_even_in_si_and_units_label_is_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            for units in ("CFS", "CMS"):
                with self.subTest(units=units):
                    weather = Path(directory) / "wind.dat"
                    user_weather(weather, units=units, minimum=45, maximum=45, wind=8)
                    model = climate_model()
                    model.reinterpret_units(units)
                    model.update_climate(file=c.ClimateFile(file=FileReference(path=str(weather))), wind=c.FileWind())
                    catchment = SNOW_CATCHMENT.replace("Rain 0:00 0", "Rain 0:00 .1\nRain 24:00 0")
                    if units == "CMS":
                        catchment = catchment.replace("Rain 0:00 .1", "Rain 0:00 2.54").replace("32 .1 1 0", "0 .1 25.4 0")
                    expected = self.solve(directory, "file_wind", model.to_document().text + catchment, snow=True)
                    model.update_climate(file=replace(model.climate.file, units="C10"))
                    self.assertEqual(self.solve(directory, "ignored_label", model.to_document().text + catchment, snow=True), expected)
                    model.update_climate(wind=c.MonthlyWindSpeeds(values=(8 if units == "CFS" else 8 * 1.608,) * 12))
                    monthly = self.solve(directory, "monthly_wind", model.to_document().text + catchment, snow=True)
                    self.assertEqual(monthly, expected)
                    model.update_climate(wind=c.MonthlyWindSpeeds(values=(0.0,) * 12))
                    still = self.solve(directory, "still_air", model.to_document().text + catchment, snow=True)
                    self.assertNotEqual(still["snow"], expected["snow"])

    def test_temperature_evaporation_requires_active_file_temperature(self):
        with tempfile.TemporaryDirectory() as directory:
            weather = Path(directory) / "temperature.dat"
            user_weather(weather, minimum=30, maximum=50, evaporation=.75)
            model = climate_model()
            model.update_climate(file=c.ClimateFile(file=FileReference(path=str(weather))),
                                 evaporation=c.Evaporation(source=c.TemperatureEvaporation()))
            hargreaves = self.solve(directory, "hargreaves", model.to_document().text)
            air = add_series(model, "Air", ((0, 40), (72, 40)))
            model.update_climate(temperature=c.SeriesTemperature(series=air))
            self.assertIn("climate.temperature_evap_source", {d.code for d in model.validate(for_run=True).errors})
            series = self.solve(directory, "series_evap", model.to_document().text)
            self.assertTrue(all(abs(value - .75) < 1e-6 for value in series["evaporation"]))
            self.assertNotEqual(series["evaporation"], hargreaves["evaporation"])

    def test_rainfall_adjustment_and_dry_only_change_actual_catchment_water_balance(self):
        with tempfile.TemporaryDirectory() as directory:
            model = climate_model()
            # Rain on every day; one hourly observation represents each wet hour.
            catchment = DRY_CATCHMENT.replace("Rain 0:00 0", "\n".join(f"Rain {hour}:00 .1" for hour in range(72)))
            model.update_climate(evaporation=c.Evaporation(source=c.ConstantEvaporation(rate=1), dry_only=False))
            wet = self.solve(directory, "wet_evap", model.to_document().text + catchment)
            model.update_climate(evaporation=replace(model.climate.evaporation, dry_only=True))
            dry = self.solve(directory, "dry_only", model.to_document().text + catchment)
            self.assertEqual(wet["evaporation"], dry["evaporation"])
            self.assertLess(sum(dry["actual_evaporation"]), sum(wet["actual_evaporation"]))
            model.update_climate(adjustments=c.ClimateAdjustments(rainfall=c.MonthlyFactors(values=(.5, 2, *(1.0,) * 10))))
            adjusted = self.solve(directory, "adjusted_rain", model.to_document().text + catchment)
            for hour, factor in ((11, .5), (59, 2)):
                self.assertAlmostEqual(adjusted["rainfall"][hour], dry["rainfall"][hour] * factor, delta=1e-6)

    def test_conductivity_and_recovery_factors_affect_infiltration(self):
        with tempfile.TemporaryDirectory() as directory:
            model = climate_model()
            storms = "\n".join(f"Rain {hour}:00 1" for hour in (*range(6), *range(48, 54)))
            catchment = (DRY_CATCHMENT.replace("Rain 0:00 0", storms)
                         .replace("S R J 2 100", "S R J 2 0").replace("S 3 .5 4 7 0", "S 1 .02 4 1 0"))
            baseline = self.solve(directory, "baseline_infiltration", model.to_document().text + catchment, snow=True)
            model.patterns.add(Pattern(id="Recovery", kind="MONTHLY", factors=(0, 0)))
            model.update_climate(evaporation=c.Evaporation(recovery_pattern=Ref(collection="swmm:patterns", key="Recovery")))
            no_recovery = self.solve(directory, "no_recovery", model.to_document().text + catchment, snow=True)
            self.assertEqual(no_recovery["runoff"][:24], baseline["runoff"][:24])
            self.assertGreater(sum(no_recovery["runoff"][48:]), sum(baseline["runoff"][48:]))
            model.update_climate(evaporation=None, adjustments=c.ClimateAdjustments(conductivity=c.MonthlyFactors(values=(.5,) * 12)))
            reduced = self.solve(directory, "reduced_conductivity", model.to_document().text + catchment, snow=True)
            self.assertGreater(sum(reduced["runoff"]), sum(baseline["runoff"]))
            model.update_climate(adjustments=c.ClimateAdjustments(conductivity=c.MonthlyFactors(values=(0, -1, *(1.0,) * 10))))
            defaulted = self.solve(directory, "defaulted_conductivity", model.to_document().text + catchment, snow=True)
            self.assertEqual(defaulted, baseline)

    def test_resolved_snow_defaults_match_implicit_engine_behavior(self):
        with tempfile.TemporaryDirectory() as directory:
            weather = Path(directory) / "defaults.dat"
            user_weather(weather, minimum=30, maximum=50)
            model = climate_model()
            model.update_climate(file=c.ClimateFile(file=FileReference(path=str(weather))),
                                 evaporation=c.Evaporation(source=c.TemperatureEvaporation()))
            implicit = self.solve(directory, "implicit", model.to_document().text + SNOW_CATCHMENT, snow=True)
            resolved = model.effective_climate
            model.update_climate(snowmelt=resolved.snowmelt, wind=resolved.wind,
                                 impervious_depletion=resolved.impervious_depletion, pervious_depletion=resolved.pervious_depletion)
            explicit = self.solve(directory, "explicit", model.to_document().text + SNOW_CATCHMENT, snow=True)
            self.assertEqual(explicit, implicit)
            model.update_climate(snowmelt=replace(resolved.snowmelt, latitude=50))
            manual_default = self.solve(directory, "manual_default", model.to_document().text + SNOW_CATCHMENT, snow=True)
            self.assertNotEqual(manual_default["temperature"], implicit["temperature"])
            self.assertNotEqual(manual_default["evaporation"], implicit["evaporation"])


if __name__ == "__main__":
    unittest.main()
