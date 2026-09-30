"""Full native hydraulic comparisons for transects, streets and inlet capture."""

from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from easysewer import get_native_capabilities
from easysewer.io.inp import InpDocument
from easysewer.model import Model, Ref
from easysewer.model.geometry import Circular, CrossSection, Irregular, RectOpen, Street, Trapezoidal
from easysewer.model.network import Conduit, FreeBoundary, Junction, Outfall
from easysewer.model.resources import Curve, CurvePoint
from easysewer.model.surface import (
    CombinationInlet, CurbInlet, CustomInlet, GenericGrate, GrateInlet, InletDesign,
    InletUsage, SlottedInlet, StandardGrate, StreetSection,
    TransectPoint,
)
from test_options_v2 import network
from test_surface_v2 import transect


def surface_network(design, *, shape="STREET", sides=2, placement="ON_GRADE"):
    model = network()
    model.links.update("P", losses=None, initial_flow=None, maximum_flow=None, inlet_offset=0, outlet_offset=0)
    model.nodes.update("J", initial_depth=.1)
    model.nodes.add(Junction(id="C", elevation=8, max_depth=5, initial_depth=0))
    model.nodes.add(Outfall(id="Z", elevation=7, boundary=FreeBoundary()))
    model.links.add(Conduit(id="D", inlet=Ref(collection="swmm:nodes", key="C"), outlet=Ref(collection="swmm:nodes", key="Z"),
                           length=100, roughness=.013, inlet_offset=0, outlet_offset=0,
                           section=CrossSection(geometry=Circular(diameter=2))))
    if shape == "STREET":
        model.streets.add(StreetSection(id="Road", crown_width=10, curb_height=.5, cross_slope=2, road_roughness=.016,
                                       gutter_depression=.05, gutter_width=1, sides=sides, backing_width=2,
                                       backing_slope=4, backing_roughness=.03))
        model.links.update("P", section=CrossSection(geometry=Street(street=Ref(collection="swmm:streets", key="Road"))))
    else:
        geometry = (RectOpen(full_depth=2, width=3) if shape == "RECT_OPEN" else Circular(diameter=2) if shape == "CIRCULAR"
                    else Trapezoidal(full_depth=2, bottom_width=3, left_slope=1, right_slope=1))
        model.links.update("P", section=CrossSection(geometry=geometry))
    if isinstance(design, CustomInlet):
        kind = "RATING" if design.curve.key == "Rating" else "DIVERSION"
        model.curves.add(Curve(id=design.curve.key, kind=kind, points=(CurvePoint(x=0, y=0), CurvePoint(x=1, y=.5), CurvePoint(x=10, y=5))))
    model.inlets.add(InletDesign(id="I", design=design))
    model.inlet_usage.add(InletUsage(link=Ref(collection="swmm:links", key="P"), inlet=Ref(collection="swmm:inlets", key="I"),
                                    node=Ref(collection="swmm:nodes", key="C"), count=2, percent_clogged=20, maximum_flow=.4,
                                    local_depression=.03, local_width=1, placement=placement))
    return model


