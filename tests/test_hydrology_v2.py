from dataclasses import fields, replace
from datetime import date, timedelta
from pathlib import Path
import tempfile
import unittest

from easysewer.io.inp import InpDocument
from easysewer.model import Model, Ref
from easysewer.model import hydrology as h, climate as c, network as n
from easysewer.model.resources import Pattern
from easysewer.model.values import FileReference, Point
from easysewer.validation import ValidationError
from test_climate_v2 import climate_model, add_series


INFILTRATION = {
    "HORTON": "3 .2 4 2 0",
    "MODIFIED_HORTON": "3 .2 4 2",
    "GREEN_AMPT": "3 .2 .3",
    "MODIFIED_GREEN_AMPT": "3 .2 .3",
    "CURVE_NUMBER": "75 0 2",
}


def hydrology_model(method="HORTON", *, form="INTENSITY"):
    model = climate_model()
    model.nodes.update("J", initial_depth=0, max_depth=50, evaporation_fraction=0)
    model.links.update("P", rating=n.FunctionalRating(basis="HEAD", coefficient=1, exponent=1))
    rain = add_series(model, "Rain", tuple((hour, .6 if 2 <= hour < 8 or 48 <= hour < 52 else 0) for hour in range(73)))
    model.raingages.add(h.RainGage(id="R", form=form, interval=timedelta(hours=1), snow_factor=1, source=h.SeriesRainfall(series=rain)))
    parameters = (h.Horton(maximum_rate=3, minimum_rate=.2, decay=4, drying_time=2, maximum_volume=0)
        if method in ("HORTON", "MODIFIED_HORTON") else h.GreenAmpt(suction=3, conductivity=.2, initial_deficit=.3)
        if method in ("GREEN_AMPT", "MODIFIED_GREEN_AMPT") else h.CurveNumber(curve_number=75, drying_time=2))
    model.subcatchments.add(h.Subcatchment(id="S", rain_gage=Ref(collection="swmm:raingages", key="R"),
        outlet=Ref(collection="swmm:nodes", key="J"), area=2, impervious_percent=30, width=100, slope=1, curb_length=12,
        subareas=h.Subareas(impervious_roughness=.01, pervious_roughness=.2, impervious_storage=.05, pervious_storage=.1,
                           zero_storage_percent=25, route_to="OUTLET"),
        infiltration=h.Infiltration(parameters=parameters, method=method)))
    return model


def snowpack(name="Snow", *, initial=1, removal=None):
    args = dict(minimum_melt=.001, maximum_melt=.004, base_temperature=32, free_water_fraction=.1,
                initial_snow=initial, initial_free_water=0)
    return h.Snowpack(id=name, plowable=h.PlowableSnow(**args, fraction=.5),
        impervious=h.DepletableSnow(**args, full_cover_depth=4), pervious=h.DepletableSnow(**args, full_cover_depth=4), removal=removal)


def rebuild(model):
    result = Model()
    result.update_options(**{field.name: getattr(model.options, field.name) for field in fields(model.options)})
    result.update_climate(**{field.name: getattr(model.climate, field.name) for field in fields(model.climate)})
    for name in ("nodes", "links", "timeseries", "patterns", "raingages", "subcatchments", "snowpacks", "subcatchment_adjustments"):
        for row in getattr(model, name).values():
            getattr(result, name).add(row)
    return result


