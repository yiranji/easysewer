"""Regulator IO, optional forms and transformations against SWMM 5.2.4."""

from dataclasses import fields
from datetime import timedelta
from itertools import product
import tempfile
import unittest

from easysewer import get_native_capabilities
from easysewer.io.inp import InpDocument
from easysewer.model import Model, Ref
from easysewer.model import network as n
from easysewer.model.geometry import CrossSection, Circular, RectClosed
from easysewer.model.units import UnitContext
from easysewer.model.values import Offset
import test_native_v2_nodes as native_nodes
from test_regulators_v2 import KINDS, curve, regulator_model


def rebuilt(model):
    result = Model()
    result.update_options(**{field.name: getattr(model.options, field.name) for field in fields(model.options)})
    for name in ("nodes", "links", "curves"):
        for record in getattr(model, name).values():
            getattr(result, name).add(record)
    return result


@unittest.skipUnless(get_native_capabilities()["swmm_solver"], "Native solver unavailable")
class NativeRegulatorTests(unittest.TestCase):
    solve = native_nodes.NativeNodeTests.solve

    def assert_process(self, actual, expected, units="CFS", *, tolerance=1e-8):
        context = UnitContext(flow_units=units)
        rules = Model().profile.unit_rules
        factors = {dimension: UnitContext().convert(1, dimension=dimension, to=context, rules=rules)
                   for dimension in ("length", "volume", "flow")}
        self.assertEqual(len(actual["history"]), len(expected["history"]))
        for a, b in zip(actual["history"], expected["history"]):
            self.assertEqual(a[0], b[0])
            for index, dimension in ((1, "length"), (2, "volume"), (3, "flow"), (4, "flow")):
                for value, reference in zip(a[index], b[index]):
                    self.assertAlmostEqual(value / factors[dimension], reference, delta=tolerance)
        for a, b in zip(actual["balance"], expected["balance"]):
            self.assertAlmostEqual(a, b, delta=1e-7)

    def test_all_link_variants_and_optional_forms_preserve_native_trajectories(self):
        with tempfile.TemporaryDirectory() as directory:
            for kind, routing, units in product(KINDS, ("STEADY", "KINWAVE", "DYNWAVE"), ("CFS", "CMS")):
                with self.subTest(kind=kind, routing=routing, units=units):
                    model = regulator_model(kind)
                    model.reinterpret_units(units)
                    model.update_options(flow_routing=routing)
                    if isinstance(model.links["P"], n.Pump):
                        raw = "P J O" + (" *" if kind == "IDEAL" else " Characteristic") + " ON 1.5 .1"
                    elif kind in ("SIDE", "BOTTOM"):
                        raw = f"P J O {kind} .2 .65 YES .0125"
                    elif isinstance(model.links["P"], n.Weir):
                        raw = f"P J O {kind} .2 3.1 NO 1.5 2.4 NO"
                        if kind == "ROADWAY":
                            raw += " 20 GRAVEL"
                    elif kind.startswith("FUNCTIONAL"):
                        raw = f"P J O .2 {kind} 2 1.4 YES"
                    else:
                        raw = f"P J O .2 {kind} Rating YES"
                    original = model.to_document()
                    source = "\n".join(raw if line.kind == "data" and line.section == model.links["P"].kind else line.content
                                         for line in original.lines) + "\n"
                    parsed = Model.from_document(InpDocument.from_text(source), strict=True)
                    self.assertTrue(parsed.validate(for_run=True).is_valid)
                    # DWF is a future consumer codec. Append identical native forcing after model transformations.
                    extra = "[DWF]\nJ FLOW 2\n"
                    expected = self.solve(directory, "raw", source + extra)
                    actual = self.solve(directory, "rebuilt", rebuilt(parsed).to_document().text + extra)
                    self.assertEqual(actual, expected)
                    self.assertGreater(max(row[3][0] for row in expected["history"]), 0)

    def test_all_six_flow_units_preserve_dynamic_regulator_hydraulics(self):
        with tempfile.TemporaryDirectory() as directory:
            # Non-storage inlet isolates link conversion from the known native storage volume-factor discrepancy.
            for kind in KINDS:
                for units in ("CFS", "GPM", "MGD", "CMS", "LPS", "MLD"):
                    with self.subTest(kind=kind, units=units):
                        model = regulator_model(kind, storage=False)
                        expected = self.solve(directory, "reference", model.to_document().text + "[DWF]\nJ FLOW 2\n")
                        model.convert_units(units)
                        factor = UnitContext().convert(2, dimension="flow", to=model.units, rules=model.profile.unit_rules)
                        actual = self.solve(directory, "converted", model.to_document().text + f"[DWF]\nJ FLOW {factor!r}\n")
                        self.assert_process(actual, expected, units)

    def test_weir_coefficient_curves_and_zero_end_default(self):
        with tempfile.TemporaryDirectory() as directory:
            for kind in ("TRANSVERSE", "SIDEFLOW", "V-NOTCH", "TRAPEZOIDAL", "ROADWAY"):
                model = regulator_model(kind, storage=False)
                model.curves.add(curve("Cd", "WEIR", ((0, 2), (1, 2.7), (4, 3.5))))
                model.links.update("P", coefficient_curve=Ref(collection="swmm:curves", key="Cd"))
                text = model.to_document().text
                expected = self.solve(directory, "curve", text + "[DWF]\nJ FLOW 2\n")
                parsed = Model.from_document(InpDocument.from_text(text), strict=True)
                self.assertEqual(self.solve(directory, "rebuilt", rebuilt(parsed).to_document().text + "[DWF]\nJ FLOW 2\n"), expected)
                model.convert_units("CMS")
                self.assert_process(self.solve(directory, "metric", model.to_document().text + "[DWF]\nJ FLOW .05664\n"), expected, "CMS")
            model = regulator_model("TRAPEZOIDAL")
            baseline = self.solve(directory, "default_end", model.to_document().text)
            model.links.update("P", end_coefficient=0)
            self.assertEqual(self.solve(directory, "zero_end", model.to_document().text), baseline)
            model.links.update("P", end_coefficient=3.1)
            self.assertNotEqual(self.solve(directory, "copied_end", model.to_document().text)["history"], baseline["history"])

    def test_roadway_surfaces_and_ignored_optional_parameters(self):
        with tempfile.TemporaryDirectory() as directory:
            for surface in (None, "PAVED", "GRAVEL"):
                model = regulator_model("ROADWAY", storage=False)
                model.links.update("P", road_width=20 if surface else None, road_surface=surface)
                expected = self.solve(directory, "road", model.to_document().text + "[DWF]\nJ FLOW 2\n")
                model.links.update("P", gated=True, can_surcharge=True, end_coefficient=9, end_contractions=2)
                self.assertEqual(self.solve(directory, "ignored", model.to_document().text + "[DWF]\nJ FLOW 2\n"), expected)
                model.convert_units("CMS")
                self.assert_process(self.solve(directory, "metric", model.to_document().text + "[DWF]\nJ FLOW .05664\n"), expected, "CMS")

    def test_offset_modes_and_node_invert_marker_preserve_all_link_variants(self):
        with tempfile.TemporaryDirectory() as directory:
            for kind in KINDS:
                with self.subTest(kind=kind):
                    model = regulator_model(kind)
                    expected = self.solve(directory, "depth", model.to_document().text + "[DWF]\nJ FLOW 2\n")
                    model.convert_link_offsets("ELEVATION")
                    self.assert_process(self.solve(directory, "elevation", model.to_document().text + "[DWF]\nJ FLOW 2\n"), expected)
                    if not isinstance(model.links["P"], n.Pump):
                        field = "crest_height" if isinstance(model.links["P"], n.Weir) else "offset"
                        model.links.update("P", **{field: Offset.NODE_INVERT})
                        marker = self.solve(directory, "marker", model.to_document().text + "[DWF]\nJ FLOW 2\n")
                        model.convert_link_offsets("DEPTH")
                        self.assertEqual(self.solve(directory, "zero", model.to_document().text + "[DWF]\nJ FLOW 2\n"), marker)

    def test_orifice_shapes_zero_coefficient_and_timed_opening(self):
        # A real control action exercises Orate, rather than merely accepting a dormant parameter.
        controls = "[CONTROLS]\nRULE Open\nIF SIMULATION TIME > 0:01\nTHEN ORIFICE P SETTING = 1\nELSE ORIFICE P SETTING = 0\n"
        with tempfile.TemporaryDirectory() as directory:
            for orientation in ("SIDE", "BOTTOM"):
                for geometry in (Circular(diameter=1), RectClosed(full_depth=1, width=2)):
                    model = regulator_model(orientation)
                    model.links.update("P", opening_time=timedelta(seconds=60), section=CrossSection(geometry=geometry))
                    text = model.to_document().text
                    expected = self.solve(directory, "timed", text + controls)
                    self.assertEqual(self.solve(directory, "rebuilt", rebuilt(Model.from_document(InpDocument.from_text(text), strict=True)).to_document().text + controls), expected)
                    model.links.update("P", opening_time=timedelta(0))
                    self.assertNotEqual(self.solve(directory, "instant", model.to_document().text + controls)["history"], expected["history"])
                    model.links.update("P", coefficient=0)
                    self.assertTrue(all(row[3][0] == 0 for row in self.solve(directory, "closed", model.to_document().text)["history"]))

    def test_native_topology_and_pump_limit_errors_match_preflight(self):
        with tempfile.TemporaryDirectory() as directory:
            for kind in ("SIDE", "TRANSVERSE", "FUNCTIONAL/HEAD"):
                model = regulator_model(kind, storage=False)
                model.update_options(flow_routing="KINWAVE")
                self.assertIn("regulator.requires_storage", {d.code for d in model.validate(for_run=True).errors})
                error, report = self.solve(directory, "bad_tree", model.to_document().text, allow_error=True)
                self.assertNotEqual(error, 0)
                self.assertIn("ERROR 139", report)
            model = regulator_model("PUMP2")
            model.links.update("P", startup_depth=1, shutoff_depth=2)
            self.assertIn("pump.invalid_depth_limits", {d.code for d in model.validate(for_run=True).errors})
            error, report = self.solve(directory, "bad_limits", model.to_document().text, allow_error=True)
            self.assertNotEqual(error, 0)
            self.assertIn("ERROR 122", report)

    def test_reverse_flow_and_flap_gate_for_each_regulator_family(self):
        with tempfile.TemporaryDirectory() as directory:
            for kind in ("SIDE", "BOTTOM", "TRANSVERSE", "SIDEFLOW", "V-NOTCH", "TRAPEZOIDAL", "ROADWAY",
                         "FUNCTIONAL/HEAD", "FUNCTIONAL/DEPTH", "TABULAR/HEAD", "TABULAR/DEPTH"):
                with self.subTest(kind=kind):
                    model = regulator_model(kind)
                    model.nodes.update("J", initial_depth=.5)
                    model.nodes.update("O", boundary=n.FixedBoundary(stage=14))
                    expected = self.solve(directory, "reverse", model.to_document().text)
                    self.assertLess(min(row[3][0] for row in expected["history"]), 0)
                    model.links.update("P", gated=True)
                    gated = self.solve(directory, "gated", model.to_document().text)
                    if kind == "ROADWAY":
                        self.assertEqual(gated, expected)
                    else:
                        self.assertTrue(all(row[3][0] == 0 for row in gated["history"]))

    def test_pump_status_and_start_stop_depths_change_real_pumping(self):
        with tempfile.TemporaryDirectory() as directory:
            for kind in ("PUMP1", "PUMP2", "PUMP3", "PUMP4", "PUMP5"):
                model = regulator_model(kind)
                model.links.update("P", initially_on=False)
                off = self.solve(directory, "off", model.to_document().text)
                self.assertTrue(all(row[3][0] == 0 for row in off["history"]))
                model.links.update("P", initially_on=False, startup_depth=1.5, shutoff_depth=1)
                text = model.to_document().text
                automatic = self.solve(directory, "automatic", text)
                self.assertGreater(max(row[3][0] for row in automatic["history"]), 0)
                self.assertEqual(automatic["history"][-1][3][0], 0)
                self.assertEqual(self.solve(directory, "rebuilt", rebuilt(Model.from_document(InpDocument.from_text(text), strict=True)).to_document().text), automatic)

    def test_weir_surcharge_and_end_contractions_are_active(self):
        with tempfile.TemporaryDirectory() as directory:
            for kind in ("TRANSVERSE", "SIDEFLOW", "V-NOTCH", "TRAPEZOIDAL"):
                model = regulator_model(kind)
                model.nodes.update("J", initial_depth=5)
                baseline = self.solve(directory, "surcharge", model.to_document().text)
                model.links.update("P", can_surcharge=False)
                limited = self.solve(directory, "limited", model.to_document().text)
                self.assertNotEqual(baseline["history"], limited["history"])
            model = regulator_model("TRANSVERSE")
            baseline = self.solve(directory, "no_contractions", model.to_document().text)
            model.links.update("P", end_contractions=1.5)
            self.assertNotEqual(self.solve(directory, "contractions", model.to_document().text)["history"], baseline["history"])

    def test_regulator_ignored_section_slots_are_not_conduit_parameters(self):
        with tempfile.TemporaryDirectory() as directory:
            for kind, original, extra in (("SIDE", "CIRCULAR 1 0 0 0", "CIRCULAR 1 9 8 7 ignored ignored"),
                                          ("TRANSVERSE", "RECT_OPEN 3 2 0 0", "RECT_OPEN 3 2 9 17 ignored ignored")):
                source = regulator_model(kind).to_document().text.replace(original, extra)
                parsed = Model.from_document(InpDocument.from_text(source), strict=True)
                expected = self.solve(directory, "ignored_section", source)
                self.assertEqual(self.solve(directory, "normalized", parsed.to_document(normalize=True).text), expected)


if __name__ == "__main__":
    unittest.main()