@unittest.skipUnless(get_native_capabilities()["swmm_solver"], "Native solver unavailable")
class NativeSurfaceTests(unittest.TestCase):
    def solve(self, directory, name, source, *, nodes=("J", "O"), links=("P",), allow_error=False):
        from easysewer.runtime._solver_api import SWMMSolverAPI
        base = Path(directory) / name
        inp, rpt, out = (base.with_suffix(suffix) for suffix in (".inp", ".rpt", ".out"))
        inp.write_text(source, encoding="utf-8")
        solver = SWMMSolverAPI()
        if solver.get_version() != 52004:
            self.skipTest("Fixtures require native SWMM 5.2.4")
        started = False
        try:
            error = solver.open(str(inp), str(rpt), str(out))
            if error:
                solver.close()
                if allow_error:
                    return error, rpt.read_text(errors="replace")
                self.fail(f"SWMM open {error}: {rpt.read_text(errors='replace')}")
            self.assertEqual(solver.start(1), 0)
            started = True
            node_indices = [solver.get_index(2, name) for name in nodes]
            link_indices = [solver.get_index(3, name) for name in links]
            self.assertTrue(all(index >= 0 for index in node_indices + link_indices))
            full_depths = tuple(solver.get_value(405, index) for index in link_indices)
            history = []
            for _ in range(10000):
                error, elapsed = solver.step()
                self.assertEqual(error, 0)
                if elapsed == 0:
                    break
                history.append((elapsed * 86400, tuple(solver.get_value(303, index) for index in node_indices),
                                tuple(solver.get_value(410, index) for index in link_indices),
                                tuple(solver.get_value(306, index) for index in node_indices)))
            else:
                self.fail("Run exceeded fixture step bound")
            self.assertGreater(len(history), 10)
            self.assertEqual(solver.end(), 0)
            started = False
            return {"depths": full_depths, "history": history, "balance": solver.get_mass_bal_err()}
        finally:
            if started:
                solver.end()
            solver.close()

    def transect_source(self, body, *, two=False):
        base = network().to_document().text
        lines = ["P IRREGULAR T1" if line.startswith("P TRAPEZOIDAL") else line for line in base.splitlines()]
        source = "\n".join(lines) + "\n"
        if two:
            source += "[OUTFALLS]\nO2 9 FREE\n[CONDUITS]\nP2 J O2 150 .013 0 0\n[XSECTIONS]\nP2 IRREGULAR T2\n"
        return source + "[TRANSECTS]\n" + body

    def test_two_transects_inherited_adjusted_roughness_and_nonzero_modifiers(self):
        body = "NC 0 0 .02\nX1 T1 3 0 10 0 0 4 1.5 .3\nGR 2 0 0 5 2 10\nNC 0 .05 0\nX1 T2 3 0 10 0 0 1 .8 -.2\nGR 3 0 0 5 3 10\n[REPORT]\n"
        source = self.transect_source(body, two=True)
        model = Model.from_document(InpDocument.from_text(source), strict=True)
        self.assertAlmostEqual(model.transects["T2"].roughness.channel, .04)
        with tempfile.TemporaryDirectory() as directory:
            baseline = self.solve(directory, "original", source, links=("P", "P2"))
            result = self.solve(directory, "canonical", model.to_document(normalize=True).text, links=("P", "P2"))
            self.assertEqual(result, baseline)
            self.assertAlmostEqual(result["depths"][0], 2)
            self.assertAlmostEqual(result["depths"][1], 3)

    def test_manual_extra_slot_is_preserved_as_actual_native_behavior(self):
        source = self.transect_source("NC .03 .03 .02\nX1 T1 3 0 10 0 0 0 2 3 99\nGR 2 0 0 5 2 10\n[REPORT]\n")
        model = Model.from_document(InpDocument.from_text(source), strict=True)
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(self.solve(directory, "original", source), self.solve(directory, "canonical", model.to_document(normalize=True).text))

    def test_repeated_transect_sections_resolve_extra_native_roughness_updates(self):
        body = "NC .03 .03 .02\nX1 T1 3 0 10 0 0 4 1.5 .3\nGR 2 0 0 5 2 10\n[TRANSECTS]\nNC .03 .03 .02\nX1 T2 3 0 10 0 0 1 1 0\nGR 3 0 0 5 3 10\n[REPORT]\n"
        source = self.transect_source(body, two=True)
        model = Model.from_document(InpDocument.from_text(source), strict=True)
        self.assertAlmostEqual(model.transects["T1"].roughness.channel, .04)
        self.assertTrue(model.validate(for_run=True).is_valid)
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(self.solve(directory, "repeated", source, links=("P", "P2")),
                             self.solve(directory, "canonical", model.to_document(normalize=True).text, links=("P", "P2")))

    def test_editing_other_sections_does_not_refinalize_transect_with_new_nc_values(self):
        source = self.transect_source("NC .03 .03 .02\nX1 T1 3 0 10 0 0 4 1.5 .3\nGR 2 0 0 5 2 10\nNC .05 .05 .05\n")
        model = Model.from_document(InpDocument.from_text(source), strict=True)
        self.assertTrue(model.validate(for_run=True).is_valid)
        model.update_options(threads=1)
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(self.solve(directory, "original", source), self.solve(directory, "edited_options", model.to_document().text))

    def test_eof_and_omitted_intermediate_nc_are_detected_and_repaired_explicitly(self):
        bodies = (("NC .03 .03 .02\nX1 T1 3 0 10 0 0 0 0 0\nGR 2 0 0 5 2 10\n", False),
                  ("NC .03 .03 .02\nX1 T1 3 0 10 0 0 0 0 0\nGR 2 0 0 5 2 10\nX1 T2 3 0 10 0 0 0 0 0\nGR 3 0 0 5 3 10\n[REPORT]\n", True))
        with tempfile.TemporaryDirectory() as directory:
            for index, (body, two) in enumerate(bodies):
                source = self.transect_source(body, two=two)
                model = Model.from_document(InpDocument.from_text(source), strict=True)
                self.assertFalse(model.validate(for_run=True).is_valid)
                self.assertTrue(model.validate(for_run=True, normalize=True).is_valid)
                self.assertIsInstance(self.solve(directory, f"invalid_{index}", source, allow_error=True), tuple)
                result = self.solve(directory, f"fixed_{index}", model.to_document(normalize=True).text,
                                    links=("P", "P2") if two else ("P",))
                self.assertGreater(max(row[2][0] for row in result["history"]), 0)

    def test_transect_unit_conversion_preserves_native_hydraulics(self):
        model = network()
        model.transects.add(replace(transect(), left_bank=2, right_bank=7,
            stations=tuple(TransectPoint(elevation=elevation, station=station) for elevation, station in
                           ((2, 0), (.4, 2), (0, 4), (.7, 7), (2, 11)))))
        model.links.update("P", section=CrossSection(geometry=Irregular(transect=Ref(collection="swmm:transects", key="T"))))
        before = model.to_document().text
        model.convert_units("CMS")
        with tempfile.TemporaryDirectory() as directory:
            baseline = self.solve(directory, "US", before)
            actual = self.solve(directory, "SI", model.to_document().text)
            model.convert_units("CFS")
            returned = self.solve(directory, "returned_US", model.to_document().text)
        self.assert_converted(actual, baseline)
        self.assert_converted(returned, baseline, length_factor=1, flow_factor=1)

    def assert_converted(self, actual, baseline, *, length_factor=.3048, flow_factor=.02832):
        self.assertEqual(len(actual["history"]), len(baseline["history"]))
        for expected, row in zip(baseline["history"], actual["history"]):
            self.assertEqual(row[0], expected[0])
            for index, factor in ((1, length_factor), (2, flow_factor), (3, flow_factor)):
                for before, after in zip(expected[index], row[index]):
                    self.assertAlmostEqual(after / factor, before, delta=1e-8)
        for before, after in zip(baseline["balance"], actual["balance"]):
            self.assertAlmostEqual(after, before, delta=1e-7)

    def test_all_inlet_designs_placements_and_capture_paths_roundtrip_native(self):
        grate = GrateInlet(kind="GRATE", length=2, width=1, grate=StandardGrate(kind="P_BAR-50"))
        curb = CurbInlet(kind="CURB", length=4, height=.5, throat="INCLINED")
        designs = (
            (grate, "STREET"), (replace(grate, grate=GenericGrate(open_fraction=.7, splash_velocity=4)), "STREET"),
            (curb, "STREET"), (SlottedInlet(length=2, width=.1), "STREET"),
            (CombinationInlet(grate=grate, curb=curb), "STREET"),
            (replace(grate, kind="DROP_GRATE"), "TRAPEZOIDAL"),
            (CurbInlet(kind="DROP_CURB", length=4, height=.5), "TRAPEZOIDAL"),
            (CustomInlet(curve=Ref(collection="swmm:curves", key="Diversion")), "STREET"),
            (CustomInlet(curve=Ref(collection="swmm:curves", key="Rating")), "TRAPEZOIDAL"),
            (replace(grate, kind="DROP_GRATE"), "RECT_OPEN"),
            (CurbInlet(kind="DROP_CURB", length=4, height=.5), "RECT_OPEN"),
            (CustomInlet(curve=Ref(collection="swmm:curves", key="Rating")), "CIRCULAR"),
            (replace(curb, throat="HORIZONTAL"), "STREET"), (replace(curb, throat="VERTICAL"), "STREET"),
            (replace(curb, throat=None), "STREET"),
            *((replace(grate, grate=StandardGrate(kind=kind)), "STREET") for kind in
              ("P_BAR-50X100", "P_BAR-30", "CURVED_VANE", "TILT_BAR-45", "TILT_BAR-30", "RETICULINE")),
        )
        with tempfile.TemporaryDirectory() as directory:
            for index, (design, shape) in enumerate(designs):
                for placement in ("AUTOMATIC", "ON_GRADE", "ON_SAG"):
                    with self.subTest(design=design, placement=placement):
                        model = surface_network(design, shape=shape, sides=1 + index % 2, placement=placement)
                        source = model.to_document().text + "[DWF]\nJ FLOW .5\n"
                        if isinstance(design, CombinationInlet):
                            lines = source.splitlines()
                            index = next(i for i, line in enumerate(lines) if line.startswith("I GRATE "))
                            lines[index], lines[index + 1] = lines[index + 1], lines[index]
                            source = "\n".join(lines) + "\n"
                        imported = Model.from_document(InpDocument.from_text(source), strict=True)
                        exported = imported.to_document(normalize=True).text
                        baseline = self.solve(directory, f"original_{index}_{placement}", source, nodes=("J", "C"), links=("P", "D"))
                        actual = self.solve(directory, f"canonical_{index}_{placement}", exported, nodes=("J", "C"), links=("P", "D"))
                        self.assertEqual(actual, baseline)
                        self.assertGreater(max(row[2][1] for row in actual["history"]), 0)

    def test_street_and_inlet_unit_conversion_preserves_capture(self):
        design = GrateInlet(kind="GRATE", length=2, width=1, grate=GenericGrate(open_fraction=.7, splash_velocity=4))
        model = surface_network(design, placement="ON_SAG")
        before = model.to_document().text + "[DWF]\nJ FLOW .5\n"
        model.convert_units("CMS")
        after = model.to_document().text + "[DWF]\nJ FLOW .01416\n"
        with tempfile.TemporaryDirectory() as directory:
            baseline = self.solve(directory, "US", before, nodes=("J", "C"), links=("P", "D"))
            actual = self.solve(directory, "SI", after, nodes=("J", "C"), links=("P", "D"))
        self.assert_converted(actual, baseline)

    def test_minimal_street_generic_grate_and_usage_defaults_match_explicit_values(self):
        design = GrateInlet(kind="GRATE", length=2, width=1, grate=GenericGrate(open_fraction=.7))
        model = surface_network(design)
        model.inlet_usage.update("P", count=None, percent_clogged=None, maximum_flow=None,
                                 local_depression=None, local_width=None, placement=None)
        model.streets.update("Road", gutter_depression=None, gutter_width=None, sides=None,
                             backing_width=None, backing_slope=None, backing_roughness=None)
        before = model.to_document().text + "[DWF]\nJ FLOW .5\n"
        model.inlet_usage.update("P", count=1, percent_clogged=0, maximum_flow=0,
                                 local_depression=0, local_width=0, placement="AUTOMATIC")
        model.streets.update("Road", gutter_depression=0, gutter_width=0, sides=2,
                             backing_width=0, backing_slope=0, backing_roughness=0)
        model.inlets.update("I", design=replace(design, grate=GenericGrate(open_fraction=.7, splash_velocity=0)))
        after = model.to_document().text + "[DWF]\nJ FLOW .5\n"
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(self.solve(directory, "implicit", before, nodes=("J", "C"), links=("P", "D")),
                             self.solve(directory, "explicit", after, nodes=("J", "C"), links=("P", "D")))

    def test_spaced_header_fails_preflight_until_supported_section_is_normalized(self):
        source = network().to_document().text + "[ PATTERNS ]\nP DAILY 1\n"
        model = Model.from_document(InpDocument.from_text(source), strict=True)
        self.assertFalse(model.validate(for_run=True).is_valid)
        self.assertTrue(model.validate(for_run=True, normalize=True).is_valid)
        with tempfile.TemporaryDirectory() as directory:
            self.assertIsInstance(self.solve(directory, "invalid", source, allow_error=True), tuple)
            self.assertGreater(len(self.solve(directory, "valid", model.to_document(normalize=True).text)["history"]), 10)


if __name__ == "__main__":
    unittest.main()
