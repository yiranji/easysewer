from dataclasses import replace
from datetime import timedelta, time
import unittest

from easysewer.io.inp import InpDocument
from easysewer.model import Model, Ref
from easysewer.model import network as n
from easysewer.model.geometry import CrossSection, Circular, RectClosed, RectOpen, Triangular, Trapezoidal
from easysewer.model.resources import Curve, CurvePoint
from easysewer.model.values import Offset, Point
from easysewer.validation import ValidationError


def curve(name, kind, points):
    return Curve(id=name, kind=kind, points=tuple(CurvePoint(x=x, y=y) for x, y in points))


def regulator_model(kind="TRANSVERSE", *, storage=True):
    model = Model()
    model.update_options(end_time=time(0, 5), routing_step=timedelta(seconds=2), variable_step=0)
    if storage:
        model.nodes.add(n.Storage(id="J", elevation=10, max_depth=8, initial_depth=2,
            shape=n.FunctionalStorage(coefficient=0, exponent=1, constant=500)))
    else:
        model.nodes.add(n.Junction(id="J", elevation=10, max_depth=8, initial_depth=2))
    model.nodes.add(n.Outfall(id="O", elevation=9, boundary=n.FreeBoundary()))
    common = dict(id="P", inlet=Ref(collection="swmm:nodes", key="J"), outlet=Ref(collection="swmm:nodes", key="O"))
    if kind.startswith("PUMP") or kind == "IDEAL":
        pump_curve = None
        if kind != "IDEAL":
            points = ((0, 2), (2000, 4)) if kind == "PUMP1" else ((0, 2), (8, 4))
            model.curves.add(curve("Characteristic", kind, points))
            pump_curve = Ref(collection="swmm:curves", key="Characteristic")
        link = n.Pump(**common, curve=pump_curve)
    elif kind in ("SIDE", "BOTTOM"):
        link = n.Orifice(**common, orientation=kind, offset=.2, coefficient=.65,
                         section=CrossSection(geometry=Circular(diameter=1)))
    elif kind.startswith(("FUNCTIONAL", "TABULAR")):
        rating, basis = kind.split("/")
        if rating == "TABULAR":
            model.curves.add(curve("Rating", "RATING", ((0, 0), (2, 3), (8, 8))))
            law = n.TabularRating(basis=basis, curve=Ref(collection="swmm:curves", key="Rating"))
        else:
            law = n.FunctionalRating(basis=basis, coefficient=2, exponent=1.4)
        link = n.Outlet(**common, offset=.2, rating=law)
    else:
        geometry = (Triangular(full_depth=3, top_width=4) if kind == "V-NOTCH" else
                    Trapezoidal(full_depth=3, bottom_width=2, left_slope=.5, right_slope=1) if kind == "TRAPEZOIDAL" else
                    RectOpen(full_depth=3, width=2, ignored_sides=0))
        link = n.Weir(**common, weir_type=kind, crest_height=.2, coefficient=3.1,
                      section=CrossSection(geometry=geometry))
    model.links.add(link)
    return model


KINDS = ("IDEAL", "PUMP1", "PUMP2", "PUMP3", "PUMP4", "PUMP5", "SIDE", "BOTTOM",
         "TRANSVERSE", "SIDEFLOW", "V-NOTCH", "TRAPEZOIDAL", "ROADWAY", "FUNCTIONAL/HEAD", "FUNCTIONAL/DEPTH", "TABULAR/HEAD", "TABULAR/DEPTH")


