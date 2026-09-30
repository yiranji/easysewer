"""Behavioral tests of fully structured rainfall/catchment/snow input chains."""

from dataclasses import replace
from datetime import date, timedelta
from itertools import product
from pathlib import Path
import tempfile
import unittest

from easysewer import get_native_capabilities
from easysewer.io.inp import InpDocument
from easysewer.model import Model, Ref
from easysewer.model import hydrology as h, climate as c
from easysewer.model.resources import Pattern
from easysewer.model.values import FileReference
from test_climate_v2 import climate_model, add_series
from test_hydrology_v2 import hydrology_model, snowpack, rebuild, INFILTRATION
import test_native_v2_climate as native_climate


def handwritten(method, form, units, routing):
    base = climate_model()
    base.reinterpret_units(units)
    rain = "\n".join(f"Rain {hour}:00 {'.6' if 2 <= hour < 8 or 48 <= hour < 52 else '0'}" for hour in range(73))
    return (base.to_document().text + f"[OPTIONS]\nINFILTRATION {method}\n"
        f"[RAINGAGES]\nR {form} 1 1 TIMESERIES Rain\n[TIMESERIES]\n{rain}\n"
        "[SUBCATCHMENTS]\nS R J 2 30 100 1 12\n"
        f"[SUBAREAS]\nS .01 .2 .05 .1 25 {routing} 50\n[INFILTRATION]\nS {INFILTRATION[method]}\n")