class HydrologyTests(unittest.TestCase):
    def test_five_infiltration_methods_implicit_and_explicit_roundtrip(self):
        for method, parameters in INFILTRATION.items():
            for suffix in ("", " " + method):
                source = (f"[OPTIONS]\nINFILTRATION {method}\n[RAINGAGES]\nR VOLUME .5 1 TIMESERIES Rain\n"
                          "[TIMESERIES]\nRain 0 0\n[JUNCTIONS]\nJ 0\n[SUBCATCHMENTS]\nS R J 2 30 100 1 0\n"
                          f"[SUBAREAS]\nS .01 .2 .05 .1 25 PERVIOUS 50\n[INFILTRATION]\nS {parameters}{suffix}\n")
                with self.subTest(method=method, suffix=suffix):
                    model = Model.from_document(InpDocument.from_text(source), strict=True)
                    self.assertEqual(model.to_document().text, source)
                    self.assertEqual(model.subcatchments["S"].infiltration.method, method if suffix else None)
                    parsed = Model.from_document(model.to_document(normalize=True), strict=True)
                    self.assertEqual(parsed.subcatchments["S"], model.subcatchments["S"])

    def test_inherited_method_keeps_dependency_and_rejects_incompatible_parameters(self):
        model = hydrology_model()
        row = model.subcatchments["S"]
        model.subcatchments.update("S", infiltration=replace(row.infiltration, method=None))
        model = Model.from_document(model.to_document(), strict=True)
        model.update_options(infiltration="MODIFIED_HORTON")
        self.assertEqual(Model.from_document(model.to_document(), strict=True).subcatchments["S"].infiltration.method, None)
        with self.assertRaises(ValidationError):
            with model.transaction():
                model.update_options(infiltration="GREEN_AMPT")
        self.assertEqual(model.options.infiltration, "MODIFIED_HORTON")

    def test_reference_rename_removal_and_derived_local_adjustment_keys(self):
        model = hydrology_model()
        model.patterns.add(Pattern(id="Monthly", kind="MONTHLY", factors=(.5, 2)))
        model.subcatchment_adjustments.add(c.SubcatchmentAdjustments(subcatchment=Ref(collection="swmm:subcatchments", key="S"),
            infiltration=Ref(collection="swmm:patterns", key="Monthly"), depression_storage=Ref(collection="swmm:patterns", key="Monthly")))
        model.snowpacks.add(snowpack())
        model.subcatchments.update("S", snowpack=Ref(collection="swmm:snowpacks", key="Snow"))
        model.raingages.rename("R", "Gage")
        model.timeseries.rename("Rain", "Storm")
        model.snowpacks.rename("Snow", "SnowParameters")
        model.subcatchments.rename("S", "Catchment")
        model.patterns.rename("Monthly", "Seasonal")
        self.assertEqual(model.subcatchments["Catchment"].rain_gage.key, "Gage")
        self.assertEqual(model.raingages["Gage"].source.series.key, "Storm")
        self.assertEqual(model.subcatchment_adjustments["Catchment"].infiltration.key, "Seasonal")
        self.assertEqual(model.subcatchments["Catchment"].snowpack.key, "SnowParameters")
        with self.assertRaises(ValidationError):
            model.raingages.remove("Gage")
        self.assertEqual(Model.from_document(model.to_document(), strict=True).subcatchments["Catchment"], model.subcatchments["Catchment"])

    def test_outlet_namespace_and_ambiguity_and_routed_outfall(self):
        model = hydrology_model()
        model.subcatchments.add(replace(model.subcatchments["S"], id="Downstream"))
        model.subcatchments.update("S", outlet=Ref(collection="swmm:subcatchments", key="Downstream"))
        model.nodes.update("O", route_to=Ref(collection="swmm:subcatchments", key="S"))
        model.subcatchments.rename("Downstream", "Receiver")
        parsed = Model.from_document(model.to_document(), strict=True)
        self.assertEqual(parsed.subcatchments["S"].outlet, Ref(collection="swmm:subcatchments", key="Receiver"))
        self.assertEqual(parsed.nodes["O"].route_to.key, "S")
        model.nodes.add(n.Junction(id="Receiver", elevation=0))
        self.assertIn("subcatchment.ambiguous_outlet", {d.code for d in model.validate().errors})

    def test_all_rain_forms_units_and_file_path_rebasing(self):
        for form in ("INTENSITY", "VOLUME", "CUMULATIVE"):
            model = hydrology_model(form=form)
            before = model.timeseries["Rain"].points[2].value
            model.convert_units("CMS")
            self.assertAlmostEqual(model.timeseries["Rain"].points[2].value, before * 25.4)
            parsed = Model.from_document(model.to_document(), strict=True)
            self.assertEqual(parsed.raingages["R"].form, form)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "old").mkdir()
            (root / "new").mkdir()
            path = root / "old" / "rain.inp"
            source = '[RAINGAGES]\nR INTENSITY 0:30 1 FILE "weather data.dat" Station MM *\n'
            path.write_text(source, encoding="utf-8")
            model = Model.from_inp(path, strict=True)
            model.convert_units("CMS")
            self.assertEqual(model.raingages["R"].source.units, "MM")
            model.to_inp(root / "new" / "rain.inp")
            parsed = Model.from_inp(root / "new" / "rain.inp", strict=True)
            self.assertEqual(parsed.raingages["R"].source.file.resolve(), model.raingages["R"].source.file.resolve())

    def test_rain_interval_rounds_series_but_truncates_numeric_file_values(self):
        for source, seconds in (("TIMESERIES Rain", 2), ('FILE weather.dat Station IN', 1)):
            document = InpDocument.from_text(f"[RAINGAGES]\nR VOLUME .0005 1 {source}\n[TIMESERIES]\nRain 0 0\n")
            model = Model.from_document(document, strict=True)
            self.assertEqual(model.raingages["R"].interval, timedelta(seconds=seconds))
            self.assertEqual(model.to_document().text, document.text)
            self.assertIn("rainfall.interval_coercion", {d.code for d in model.validate().diagnostics})
            self.assertEqual(Model.from_document(model.to_document(normalize=True), strict=True).raingages["R"].interval, timedelta(seconds=seconds))

    def test_file_missing_station_and_units_stays_source_owned(self):
        source = '[RAINGAGES]\nR VOLUME 1 1 FILE "unknown weather.dat"\n'
        model = Model.from_document(InpDocument.from_text(source))
        self.assertFalse(model.raingages)
        self.assertEqual(model.to_document().text, source)
        self.assertIn("hydrology.unsupported_input", {d.code for d in model.validate().diagnostics})
        model.update_options(end_date=date(2004, 1, 2))
        self.assertIn("rainfall.native_file_fields", {d.code for d in model.validate(for_run=True).errors})

    def test_snow_surfaces_and_removal_variants_repeated_rows_and_renames(self):
        model = hydrology_model()
        removal = h.SnowRemoval(threshold=.1, out_of_system=.1, to_impervious=.2, to_pervious=.2, immediate_melt=.1,
                                to_subcatchment=.3, destination=Ref(collection="swmm:subcatchments", key="S"))
        model.snowpacks.add(snowpack(removal=removal))
        model.subcatchments.update("S", snowpack=Ref(collection="swmm:snowpacks", key="Snow"))
        source = model.to_document().text + "[SNOWPACKS]\nSnow PLOWABLE .002 .005 -1 .2 2 .1 .7\n"
        parsed = Model.from_document(InpDocument.from_text(source), strict=True)
        self.assertEqual(parsed.to_document().text, source)
        parsed.subcatchments.rename("S", "Receiver")
        self.assertEqual(parsed.snowpacks["Snow"].removal.destination.key, "Receiver")
        normalized = Model.from_document(parsed.to_document(normalize=True), strict=True)
        self.assertEqual(normalized.snowpacks["Snow"], parsed.snowpacks["Snow"])
        self.assertEqual(len(normalized.to_document().records("SNOWPACKS")), 4)

    def test_snow_missing_destination_is_rejected_before_native_run(self):
        model = hydrology_model()
        model.snowpacks.add(snowpack(removal=h.SnowRemoval(threshold=0, out_of_system=0, to_impervious=0,
            to_pervious=0, immediate_melt=0, to_subcatchment=.5)))
        self.assertIn("snowpack.missing_destination", {d.code for d in model.validate(for_run=True).errors})

    def test_manual_snow_removal_omission_requires_normalization(self):
        model = hydrology_model()
        source = model.to_document().text + "[SNOWPACKS]\nSnow REMOVAL .1 .2 .2 .2 .1\n"
        parsed = Model.from_document(InpDocument.from_text(source), strict=True)
        self.assertIsNone(parsed.snowpacks["Snow"].removal.to_subcatchment)
        self.assertIn("snowpack.native_removal_syntax", {d.code for d in parsed.validate(for_run=True).errors})
        self.assertTrue(parsed.validate(for_run=True, normalize=True).is_valid)

    def test_unit_dimensions_preserve_maps_and_user_curb_units(self):
        model = hydrology_model()
        model.raingages.update("R", position=Point(x=100, y=200))
        model.subcatchments.update("S", polygon=(Point(x=1, y=2), Point(x=3, y=4)))
        model.snowpacks.add(snowpack())
        physical = model.copy()
        model.convert_units("CMS")
        self.assertAlmostEqual(model.subcatchments["S"].area, 2 * .92903e-5 / 2.2956e-5)
        self.assertEqual(model.subcatchments["S"].curb_length, 12)
        self.assertEqual(model.raingages["R"].position, Point(x=100, y=200))
        self.assertEqual(model.subcatchments["S"].polygon, (Point(x=1, y=2), Point(x=3, y=4)))
        self.assertAlmostEqual(model.snowpacks["Snow"].plowable.minimum_melt, .001 * 25.4 / 1.8)
        physical.convert_units("CMS", basis="physical")
        self.assertAlmostEqual(physical.snowpacks["Snow"].plowable.minimum_melt, .001 * 25.4 * 1.8)
        self.assertEqual(Model.from_document(model.to_document(), strict=True).subcatchments["S"], model.subcatchments["S"])

    def test_invalid_multiline_group_keeps_all_rows_unclaimed(self):
        for section, tail in (("SNOWPACKS", "Snow FUTURE 1 2 3"), ("SUBAREAS", "S bad .2 .1 .2 25 OUTLET")):
            model = hydrology_model()
            model.snowpacks.add(snowpack())
            source = model.to_document().text + f"[{section}]\n{tail}\n"
            parsed = Model.from_document(InpDocument.from_text(source))
            target = parsed.snowpacks if section == "SNOWPACKS" else parsed.subcatchments
            self.assertFalse(target)
            self.assertEqual(parsed.document.text, source)

    def test_repeated_relations_keep_final_values_and_original_text(self):
        source = (hydrology_model().to_document().text + "[SYMBOLS]\nR 1 2\nR 3 4\n"
                  "[SUBAREAS]\nS .02 .3 .02 .5 30 PERVIOUS 75\n"
                  "[INFILTRATION]\nS 4 .3 .2 GREEN_AMPT\n")
        model = Model.from_document(InpDocument.from_text(source), strict=True)
        self.assertEqual(model.to_document().text, source)
        self.assertEqual(model.raingages["R"].position, Point(x=3, y=4))
        self.assertEqual(model.subcatchments["S"].subareas.routed_percent, 75)
        self.assertEqual(model.subcatchments["S"].infiltration.method, "GREEN_AMPT")
        normalized = model.to_document(normalize=True)
        self.assertEqual(len(normalized.records("INFILTRATION")), 1)
        self.assertEqual(len(normalized.records("SUBAREAS")), 1)
        self.assertEqual(len(normalized.records("SYMBOLS")), 1)
        self.assertEqual(Model.from_document(normalized, strict=True).subcatchments["S"], model.subcatchments["S"])

    def test_native_subarea_and_local_adjustment_order_requires_explicit_repair(self):
        model = hydrology_model()
        model.patterns.add(Pattern(id="Pat", kind="MONTHLY", factors=(.5,)))
        document = model.to_document()
        row = document.records("SUBAREAS")[0].content
        source = ("[SUBAREAS]\n" + row + "\n[ADJUSTMENTS]\nINFIL S Pat\n" + document.text.replace(row + "\n", ""))
        parsed = Model.from_document(InpDocument.from_text(source), strict=True)
        self.assertIn("subcatchment.native_order", {d.code for d in parsed.validate(for_run=True).errors})
        self.assertTrue(parsed.validate(for_run=True, normalize=True).is_valid)
        parsed.subcatchments.update("S", width=123)
        self.assertTrue(parsed.validate(for_run=True).is_valid)
        projected = parsed.to_document()
        self.assertLess(projected.records("SUBCATCHMENTS")[0].number, projected.records("ADJUSTMENTS")[0].number)

    def test_rainfall_incompatible_shared_gages_and_nonrain_consumers(self):
        model = hydrology_model()
        model.raingages.add(replace(model.raingages["R"], id="R2", form="CUMULATIVE"))
        model.subcatchments.add(replace(model.subcatchments["S"], id="S2", rain_gage=Ref(collection="swmm:raingages", key="R2")))
        self.assertIn("rainfall.shared_gage_settings", {d.code for d in model.validate(for_run=True).errors})
        model.subcatchments.remove("S2")
        model.raingages.remove("R2")
        model.update_climate(temperature=c.SeriesTemperature(series=Ref(collection="swmm:timeseries", key="Rain")))
        self.assertIn("rainfall.exclusive_series", {d.code for d in model.validate(for_run=True).errors})

    def test_rain_file_station_conflicts_are_rejected_even_for_unused_gages(self):
        model = climate_model()
        first = h.RainGage(id="A", form="VOLUME", interval=timedelta(hours=1), snow_factor=1,
            source=h.FileRainfall(file=FileReference(path="first.dat"), station="Station", units="IN"))
        model.raingages.add(first)
        model.raingages.add(replace(first, id="B", source=replace(first.source, file=FileReference(path="second.dat"))))
        self.assertIn("rainfall.station_file_conflict", {d.code for d in model.validate(for_run=True).errors})
        model.raingages.update("B", source=replace(first.source, units="MM"))
        self.assertIn("rainfall.shared_file_settings", {d.code for d in model.validate(for_run=True).errors})
        model.update_options(ignore_rainfall=True)
        self.assertTrue(model.validate(for_run=True).is_valid)


if __name__ == "__main__":
    unittest.main()