class RegulatorTests(unittest.TestCase):
    def test_every_variant_roundtrips_as_typed_link_and_preserves_source(self):
        for kind in KINDS:
            with self.subTest(kind=kind):
                model = regulator_model(kind)
                model.links.update("P", vertices=(Point(x=123, y=456),))
                document = model.to_document()
                parsed = Model.from_document(document, strict=True)
                self.assertEqual(parsed.links["P"], model.links["P"])
                self.assertEqual(parsed.to_document().text, document.text)
                self.assertTrue(parsed.validate(for_run=True).is_valid)

    def test_pump_optional_defaults_ideal_form_and_auto_control(self):
        for tail in ("", " *", " * ON", " * OFF 3 1"):
            source = "[JUNCTIONS]\nJ 0 8\nO -1 8\n[PUMPS]\nP J O" + tail + "\n"
            model = Model.from_document(InpDocument.from_text(source), strict=True)
            self.assertIsNone(model.links["P"].curve)
            self.assertEqual(Model.from_document(model.to_document(normalize=True), strict=True).links["P"], model.links["P"])
        model.links.update("P", startup_depth=1, shutoff_depth=2)
        self.assertIn("pump.invalid_depth_limits", {d.code for d in model.validate(for_run=True).errors})
        model.links.update("P", startup_depth=0)
        self.assertNotIn("pump.invalid_depth_limits", {d.code for d in model.validate(for_run=True).errors})

    def test_orifice_decimal_hours_zero_coefficient_and_duration_precision(self):
        model = regulator_model("SIDE")
        model.links.update("P", coefficient=0, gated=True, opening_time=timedelta(seconds=1))
        parsed = Model.from_document(model.to_document(), strict=True)
        self.assertEqual(parsed.links["P"], model.links["P"])
        model.links.update("P", opening_time=timedelta(seconds=-1))
        self.assertFalse(model.validate().is_valid)
        text = parsed.to_document().text.replace("0.0002777777777777778", "00:00:01")
        self.assertTrue(Model.from_document(InpDocument.from_text(text)).validate().errors)

    def test_weir_all_optional_fields_and_curve_placeholders(self):
        model = regulator_model("TRAPEZOIDAL")
        model.curves.add(curve("Cd", "WEIR", ((0, 2), (4, 3))))
        model.links.update("P", gated=True, end_contractions=1.5, end_coefficient=2.4, can_surcharge=False,
                           coefficient_curve=Ref(collection="swmm:curves", key="Cd"))
        parsed = Model.from_document(model.to_document(), strict=True)
        self.assertEqual(parsed.links["P"], model.links["P"])
        parsed.curves.rename("Cd", "Cnew")
        self.assertEqual(parsed.links["P"].coefficient_curve.key, "Cnew")
        with self.assertRaises(ValidationError):
            parsed.curves.remove("Cnew")
        for surface in ("PAVED", "GRAVEL"):
            road = regulator_model("ROADWAY")
            road.links.update("P", road_width=20, road_surface=surface)
            self.assertEqual(Model.from_document(road.to_document(), strict=True).links["P"], road.links["P"])

    def test_outlet_legacy_alias_and_constant_rating(self):
        model = regulator_model("FUNCTIONAL/DEPTH")
        model.links.update("P", rating=n.FunctionalRating(basis="DEPTH", coefficient=2, exponent=0))
        source = model.to_document().text.replace("FUNCTIONAL/DEPTH", "FUNCTIONAL")
        parsed = Model.from_document(InpDocument.from_text(source), strict=True)
        self.assertEqual(parsed.links["P"], model.links["P"])
        self.assertEqual(parsed.to_document().text, source)
        self.assertIn("FUNCTIONAL/DEPTH", parsed.to_document(normalize=True).text)
        parsed.links.update("P", rating=n.FunctionalRating(basis="HEAD", coefficient=-2, exponent=-1))
        self.assertTrue(parsed.validate().is_valid)
        self.assertIn("outlet.negative_rating", {d.code for d in parsed.validate(for_run=True).errors})

    def test_resource_purpose_and_shared_curve_single_conversion(self):
        model = regulator_model("PUMP2")
        model.links.add(replace(model.links["P"], id="Second"))
        model.convert_units("CMS")
        self.assertAlmostEqual(model.curves["Characteristic"].points[1].x, 8 * .3048)
        self.assertAlmostEqual(model.curves["Characteristic"].points[1].y, 4 * .02832)
        model.curves.update("Characteristic", kind="STORAGE")
        self.assertIn("resource.wrong_purpose", {d.code for d in model.validate().errors})

    def test_weir_units_ignore_selected_flow_unit_and_use_profile_constants(self):
        for kind in ("TRANSVERSE", "SIDEFLOW", "V-NOTCH", "TRAPEZOIDAL", "ROADWAY"):
            model = regulator_model(kind)
            model.curves.add(curve("Cd", "WEIR", ((0, 2), (4, 3))))
            model.links.update("P", coefficient_curve=Ref(collection="swmm:curves", key="Cd"), end_coefficient=1.2)
            original = model.links["P"]
            model.convert_units("GPM")
            self.assertEqual(model.links["P"], original)
            model.convert_units("LPS")
            factor = .552 if kind == "ROADWAY" else .028317 / .3048 ** 2.5
            self.assertAlmostEqual(model.links["P"].coefficient, 3.1 * factor)
            self.assertAlmostEqual(model.curves["Cd"].points[1].y, 3 * .028317 / .3048 ** 2.5)
            model.convert_units("CFS")
            self.assertAlmostEqual(model.links["P"].coefficient, 3.1)
        model = regulator_model("TRANSVERSE")
        model.convert_units("CMS", basis="physical")
        self.assertAlmostEqual(model.links["P"].coefficient, 3.1 * .3048 ** .5)

    def test_outlet_coefficient_conversion_and_overflow_rollback(self):
        model = regulator_model("FUNCTIONAL/HEAD")
        for units in ("GPM", "MGD", "CMS", "LPS", "MLD"):
            converted = model.copy()
            q = model.units.convert(1, dimension="flow", to=type(model.units)(flow_units=units), rules=model.profile.unit_rules)
            length = .3048 if units in ("CMS", "LPS", "MLD") else 1
            converted.convert_units(units)
            self.assertAlmostEqual(converted.links["P"].rating.coefficient, 2 * q / length ** 1.4)
        model.links.update("P", rating=n.FunctionalRating(basis="HEAD", coefficient=2, exponent=1e9))
        before = model.to_document().text
        with self.assertRaises(ValidationError):
            model.convert_units("CMS")
        self.assertEqual(model.to_document().text, before)

    def test_offsets_all_link_types_and_clamping(self):
        for kind in KINDS:
            model = regulator_model(kind)
            before = model.links["P"]
            model.convert_link_offsets("ELEVATION")
            if isinstance(before, n.Pump):
                self.assertEqual(model.links["P"], before)
            else:
                field = "crest_height" if isinstance(before, n.Weir) else "offset"
                self.assertAlmostEqual(getattr(model.links["P"], field), 10.2)
                model.links.update("P", **{field: Offset.NODE_INVERT})
                parsed = Model.from_document(model.to_document(), strict=True)
                self.assertIs(getattr(parsed.links["P"], field), Offset.NODE_INVERT)
                parsed.convert_link_offsets("DEPTH")
                self.assertEqual(getattr(parsed.links["P"], field), 0)
                model.links.update("P", **{field: 9})
            model.convert_link_offsets("DEPTH")
            if not isinstance(before, n.Pump):
                self.assertEqual(getattr(model.links["P"], field), 0)

    def test_shape_topology_and_invalid_drafts_return_diagnostics(self):
        model = regulator_model("SIDE", storage=False)
        model.update_options(flow_routing="KINWAVE")
        self.assertIn("regulator.requires_storage", {d.code for d in model.validate(for_run=True).errors})
        model.links.update("P", section=CrossSection(geometry=RectOpen(full_depth=1, width=2)))
        self.assertIn("regulator.invalid_shape", {d.code for d in model.validate().errors})
        model.links.update("P", inlet="bad", section=None)
        self.assertFalse(model.validate(for_run=True).is_valid)

    def test_unsupported_relations_extra_fields_and_duplicates_stay_source_owned(self):
        for appended in ("[XSECTIONS]\nP CIRCULAR 1 0 0 0\n", "[LOSSES]\nP 0 0 0 YES\n",
                         "[PUMPS]\nP J O *\n"):
            source = regulator_model("IDEAL").to_document().text + appended
            model = Model.from_document(InpDocument.from_text(source))
            self.assertEqual(model.document.text, source)
            with self.assertRaises(ValidationError):
                model.links.rename("P", "Changed")

    def test_mixed_link_declaration_order_and_reference_rename(self):
        model = regulator_model("SIDE")
        for index, kind in enumerate(("PUMP2", "TRANSVERSE", "FUNCTIONAL/HEAD", "BOTTOM")):
            other = regulator_model(kind)
            for resource in other.curves.values():
                model.curves.add(resource)
            model.links.add(replace(other.links["P"], id=f"L{index}"))
        parsed = Model.from_document(model.to_document(), strict=True)
        self.assertEqual(tuple(parsed.links), ("P", "L0", "L1", "L2", "L3"))
        parsed.nodes.rename("J", "Inlet")
        self.assertTrue(all(link.inlet.key == "Inlet" for link in parsed.links.values()))
        self.assertEqual(tuple(Model.from_document(parsed.to_document(normalize=True), strict=True).links), tuple(parsed.links))

    def test_native_ignored_section_tokens_do_not_inherit_conduit_constraints(self):
        model = regulator_model("TRANSVERSE")
        source = model.to_document().text.replace("RECT_OPEN 3 2 0 0", "RECT_OPEN 3 2 9 17 unused unused")
        parsed = Model.from_document(InpDocument.from_text(source), strict=True)
        self.assertEqual(parsed.to_document().text, source)
        self.assertEqual(parsed.links["P"].section, model.links["P"].section)
        self.assertIn("regulator.ignored_section_fields", {d.code for d in parsed.validate().diagnostics})
        self.assertNotIn("unused", parsed.to_document(normalize=True).text)


if __name__ == "__main__":
    unittest.main()
