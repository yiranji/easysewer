from dataclasses import replace
from datetime import date, timedelta, time
from pathlib import Path
import tempfile
import unittest

from easysewer.io.inp import InpDocument
from easysewer.model import Model, Ref
from easysewer.model import climate as c, network as n
from easysewer.model.resources import InlineTimeSeries, SeriesPoint, Pattern
from easysewer.model.values import FileReference
from easysewer.validation import ValidationError


def climate_model():
    model = Model()
    model.update_options(start_date=date(2020, 1, 30), end_date=date(2020, 2, 2), end_time=time(0),
        report_start_date=date(2020, 1, 30), report_step=timedelta(hours=1), routing_step=timedelta(minutes=5),
        wet_step=timedelta(minutes=5), variable_step=0)
    model.nodes.add(n.Storage(id="J", elevation=0, max_depth=8, initial_depth=4, evaporation_fraction=1,
        shape=n.FunctionalStorage(coefficient=0, exponent=1, constant=500)))
    model.nodes.add(n.Outfall(id="O", elevation=-1, boundary=n.FreeBoundary()))
    model.links.add(n.Outlet(id="P", inlet=Ref(collection="swmm:nodes", key="J"), outlet=Ref(collection="swmm:nodes", key="O"),
        offset=0, rating=n.FunctionalRating(basis="HEAD", coefficient=0, exponent=1)))
    return model


def add_series(model, name, values):
    model.timeseries.add(InlineTimeSeries(id=name, points=tuple(SeriesPoint(time=timedelta(hours=hour), value=value) for hour, value in values)))
    return Ref(collection="swmm:timeseries", key=name)


