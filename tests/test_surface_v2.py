from dataclasses import replace
import unittest

from easysewer.io.inp import InpDocument
from easysewer.model import Model, Ref
from easysewer.model.geometry import CrossSection, Irregular, Street
from easysewer.model.resources import Curve, CurvePoint
from easysewer.model.surface import (
    CombinationInlet, CurbInlet, GenericGrate, GrateInlet, InletDesign, InletUsage,
    StandardGrate, StreetSection, Transect, TransectPoint, TransectRoughness,
)
from easysewer.validation import ValidationError
from test_options_v2 import network


def transect(name="T"):
    return Transect(id=name, roughness=TransectRoughness(left=.03, right=.04, channel=.02),
                   left_bank=0, right_bank=10, meander_factor=1.5, width_factor=1.2, elevation_offset=.3,
                   stations=(TransectPoint(elevation=2, station=0), TransectPoint(elevation=0, station=5), TransectPoint(elevation=2, station=10)))


INLETS = """[INLETS]
G GRATE 2 1 P_BAR-50
GG GRATE 2 1 GENERIC .8 4
C CURB 4 .5 HORIZONTAL
S SLOTTED 2 .1
DG DROP_GRATE 2 2 P_BAR-30
DC DROP_CURB 4 .5
Combo CURB 4 .5 INCLINED
Combo GRATE 2 1 CURVED_VANE
CD CUSTOM Div
CR CUSTOM Rate
[CURVES]
Div DIVERSION 0 0 10 8
Rate RATING 0 0 1 3
"""