@unittest.skipUnless(get_native_capabilities()["swmm_solver"] and get_native_capabilities()["swmm_output"], "Native solver/output unavailable")
class NativeHydrologyTests(unittest.TestCase):
    def solve(self, directory, name, source, *, catchments=1, allow_error=False):
        result = native_climate.NativeClimateTests.solve(self, directory, name, source, allow_error=allow_error, snow=True)
        if isinstance(result, tuple):
            return result
        from easysewer.runtime._output_api import SWMMOutputAPI
        with SWMMOutputAPI() as output:
            output.open(str(Path(directory) / (name + ".out")))
            periods = output.get_times(1)
            result["catchments"] = tuple(tuple(output.get_subcatch_series(index, attr, 0, periods) for attr in range(5)) for index in range(catchments))
        return result

    def test_all_infiltration_rainfall_and_subarea_variants_match_fresh_models(self):
        with tempfile.TemporaryDirectory() as directory:
            for method, form, units, routing in product(INFILTRATION, ("INTENSITY", "VOLUME", "CUMULATIVE"), ("CFS", "CMS"), ("OUTLET", "PERVIOUS", "IMPERVIOUS")):
                with self.subTest(method=method, form=form, units=units, routing=routing):
                    source = handwritten(method, form, units, routing)
                    model = Model.from_document(InpDocument.from_text(source), strict=True)
                    self.assertTrue(model.validate(for_run=True).is_valid)
                    expected = self.solve(directory, "source", source)
                    actual = self.solve(directory, "rebuilt", rebuild(model).to_document().text)
                    self.assertEqual(actual, expected)
                    self.assertGreater(max(expected["runoff"]), 0)

    def test_full_hydrology_unit_conversion_for_all_methods_and_snow(self):
        with tempfile.TemporaryDirectory() as directory:
            for method in INFILTRATION:
                model = hydrology_model(method)
                model.snowpacks.add(snowpack())
                model.subcatchments.update("S", snowpack=Ref(collection="swmm:snowpacks", key="Snow"))
                air = add_series(model, "Air", ((0, 25), (24, 25), (48, 45), (72, 45)))
                model.update_climate(temperature=c.SeriesTemperature(series=air), wind=c.MonthlyWindSpeeds(values=(8.,) * 12))
                expected = self.solve(directory, "us", model.to_document().text)
                self.assertGreater(max(expected["snow"]), 0)
                self.assertGreater(max(expected["runoff"]), 0)
                for units in ("GPM", "MGD", "CMS", "LPS", "MLD"):
                    with self.subTest(method=method, units=units):
                        converted = model.copy()
                        converted.convert_units(units)
                        actual = self.solve(directory, "converted", converted.to_document().text)
                        factors = dict(model.profile.unit_rules.flow_from_cfs)
                        for attr, (before, after) in enumerate(zip(expected["catchments"][0], actual["catchments"][0])):
                            factor = factors[units] if attr == 4 else 25.4 if units in ("CMS", "LPS", "MLD") else 1
                            for x, y in zip(before, after):
                                self.assertAlmostEqual(y / factor, x, delta=2e-6 * max(1, abs(x)))
                physical = model.copy()
                physical.convert_units("CMS", basis="physical")
                actual = self.solve(directory, "physical", physical.to_document().text)
                self.assertGreater(max(abs(y / 25.4 - x) for x, y in zip(expected["snow"], actual["snow"])), .01)

    def test_three_rain_forms_represent_the_same_half_hour_storm(self):
        with tempfile.TemporaryDirectory() as directory:
            results = []
            for form in ("INTENSITY", "VOLUME", "CUMULATIVE"):
                model = hydrology_model(form=form)
                points = []
                for index in range(145):
                    wet = 4 <= index < 16
                    value = .6 if form == "INTENSITY" else .3 if form == "VOLUME" else .3 * (index-3)
                    points.append((index / 2, value if wet else 0))
                from easysewer.model.resources import SeriesPoint
                model.timeseries.update("Rain", points=tuple(SeriesPoint(time=timedelta(hours=hour), value=value) for hour, value in points))
                model.raingages.update("R", interval=timedelta(minutes=30))
                results.append(self.solve(directory, form, model.to_document().text))
            for result in results[1:]:
                for attr in range(5):
                    for x, y in zip(results[0]["catchments"][0][attr], result["catchments"][0][attr]):
                        self.assertAlmostEqual(x, y, delta=1e-6)

    def test_local_adjustments_survive_catchment_edits_and_pattern_renames(self):
        with tempfile.TemporaryDirectory() as directory:
            model = hydrology_model()
            for name, factors in (("Soil", (.2, .7)), ("Storage", (2, .5)), ("Roughness", (3, .5))):
                model.patterns.add(Pattern(id=name, kind="MONTHLY", factors=factors))
            model.subcatchment_adjustments.add(c.SubcatchmentAdjustments(subcatchment=Ref(collection="swmm:subcatchments", key="S"),
                infiltration=Ref(collection="swmm:patterns", key="Soil"), depression_storage=Ref(collection="swmm:patterns", key="Storage"),
                pervious_roughness=Ref(collection="swmm:patterns", key="Roughness")))
            source = model.to_document().text
            expected = self.solve(directory, "original", source)
            parsed = Model.from_document(InpDocument.from_text(source), strict=True)
            parsed.subcatchments.rename("S", "Renamed")
            parsed.patterns.rename("Soil", "SeasonalSoil")
            parsed.raingages.rename("R", "Station")
            self.assertEqual(self.solve(directory, "edited", parsed.to_document().text), expected)
            for field in ("infiltration", "depression_storage", "pervious_roughness"):
                separate = parsed.copy()
                separate.subcatchment_adjustments.update("Renamed", **{field: None})
                changed = self.solve(directory, field, separate.to_document().text)
                self.assertNotEqual(changed["catchments"], expected["catchments"])
            parsed.subcatchment_adjustments.remove("Renamed")
            unadjusted = self.solve(directory, "unadjusted", parsed.to_document().text)
            self.assertNotEqual(unadjusted["catchments"], expected["catchments"])

    def test_native_order_resets_adjustments_and_subarea_fractions(self):
        with tempfile.TemporaryDirectory() as directory:
            model = hydrology_model()
            model.patterns.add(Pattern(id="Pat", kind="MONTHLY", factors=(.1, .1)))
            model.subcatchment_adjustments.add(c.SubcatchmentAdjustments(subcatchment=Ref(collection="swmm:subcatchments", key="S"), infiltration=Ref(collection="swmm:patterns", key="Pat")))
            document = model.to_document()
            expected = self.solve(directory, "correct", document.text)
            for section in ("SUBAREAS", "ADJUSTMENTS"):
                row = document.records(section)[0].content
                source = f"[{section}]\n{row}\n" + document.text.replace(row + "\n", "")
                parsed = Model.from_document(InpDocument.from_text(source), strict=True)
                self.assertIn("subcatchment.native_order", {d.code for d in parsed.validate(for_run=True).errors})
                wrong = self.solve(directory, "wrong", source)
                self.assertNotEqual(wrong["catchments"], expected["catchments"])
                fixed = self.solve(directory, "normalized", parsed.to_document(normalize=True).text)
                self.assertEqual(fixed, expected)

    def test_file_rainfall_retains_its_own_units_after_export_and_model_conversion(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "old").mkdir()
            (root / "new").mkdir()
            for units in ("IN", "MM"):
                rows = []
                for hour in range(73):
                    when = date(2020, 1, 30) + timedelta(days=hour//24)
                    rain = .6 if 2 <= hour < 8 else 0
                    if units == "MM":
                        rain *= 25.4
                    rows.append(f"Station {when.year} {when.month} {when.day} {hour%24} 0 {rain}")
                weather = root / "old" / "rain data.dat"
                weather.write_text("\n".join(rows) + "\n", encoding="ascii")
                model = hydrology_model()
                model.raingages.update("R", source=h.FileRainfall(file=FileReference(path=str(weather)), station="Station", units=units, start_date=date(2020, 1, 30)))
                model.timeseries.remove("Rain")
                expected = self.solve(directory, "file_us", model.to_document().text)
                model.to_inp(root / "old" / "rain.inp")
                parsed = Model.from_inp(root / "old" / "rain.inp", strict=True)
                parsed.to_inp(root / "new" / "rain.inp")
                moved = Model.from_inp(root / "new" / "rain.inp", strict=True)
                # Export paths are relative to the actual INP location; generate
                # absolute references for the solver helper's scratch location.
                moved.to_inp(root / "moved.inp", path_policy="absolute")
                self.assertEqual(self.solve(directory, "moved", (root / "moved.inp").read_text()), expected)
                model.convert_units("CMS")
                si = self.solve(directory, "file_si", model.to_document().text)
                for attr, (before, after) in enumerate(zip(expected["catchments"][0], si["catchments"][0])):
                    factor = .02832 if attr == 4 else 25.4
                    for x, y in zip(before, after):
                        self.assertAlmostEqual(x, y / factor, delta=2e-6 * max(1, abs(x)))
                self.assertGreater(max(expected["runoff"]), 0)

    def test_snow_removal_all_destinations_and_documented_missing_fsub(self):
        with tempfile.TemporaryDirectory() as directory:
            for destination in range(5):
                model = hydrology_model()
                model.subcatchments.add(replace(model.subcatchments["S"], id="Receiver", impervious_percent=0,
                    snowpack=Ref(collection="swmm:snowpacks", key="EmptySnow")))
                model.snowpacks.add(snowpack("EmptySnow", initial=0))
                model.subcatchments.update("S", snowpack=Ref(collection="swmm:snowpacks", key="Snow"))
                fractions = [0.] * 5
                fractions[destination] = .5
                removal = h.SnowRemoval(threshold=.1, **dict(zip(("out_of_system", "to_impervious", "to_pervious", "immediate_melt", "to_subcatchment"), fractions)),
                    destination=Ref(collection="swmm:subcatchments", key="Receiver") if destination == 4 else None)
                model.snowpacks.add(snowpack(removal=removal))
                air = add_series(model, "Air", ((0, 20), (72, 20)))
                model.update_climate(temperature=c.SeriesTemperature(series=air))
                source = model.to_document().text
                expected = self.solve(directory, "original", source, catchments=2)
                parsed = Model.from_document(InpDocument.from_text(source), strict=True)
                self.assertEqual(self.solve(directory, "rebuilt", rebuild(parsed).to_document().text, catchments=2), expected)
                if destination == 4:
                    self.assertGreater(max(expected["catchments"][1][1]), 0)
                if destination == 3:
                    self.assertGreater(max(expected["runoff"]), 0)
                if destination == 0:
                    row = InpDocument.from_text(source).records("SNOWPACKS")[-1].content
                    short = source.replace(row, row.rsplit(" ", 1)[0])
                    error, report = self.solve(directory, "short", short, allow_error=True)
                    self.assertNotEqual(error, 0)
                    self.assertIn("ERROR 203", report)
                    repaired = Model.from_document(InpDocument.from_text(short), strict=True)
                    self.assertEqual(self.solve(directory, "fixed", repaired.to_document(normalize=True).text, catchments=2), expected)

    def test_infiltration_defaults_clamps_and_ignored_cn_parameter(self):
        with tempfile.TemporaryDirectory() as directory:
            for value, effective in ((1, 10), (100, 99)):
                source = handwritten("CURVE_NUMBER", "INTENSITY", "CFS", "OUTLET").replace("S 75 0 2", f"S {value} 99 2")
                parsed = Model.from_document(InpDocument.from_text(source), strict=True)
                expected = self.solve(directory, "clamped", source)
                self.assertEqual(self.solve(directory, "rebuilt", rebuild(parsed).to_document().text), expected)
                self.assertEqual(self.solve(directory, "explicit", source.replace(f"S {value} 99 2", f"S {effective} 0 2")), expected)

    def test_catchment_and_outfall_runon_targets_survive_reconstruction_and_rename(self):
        from easysewer.model import network as n
        with tempfile.TemporaryDirectory() as directory:
            for kind in ("catchment", "outfall"):
                model = hydrology_model()
                dry = add_series(model, "Dry", ((0, 0), (72, 0)))
                model.raingages.add(h.RainGage(id="DryGage", form="INTENSITY", interval=timedelta(hours=1), snow_factor=1, source=h.SeriesRainfall(series=dry)))
                model.nodes.add(n.Outfall(id="Final", elevation=-2, boundary=n.FreeBoundary()))
                model.subcatchments.add(replace(model.subcatchments["S"], id="Receiver", rain_gage=Ref(collection="swmm:raingages", key="DryGage"),
                    outlet=Ref(collection="swmm:nodes", key="Final"), impervious_percent=100))
                if kind == "catchment":
                    model.subcatchments.update("S", outlet=Ref(collection="swmm:subcatchments", key="Receiver"))
                else:
                    model.nodes.update("O", route_to=Ref(collection="swmm:subcatchments", key="Receiver"))
                source = model.to_document().text
                expected = self.solve(directory, "runon", source, catchments=2)
                self.assertGreater(max(expected["catchments"][1][4]), 0)
                self.assertEqual(max(expected["catchments"][1][0]), 0)
                parsed = Model.from_document(InpDocument.from_text(source), strict=True)
                parsed.subcatchments.rename("Receiver", "Destination")
                self.assertEqual(self.solve(directory, "renamed", parsed.to_document().text, catchments=2), expected)
                self.assertEqual(self.solve(directory, "rebuilt", rebuild(parsed).to_document().text, catchments=2), expected)

    def test_native_snow_transfer_omits_catchment_area_ratio(self):
        from easysewer.model.resources import SeriesPoint
        with tempfile.TemporaryDirectory() as directory:
            model = hydrology_model()
            model.timeseries.update("Rain", points=(SeriesPoint(time=timedelta(), value=0), SeriesPoint(time=timedelta(hours=72), value=0)))
            model.subcatchments.add(replace(model.subcatchments["S"], id="Receiver", impervious_percent=0,
                snowpack=Ref(collection="swmm:snowpacks", key="EmptySnow")))
            model.snowpacks.add(snowpack("EmptySnow", initial=0))
            removal = h.SnowRemoval(threshold=.1, out_of_system=0, to_impervious=0, to_pervious=0, immediate_melt=0,
                to_subcatchment=.5, destination=Ref(collection="swmm:subcatchments", key="Receiver"))
            model.snowpacks.add(snowpack(removal=removal))
            model.subcatchments.update("S", snowpack=Ref(collection="swmm:snowpacks", key="Snow"))
            air = add_series(model, "Air", ((0, 0), (72, 0)))
            model.update_climate(temperature=c.SeriesTemperature(series=air))
            equal = self.solve(directory, "equal_area", model.to_document().text, catchments=2)
            model.subcatchments.update("Receiver", area=4)
            self.assertIn("snowpack.native_transfer_area", {d.code for d in model.validate(for_run=True).diagnostics})
            doubled = self.solve(directory, "double_area", model.to_document().text, catchments=2)
            self.assertGreater(max(equal["catchments"][1][1]), 0)
            self.assertEqual(equal["catchments"][0][1], doubled["catchments"][0][1])
            self.assertEqual(equal["catchments"][1][1], doubled["catchments"][1][1])
            # Identical receiving depth over twice the area means twice the
            # transferred volume, even though source area/depth are unchanged.
            original_volume = 2 * equal["catchments"][1][1][-1]
            doubled_volume = 4 * doubled["catchments"][1][1][-1]
            self.assertAlmostEqual(doubled_volume, 2 * original_volume)

    def test_shared_series_keeps_each_gages_snow_correction_factor(self):
        with tempfile.TemporaryDirectory() as directory:
            model = hydrology_model()
            model.raingages.add(replace(model.raingages["R"], id="R2", snow_factor=2))
            model.subcatchments.add(replace(model.subcatchments["S"], id="S2", rain_gage=Ref(collection="swmm:raingages", key="R2"),
                snowpack=Ref(collection="swmm:snowpacks", key="Snow")))
            model.subcatchments.update("S", snowpack=Ref(collection="swmm:snowpacks", key="Snow"))
            model.snowpacks.add(snowpack(initial=0))
            air = add_series(model, "Air", ((0, 0), (72, 0)))
            model.update_climate(temperature=c.SeriesTemperature(series=air))
            self.assertTrue(model.validate(for_run=True).is_valid)
            result = self.solve(directory, "shared_rain", model.to_document().text, catchments=2)
            self.assertGreater(max(result["catchments"][0][1]), 0)
            for first, second in zip(result["catchments"][0][1], result["catchments"][1][1]):
                self.assertAlmostEqual(second, first * 2, delta=1e-6)

    def test_native_interval_rounding_and_required_file_fields(self):
        from datetime import time
        with tempfile.TemporaryDirectory() as directory:
            for source_kind in ("TIMESERIES", "FILE"):
                model = hydrology_model()
                model.update_options(end_date=date(2020, 1, 30), end_time=time(1), report_step=timedelta(minutes=1), routing_step=timedelta(seconds=1))
                if source_kind == "FILE":
                    path = Path(directory) / "rain.dat"
                    path.write_text("Station 2020 1 30 0 0 .01\nStation 2020 1 30 0 1 .01\nStation 2020 1 30 1 0 0\n", encoding="ascii")
                    model.raingages.update("R", source=h.FileRainfall(file=FileReference(path=str(path)), station="Station", units="IN"))
                    model.timeseries.remove("Rain")
                document = model.to_document()
                line = document.records("RAINGAGES")[0]
                source = document.text.replace(line.content, line.content.replace(line.values[2], ".0005"))
                parsed = Model.from_document(InpDocument.from_text(source), strict=True)
                raw = self.solve(directory, "raw", source)
                self.assertEqual(self.solve(directory, "rebuilt", rebuild(parsed).to_document().text), raw)
                if source_kind == "FILE":
                    shortened = source.replace(" Station IN", "")
                    error, report = self.solve(directory, "missing", shortened, allow_error=True)
                    self.assertNotEqual(error, 0)
                    self.assertIn("ERROR 203", report)

    def test_repeated_subarea_and_infiltration_assignments_use_the_last_parameters(self):
        with tempfile.TemporaryDirectory() as directory:
            source = (handwritten("HORTON", "INTENSITY", "CFS", "OUTLET") +
                "[SUBAREAS]\nS .02 .3 .02 .5 30 PERVIOUS 75\n[INFILTRATION]\nS 4 .3 .2 GREEN_AMPT\n")
            model = Model.from_document(InpDocument.from_text(source), strict=True)
            original = self.solve(directory, "repeated", source)
            self.assertEqual(self.solve(directory, "rebuilt", rebuild(model).to_document().text), original)


if __name__ == "__main__":
    unittest.main()