class ClimateTests(unittest.TestCase):
    def test_empty_and_effective_defaults_do_not_create_input_rows(self):
        model = Model()
        model.update_climate()
        self.assertEqual(model.to_document().text, "")
        self.assertEqual(model.effective_climate.temperature.value, 70)
        self.assertEqual(model.effective_climate.snowmelt.latitude, 40)
        self.assertEqual(model.effective_climate.snowmelt.snowfall_temperature, 34)
        self.assertIsNone(model.climate.snowmelt)
        model.convert_units("CMS")
        self.assertAlmostEqual(model.effective_climate.temperature.value, (70 - 32) * 5 / 9)
        self.assertAlmostEqual(model.effective_climate.snowmelt.snowfall_temperature, 10 / 9)
        self.assertFalse(model.to_document().records("TEMPERATURE"))

    def test_all_temperature_rows_and_quoted_file_forms_roundtrip(self):
        for tail in ("", " *", " 01/05/2020", " * C10", " 01/05/2020 C", " * F"):
            source = ('[TEMPERATURE]\nFILE "data folder/weather.dat"' + tail + '\nTIMESERIES Air\n'
                'WINDSPEED MONTHLY 1 2 3 4 5 6 7 8 9 10 11 12\nSNOWMELT -2 .5 .6 100 35 -15\n'
                'ADC IMPERVIOUS 0 .1 .2 .3 .4 .5 .6 .7 .8 .9\nADC PERVIOUS 1 1 1 1 1 1 1 1 1 1\n'
                '[TIMESERIES]\nAir 0:00 -8\nAir 12:00 3\n')
            model = Model.from_document(InpDocument.from_text(source), strict=True)
            self.assertEqual(model.to_document().text, source)
            parsed = Model.from_document(model.to_document(normalize=True), strict=True)
            self.assertEqual(parsed.climate, model.climate)
            self.assertEqual(model.climate.file.file.path, "data folder/weather.dat")
            self.assertEqual(model.climate.snowmelt.solar_time_correction, -15)
            self.assertEqual(model.effective_climate.file.units, model.climate.file.units or "F")
            self.assertEqual(model.effective_climate.file.start_date,
                             model.climate.file.start_date or model.effective_options.values.start_date)

    def test_last_temperature_assignment_and_file_dependency_are_independent(self):
        for rows, selected in (("TIMESERIES Air\nFILE w.dat\n", c.FileTemperature),
                                ("FILE w.dat\nTIMESERIES Air\n", c.SeriesTemperature)):
            source = "[TEMPERATURE]\n" + rows + "WINDSPEED FILE\n[TIMESERIES]\nAir 0 40\n"
            model = Model.from_document(InpDocument.from_text(source), strict=True)
            self.assertIsInstance(model.climate.temperature, selected)
            self.assertIsNotNone(model.climate.file)
            self.assertEqual(model.to_document().text, source)
            parsed = Model.from_document(model.to_document(normalize=True), strict=True)
            self.assertEqual(parsed.climate, model.climate)

    def test_evaporation_sources_auxiliaries_and_repeated_assignments(self):
        for source_row in ("CONSTANT .2", "MONTHLY " + " ".join(str(i / 10) for i in range(12)),
                           "TIMESERIES Evap", "TEMPERATURE", "FILE " + " ".join([".8"] * 12)):
            source = ("[EVAPORATION]\nDRY_ONLY NO\nCONSTANT .7\nRECOVERY Rec\n" + source_row + "\nDRY_ONLY YES\n"
                      "[PATTERNS]\nRec MONTHLY 1 .5\n[TIMESERIES]\nEvap 0 .2\n[TEMPERATURE]\nFILE w.dat\n")
            model = Model.from_document(InpDocument.from_text(source), strict=True)
            self.assertTrue(model.climate.evaporation.dry_only)
            self.assertEqual(model.climate.evaporation.recovery_pattern.key, "Rec")
            parsed = Model.from_document(model.to_document(normalize=True), strict=True)
            self.assertEqual(parsed.climate, model.climate)
            self.assertEqual(len(parsed.to_document().records("EVAPORATION")), 3)

    def test_bare_evaporation_file_is_preserved_but_requires_explicit_normalization(self):
        for keyword in ("FILE", "file"):
            source = f"[TEMPERATURE]\nFILE w.dat\n[EVAPORATION]\n{keyword}\n"
            model = Model.from_document(InpDocument.from_text(source), strict=True)
            self.assertEqual(model.to_document().text, source)
            model.update_options(end_time=time(1))
            self.assertIn("climate.native_file_evap_syntax", {issue.code for issue in model.validate(for_run=True).errors})
            self.assertTrue(model.validate(for_run=True, normalize=True).is_valid)
            values = model.to_document(normalize=True).records("EVAPORATION")[0].values
            self.assertEqual(values[0], "FILE")
            self.assertEqual(tuple(map(float, values[1:])), (1.0,) * 12)

    def test_adjustments_use_delta_temperature_and_do_not_mutate_defaults(self):
        model = Model()
        air = add_series(model, "Air", ((0, -10), (24, 50)))
        model.update_climate(temperature=c.SeriesTemperature(series=air),
            wind=c.MonthlyWindSpeeds(values=(10.0,) * 12),
            snowmelt=c.Snowmelt(snowfall_temperature=32, antecedent_weight=.5, negative_melt_ratio=.6, elevation=100, latitude=35, solar_time_correction=-20),
            adjustments=c.ClimateAdjustments(temperature=c.MonthlyTemperatureChanges(values=(9.0,) * 12),
                evaporation=c.MonthlyEvaporation(values=(-.2,) * 12), rainfall=c.MonthlyFactors(values=(.5,) * 12),
                conductivity=c.MonthlyFactors(values=(0, -1, *(2.0,) * 10))))
        model.convert_units("CMS")
        self.assertEqual(model.climate.adjustments.temperature.values, (5.0,) * 12)
        self.assertAlmostEqual(model.timeseries["Air"].points[0].value, (-10 - 32) * 5 / 9)
        self.assertEqual(model.climate.snowmelt.snowfall_temperature, 0)
        self.assertAlmostEqual(model.climate.snowmelt.elevation, 30.48)
        self.assertEqual(model.climate.snowmelt.solar_time_correction, -20)
        self.assertTrue(all(abs(value - 16.08) < 1e-12 for value in model.climate.wind.values))
        self.assertEqual(model.climate.adjustments.evaporation.values, (-5.08,) * 12)
        self.assertEqual(model.effective_climate.adjustments.conductivity.values[:2], (1, 1))
        self.assertEqual(model.climate.adjustments.conductivity.values[:2], (0, -1))

    def test_shared_series_conflicting_dimensions_and_rename_delete(self):
        model = Model()
        air = add_series(model, "Air", ((0, 40), (24, 45)))
        model.patterns.add(Pattern(id="Rec", kind="MONTHLY", factors=(.8,)))
        model.update_climate(temperature=c.SeriesTemperature(series=air), evaporation=c.Evaporation(
            source=c.SeriesEvaporation(series=air), recovery_pattern=Ref(collection="swmm:patterns", key="Rec")))
        self.assertIn("resource.conflicting_dimensions", {d.code for d in model.validate().errors})
        model.update_climate(evaporation=c.Evaporation(source=c.ConstantEvaporation(rate=.1), recovery_pattern=Ref(collection="swmm:patterns", key="Rec")))
        model.timeseries.rename("Air", "Temperature")
        model.patterns.rename("Rec", "Recovery")
        self.assertEqual(model.climate.temperature.series.key, "Temperature")
        self.assertEqual(model.climate.evaporation.recovery_pattern.key, "Recovery")
        with self.assertRaises(ValidationError):
            model.timeseries.remove("Temperature")
        model.patterns.update("Recovery", kind="DAILY")
        self.assertIn("resource.wrong_purpose", {d.code for d in model.validate().errors})

    def test_external_climate_conversion_is_atomic_and_same_system_is_safe(self):
        model = Model()
        model.update_climate(file=c.ClimateFile(file=FileReference(path="missing.dat"), units="C10"),
            wind=c.FileWind(), snowmelt=c.Snowmelt(snowfall_temperature=32, antecedent_weight=.5,
                negative_melt_ratio=.6, elevation=100, latitude=40, solar_time_correction=0))
        model.convert_units("GPM")
        before = model.to_document().text
        with self.assertRaises(ValidationError):
            model.convert_units("CMS")
        self.assertEqual(model.to_document().text, before)
        self.assertEqual(model.climate.file.file.path, "missing.dat")

    def test_file_rebasing_uses_source_origin_without_opening_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "old").mkdir()
            (root / "new").mkdir()
            inp = root / "old" / "model.inp"
            inp.write_text('[TEMPERATURE]\nFILE "weather data.dat" * F\n', encoding="utf-8")
            model = Model.from_inp(inp, strict=True)
            model.to_inp(root / "new" / "model.inp")
            exported = Model.from_inp(root / "new" / "model.inp", strict=True)
            self.assertEqual(exported.climate.file.file.resolve(), model.climate.file.file.resolve())
            self.assertEqual(model.climate.file.file.path, "weather data.dat")

    def test_unknown_and_invalid_global_blocks_are_not_partially_claimed(self):
        for row in ("WINDSPEED FILE extra", "SNOWMELT 32 .5 .6 0 90 0", "ADC PERVIOUS 0 1", "FUTURE 1 2", "FILE w.dat * K"):
            source = "[TEMPERATURE]\nWINDSPEED MONTHLY " + " ".join(["2"] * 12) + "\n" + row + "\n"
            model = Model.from_document(InpDocument.from_text(source))
            self.assertIsNone(model.climate.wind)
            self.assertEqual(model.document.text, source)
            self.assertEqual(len(model.support.opaque_records), 2)

    def test_file_dependencies_temperature_evaporation_and_draft_errors(self):
        model = Model()
        model.update_options(end_time=time(1))
        model.update_climate(wind=c.FileWind())
        self.assertIn("climate.missing_file", {d.code for d in model.validate(for_run=True).errors})
        air = add_series(model, "Air", ((0, 30),))
        model.update_climate(file=c.ClimateFile(file=FileReference(path="w.dat")), temperature=c.SeriesTemperature(series=air),
                             evaporation=c.Evaporation(source=c.TemperatureEvaporation()))
        self.assertIn("climate.temperature_evap_source", {d.code for d in model.validate(for_run=True).errors})
        model.update_climate(temperature=None)
        self.assertTrue(model.validate(for_run=True).is_valid)
        self.assertIn("climate.native_file_without_runoff", {d.code for d in model.validate(for_run=True).diagnostics})
        model.collection("swmm:climate").replace("settings", c.Climate(wind=c.MonthlyWindSpeeds(values=(float("nan"),) * 12)))
        self.assertFalse(model.validate().is_valid)

    def test_overridden_missing_native_reference_requires_repair(self):
        model = Model.from_document(InpDocument.from_text("[TEMPERATURE]\nTIMESERIES Missing\nFILE w.dat\n"), strict=True)
        model.update_options(end_time=time(1))
        self.assertIn("climate.source_missing_reference", {d.code for d in model.validate(for_run=True).errors})
        self.assertTrue(model.validate(for_run=True, normalize=True).is_valid)

    def test_negative_evaporation_series_is_a_draft_but_not_a_physical_run(self):
        model = climate_model()
        evaporation = add_series(model, "Evap", ((0, -.1), (24, .2)))
        model.update_climate(evaporation=c.Evaporation(source=c.SeriesEvaporation(series=evaporation)))
        self.assertTrue(model.validate().is_valid)
        self.assertIn("climate.negative_evaporation", {d.code for d in model.validate(for_run=True).errors})
        self.assertEqual(Model.from_document(model.to_document(), strict=True).timeseries["Evap"].points[0].value, -.1)

    def test_local_adjustment_refs_are_typed_and_unresolved_targets_are_not_silently_accepted(self):
        source = "[ADJUSTMENTS]\nINFIL S Pat\nDSTORE S Pat\nN-PERV S Pat\n[PATTERNS]\nPat MONTHLY 1 2\n"
        model = Model.from_document(InpDocument.from_text(source))
        row = model.subcatchment_adjustments["S"]
        self.assertEqual(row.subcatchment.collection, "swmm:subcatchments")
        self.assertEqual(row.infiltration.key, "Pat")
        self.assertFalse(model.validate().is_valid)
        model.patterns.rename("Pat", "Factors")
        self.assertEqual(model.subcatchment_adjustments["S"].depression_storage.key, "Factors")


if __name__ == "__main__":
    unittest.main()