class TransectTests(unittest.TestCase):
    def test_resolved_roughness_native_slots_and_ordered_writer(self):
        source = """[TRANSECTS]
NC 0 0 .02
X1 T1 3 0 10 0 0 4 1.5 .3
GR 2 0 0 5 2 10
NC 0 .05 0
X1 T2 3 0 10 0 0 1 2 .4
GR 3 0 0 5
GR 3 10
[REPORT]
"""
        model = Model.from_document(InpDocument.from_text(source), strict=True)
        first, second = model.transects["T1"], model.transects["T2"]
        self.assertEqual(first.roughness, TransectRoughness(left=.02, right=.02, channel=.02))
        self.assertEqual(second.roughness, TransectRoughness(left=.02, right=.05, channel=.04))
        self.assertEqual((first.meander_factor, first.width_factor, first.elevation_offset), (4, 1.5, .3))
        self.assertEqual(model.to_document().text, source)
        output = model.to_document(normalize=True)
        reread = Model.from_document(output, strict=True)
        self.assertEqual(list(reread.transects.values()), list(model.transects.values()))
        self.assertEqual(len([section for section in output.sections if section.name == "TRANSECTS"]), 1)
        self.assertEqual(output.sections[-1].name, "REPORT")
        self.assertEqual(len(output.records("TRANSECTS")[1].values), 10)

    def test_eof_and_missing_nc_are_visible_and_normalization_finishes_every_block(self):
        source = "[TRANSECTS]\nNC .03 .03 .02\nX1 A 3 0 10 0 0 0 0 0\nGR 2 0 0 5 2 10\nX1 B 3 0 10 0 0 0 0 0\nGR 3 0 0 5 3 10\n"
        model = Model.from_document(InpDocument.from_text(source), strict=True)
        self.assertEqual(len(model.transects), 2)
        self.assertIn("transect.missing_native_finalizer", [issue.code for issue in model.validate().diagnostics])
        self.assertEqual(model.to_document().text, source)
        output = model.to_document(normalize=True)
        self.assertEqual(sum(line.values[0] == "NC" for line in output.records("TRANSECTS")), 2)
        self.assertEqual(output.sections[-1].name, "REPORT")
        self.assertNotIn("transect.missing_native_finalizer", [issue.code for issue in Model.from_document(output).validate().diagnostics])

    def test_manual_eleventh_token_count_and_placeholders_are_diagnosed(self):
        source = "[TRANSECTS]\nNC .03 .03 .02\nX1 T 50 0 10 7 8 0 2 3 99\nGR 2 0 0 5 2 10\n[REPORT]\n"
        model = Model.from_document(InpDocument.from_text(source), strict=True)
        self.assertEqual(model.transects["T"].width_factor, 2)
        self.assertEqual(model.transects["T"].elevation_offset, 3)
        codes = {issue.code for issue in model.validate().diagnostics}
        self.assertTrue({"transect.ignored_fields", "transect.ignored_count"} <= codes)
        self.assertEqual(model.to_document().text, source)
        reread = Model.from_document(model.to_document(normalize=True), strict=True)
        self.assertEqual(reread.transects["T"], model.transects["T"])

    def test_invalid_state_preserves_complete_transect_section(self):
        cases = ("GR 2 0 0 5\n", "NC .03 .03 .02\nX1 T 3 0 10 0 0 0 0 0\nGR 2 0 0\n",
                 "NC .03 .03 .02\nX1 T 3 0 10 0 0 0 0 0\nGR 2 0 0 5 2 10\nFUTURE 1\n",
                 "NC .03 .03 .02\nX1 T 2 0 1 0 0 0 0 0\nGR 2 1 0 0\n")
        for body in cases:
            with self.subTest(body=body):
                source = "[TRANSECTS]\n" + body
                model = Model.from_document(InpDocument.from_text(source))
                self.assertFalse(model.validate().is_valid)
                self.assertFalse(model.transects)
                self.assertEqual(model.document.text, source)
                self.assertEqual(len(model.support.opaque_records), len(model.document.records("TRANSECTS")))

    def test_vertical_stations_unit_policy_and_graph_rename(self):
        model = network()
        record = transect()
        record = replace(record, stations=(TransectPoint(elevation=2, station=0), TransectPoint(elevation=0, station=0),
                                          TransectPoint(elevation=0, station=10), TransectPoint(elevation=2, station=10)))
        model.transects.add(record)
        model.links.update("P", section=CrossSection(geometry=Irregular(transect=Ref(collection="swmm:transects", key="T"))))
        model.transects.rename("T", "Channel")
        with self.assertRaises(ValidationError):
            model.transects.remove("Channel")
        model.convert_units("CMS")
        converted = model.transects["Channel"]
        self.assertAlmostEqual(converted.right_bank, 10 * 1.2 * .3048)
        self.assertAlmostEqual(converted.elevation_offset, .3 * .3048 ** 2)
        self.assertEqual(converted.width_factor, 1)
        self.assertEqual(converted.roughness, record.roughness)
        self.assertEqual(model.links["P"].section.geometry.transect.key, "Channel")
        reread = Model.from_document(model.to_document(), strict=True)
        self.assertEqual(reread.transects["Channel"], converted)

    def test_si_width_scaling_cannot_silently_change_native_bank_boundaries(self):
        model = network()
        model.transects.add(transect())
        model.convert_units("CMS", basis="physical")
        self.assertEqual(model.transects["T"].width_factor, 1.2)
        self.assertAlmostEqual(model.transects["T"].elevation_offset, .3 * .3048)
        self.assertIn("transect.width_bank_rounding", {issue.code for issue in model.validate().diagnostics})
        before = model.to_document().text
        with self.assertRaises(ValidationError) as raised:
            model.convert_units("CFS")
        self.assertIn("units.transect_width_rounding", str(raised.exception))
        self.assertEqual(model.to_document().text, before)
        model.convert_units("LPS")  # Same length system leaves width and stations alone.
        self.assertEqual(model.transects["T"].width_factor, 1.2)

    def test_edit_removes_empty_old_headers_and_retains_comments(self):
        source = "[TRANSECTS] ; header\nNC .03 .03 .02\nX1 T 3 0 10 0 0 0 0 0\nGR 2 0 0 5 2 10 ; points\n[REPORT]\n"
        model = Model.from_document(InpDocument.from_text(source), strict=True)
        model.transects.update("T", width_factor=1.1)
        output = model.to_document()
        self.assertEqual(len([section for section in output.sections if section.name == "TRANSECTS"]), 1)
        self.assertIn("; header", output.text)
        self.assertIn("; points", output.text)
        self.assertEqual(output.sections[-1].name, "REPORT")

    def test_repeated_finalization_capacity_is_checked_before_run(self):
        points = "".join(f"GR {0 if index == 749 else 2} {index}\n" for index in range(1499))
        source = network().to_document().text + "[TRANSECTS]\nNC .03 .03 .02\nX1 T 1499 0 1498 0 0 0 0 0\n" + points + "NC .03 .03 .02\n[REPORT]\n"
        model = Model.from_document(InpDocument.from_text(source), strict=True)
        self.assertIn("transect.native_station_capacity", [issue.code for issue in model.validate(for_run=True).errors])
        self.assertTrue(model.validate(for_run=True, normalize=True).is_valid)


class SurfaceTests(unittest.TestCase):
    def test_street_all_parameters_missing_values_and_units(self):
        source = "[STREETS]\nA 10 .5 2 .016\nB 10 .5 2 .016 .05 1 1 3 4 .03\n"
        model = Model.from_document(InpDocument.from_text(source), strict=True)
        self.assertIsNone(model.streets["A"].sides)
        self.assertEqual(model.streets["B"].backing_roughness, .03)
        model.convert_units("CMS")
        self.assertAlmostEqual(model.streets["B"].gutter_depression, .05 * .3048)
        self.assertEqual(model.streets["B"].cross_slope, 2)
        self.assertEqual(model.streets["B"].sides, 1)
        reread = Model.from_document(model.to_document(), strict=True)
        self.assertEqual(list(reread.streets.values()), list(model.streets.values()))
        explicit = Model.from_document(InpDocument.from_text("[STREETS]\nS 10 .5 2 .016 0 0 2 0 0 0\n"), strict=True)
        self.assertEqual(explicit.streets["S"].backing_slope, 0)
        self.assertEqual(explicit.streets["S"].backing_roughness, 0)

    def test_all_inlet_variants_combinations_and_native_ignored_parameters(self):
        model = Model.from_document(InpDocument.from_text(INLETS), strict=True)
        self.assertEqual(len(model.inlets), 9)
        self.assertIsInstance(model.inlets["Combo"].design, CombinationInlet)
        model.inlets.rename("Combo", "Combined")
        self.assertEqual(list(Model.from_document(model.to_document(), strict=True).inlets.values()), list(model.inlets.values()))
        source = "[INLETS]\nG GRATE 2 1 P_BAR-50 ignored ignored\nD DROP_CURB 2 .5 IGNORED\n"
        parsed = Model.from_document(InpDocument.from_text(source), strict=True)
        self.assertEqual(parsed.to_document().text, source)
        self.assertIsNone(parsed.inlets["D"].design.throat)
        self.assertTrue(any("ignored" in item.code for item in parsed.validate().diagnostics))

    def test_all_standard_grates_and_generic_optional_velocity(self):
        names = ("P_BAR-50", "P_BAR-50X100", "P_BAR-30", "CURVED_VANE", "TILT_BAR-45", "TILT_BAR-30", "RETICULINE")
        source = "[INLETS]\n" + "\n".join(f"G{i} GRATE 2 1 {name}" for i, name in enumerate(names)) + "\nGG GRATE 2 1 GENERIC .7\n"
        model = Model.from_document(InpDocument.from_text(source), strict=True)
        self.assertIsNone(model.inlets["GG"].design.grate.splash_velocity)
        self.assertEqual(list(Model.from_document(model.to_document(normalize=True), strict=True).inlets.values()), list(model.inlets.values()))

    def test_usage_derived_identity_updates_with_link_node_and_design(self):
        model = network()
        model.streets.add(StreetSection(id="Road", crown_width=10, curb_height=.5, cross_slope=2, road_roughness=.016))
        model.links.update("P", section=CrossSection(geometry=Street(street=Ref(collection="swmm:streets", key="Road"))))
        model.inlets.add(InletDesign(id="I", design=GrateInlet(kind="GRATE", length=2, width=1, grate=GenericGrate(open_fraction=.7, splash_velocity=3))))
        model.inlet_usage.add(InletUsage(link=Ref(collection="swmm:links", key="P"), inlet=Ref(collection="swmm:inlets", key="I"),
                                        node=Ref(collection="swmm:nodes", key="J"), count=2, percent_clogged=20, maximum_flow=3,
                                        local_depression=.03, local_width=1, placement="ON_SAG"))
        model.links.rename("P", "RoadLink")
        model.nodes.rename("J", "Receiver")
        model.inlets.rename("I", "Grate")
        model.streets.rename("Road", "Street")
        self.assertNotIn("P", model.inlet_usage)
        self.assertEqual(model.inlet_usage["RoadLink"].node.key, "Receiver")
        self.assertEqual(model.inlet_usage["RoadLink"].inlet.key, "Grate")
        model.convert_units("CMS")
        self.assertAlmostEqual(model.inlet_usage["RoadLink"].local_depression, .03 * .3048)
        self.assertAlmostEqual(model.inlet_usage["RoadLink"].maximum_flow, 3 * .02832)
        self.assertAlmostEqual(model.inlets["Grate"].design.grate.splash_velocity, 3 * .3048)
        self.assertEqual(Model.from_document(model.to_document(), strict=True).inlet_usage["RoadLink"], model.inlet_usage["RoadLink"])

    def test_usage_repeated_assignment_and_interior_defaults(self):
        model = network()
        source = model.to_document().text + "[INLETS]\nI DROP_CURB 2 .5\n[INLET_USAGE]\nP I J 1 20\nP I J\n"
        parsed = Model.from_document(InpDocument.from_text(source), strict=True)
        self.assertIsNone(parsed.inlet_usage["P"].count)
        parsed.inlet_usage.update("P", placement="ON_GRADE")
        output = parsed.to_document()
        self.assertEqual(output.records("INLET_USAGE")[0].values, ("P", "I", "J", "1", "0", "0", "0", "0", "ON_GRADE"))
        self.assertEqual(len(output.records("INLET_USAGE")), 1)

    def test_invalid_usage_shape_and_custom_curve_purpose_are_errors(self):
        model = network()
        model.inlets.add(InletDesign(id="I", design=CurbInlet(kind="CURB", length=2, height=.5)))
        model.inlet_usage.add(InletUsage(link=Ref(collection="swmm:links", key="P"), inlet=Ref(collection="swmm:inlets", key="I"), node=Ref(collection="swmm:nodes", key="J")))
        self.assertIn("inlet.incompatible_conduit", [item.code for item in model.validate().errors])
        parsed = Model.from_document(InpDocument.from_text("[INLETS]\nI CUSTOM C\n[CURVES]\nC SHAPE 0 0 1 1\n"))
        self.assertIn("resource.wrong_purpose", [item.code for item in parsed.validate().errors])

    def test_invalid_surface_variants_remain_source_owned(self):
        cases = ("[STREETS]\nS 10 .5 2 .016 0 0 2 1\n", "[INLETS]\nI GRATE 2 1 GENERIC 0\n",
                 "[INLETS]\nI GRATE 2 1 P_BAR-30\nI DROP_CURB 2 .5\n", "[INLET_USAGE]\nP I J 1 100\n")
        for source in cases:
            with self.subTest(source=source):
                model = Model.from_document(InpDocument.from_text(source))
                self.assertFalse(model.validate().is_valid)
                self.assertTrue(model.support.opaque_records)
                self.assertEqual(model.document.text, source)


if __name__ == "__main__":
    unittest.main()
