"""Storage and diversion behavior against the pinned native solver."""

from dataclasses import fields
from itertools import product
import math
from pathlib import Path
import tempfile
import unittest

from easysewer import get_native_capabilities
from easysewer.io.inp import InpDocument
from easysewer.model import Model, Ref
from easysewer.model import network as n
from easysewer.model.resources import Curve, CurvePoint
from easysewer.model.units import UnitContext
from test_nodes_v2 import CURVE, SHAPES, divider_model, storage_model


def circular_area_fraction(depth, diameter):
    theta = 2 * math.acos(1 - 2 * max(0, min(1, depth / diameter)))
    return (theta - math.sin(theta)) / (2 * math.pi)


@unittest.skipUnless(get_native_capabilities()["swmm_solver"], "Native solver unavailable")
class NativeNodeTests(unittest.TestCase):
    def solve(self, directory, name, source, *, nodes=("J", "O"), links=("P",), allow_error=False):
        from easysewer.runtime._solver_api import SWMMSolverAPI
        base = Path(directory) / name
        inp, rpt, out = (base.with_suffix(suffix) for suffix in (".inp", ".rpt", ".out"))
        inp.write_text(source, encoding="utf-8")
        solver = SWMMSolverAPI()
        if solver.get_version() != 52004:
            self.skipTest("Fixtures require SWMM 5.2.4")
        started = False
        try:
            error = solver.open(str(inp), str(rpt), str(out))
            if error:
                solver.close()
                if allow_error:
                    return error, rpt.read_text(errors="replace")
                self.fail(f"SWMM open {error}: {rpt.read_text(errors='replace')}")
            error = solver.start(1)
            if error:
                solver.close()
                if allow_error:
                    return error, rpt.read_text(errors="replace")
                self.fail(f"SWMM start {error}: {rpt.read_text(errors='replace')}")
            started = True
            ni = [solver.get_index(2, name) for name in nodes]
            li = [solver.get_index(3, name) for name in links]
            self.assertTrue(all(i >= 0 for i in ni + li))
            initial = tuple((solver.get_value(302, i), solver.get_value(303, i), solver.get_value(305, i)) for i in ni)
            parameters = tuple(tuple(solver.get_value(code, i) for code in (403, 404, 405, 406)) for i in li)
            history = []
            for _ in range(10000):
                error, elapsed = solver.step()
                self.assertEqual(error, 0)
                if elapsed == 0:
                    break
                history.append((elapsed * 86400, tuple(solver.get_value(303, i) for i in ni),
                                tuple(solver.get_value(305, i) for i in ni), tuple(solver.get_value(410, i) for i in li),
                                tuple(solver.get_value(308, i) for i in ni)))
            else:
                self.fail("Fixture exceeded step bound")
            self.assertGreater(len(history), 10)
            self.assertEqual(solver.end(), 0)
            started = False
            return dict(initial=initial, parameters=parameters, history=history, balance=solver.get_mass_bal_err())
        finally:
            if started:
                solver.end()
            solver.close()

    def test_all_storage_shapes_defaults_and_seepage_forms_preserve_native_processes(self):
        with tempfile.TemporaryDirectory() as directory:
            for units, routing in product(("CFS", "CMS"), ("STEADY", "KINWAVE", "DYNWAVE")):
                for kind, parameters in SHAPES.items():
                    for index, tail in enumerate(("", " .5 .7", " .5 .7 .2", " .5 .7 3 .2 .3", " 0 0 0 0 0")):
                        if kind == "PARABOLOID" and index in (2, 3):
                            # Pinned native initializer omits this case: tested as a preflight error.
                            continue
                        with self.subTest(units=units, routing=routing, shape=kind, tail=tail):
                            base = storage_model()
                            base.reinterpret_units(units)
                            base.update_options(flow_routing=routing)
                            source = base.to_document().text
                            native_kind = "PARABOLIC" if kind == "PARABOLOID" else kind
                            source = "\n".join(f"J 10 5 2 {native_kind} {parameters}{tail}" if line.startswith("J 10 5 2 FUNCTIONAL") else line
                                               for line in source.splitlines()) + "\n"
                            if kind == "TABULAR":
                                source += CURVE
                            source += "[EVAPORATION]\nCONSTANT .1\n"
                            parsed = Model.from_document(InpDocument.from_text(source), strict=True)
                            self.assertTrue(parsed.validate(for_run=True).is_valid)
                            rebuilt = Model()
                            rebuilt.update_options(**{field.name: getattr(parsed.options, field.name) for field in fields(parsed.options)})
                            for record in parsed.curves.values():
                                rebuilt.curves.add(record)
                            for record in parsed.nodes.values():
                                rebuilt.nodes.add(record)
                            for record in parsed.links.values():
                                rebuilt.links.add(record)
                            rewritten = rebuilt.to_document().text + "[EVAPORATION]\nCONSTANT .1\n"
                            expected = self.solve(directory, "original", source)
                            actual = self.solve(directory, "rewritten", rewritten)
                            self.assertEqual(actual, expected)
                            self.assertGreater(expected["initial"][0][2], 0)
                            self.assertGreater(max(row[3][0] for row in expected["history"]), 0)

    def test_functional_special_cases_and_ignored_cylinder_slot(self):
        with tempfile.TemporaryDirectory() as directory:
            for kind, parameters in (("FUNCTIONAL", "0 0 100"), ("FUNCTIONAL", "5 0 100"),
                                     ("FUNCTIONAL", "5 1 100"), ("FUNCTIONAL", "-5 1 100"),
                                     ("FUNCTIONAL", "5 1.2 0"), ("CYLINDRICAL", "10 8 9")):
                model = storage_model()
                native_kind = "PARABOLIC" if kind == "PARABOLOID" else kind
                source = "\n".join(f"J 10 5 2 {native_kind} {parameters}" if line.startswith("J 10 5 2 FUNCTIONAL") else line
                                   for line in model.to_document().text.splitlines()) + "\n"
                parsed = Model.from_document(InpDocument.from_text(source), strict=True)
                self.assertEqual(self.solve(directory, "source", source),
                                 self.solve(directory, "normalized", parsed.to_document(normalize=True).text))

    def test_storage_surcharge_depth_is_not_a_ponding_area(self):
        model = storage_model(surcharge_depth=3)
        model.nodes.update("J", initial_depth=6)
        with tempfile.TemporaryDirectory() as directory:
            source = model.to_document().text
            self.assertTrue(model.validate(for_run=True).is_valid)
            result = self.solve(directory, "closed", source)
            self.assertEqual(result["initial"][0][1], 6)
            model.nodes.update("J", surcharge_depth=0)
            self.assertFalse(model.validate(for_run=True).is_valid)
            error, report = self.solve(directory, "open", model.to_document().text, allow_error=True)
            self.assertNotEqual(error, 0)
            self.assertIn("ERROR 138", report)

    def test_all_divider_laws_and_routing_modes_preserve_native_split(self):
        with tempfile.TemporaryDirectory() as directory:
            for kind in ("OVERFLOW", "CUTOFF", "TABULAR", "WEIR"):
                for routing in ("STEADY", "KINWAVE", "DYNWAVE"):
                    model = divider_model(kind, routing)
                    source = model.to_document().text + "[DWF]\nJ FLOW 5\n"
                    parsed = Model.from_document(InpDocument.from_text(source), strict=True)
                    self.assertTrue(parsed.validate(for_run=True).is_valid)
                    expected = self.solve(directory, "original", source, nodes=("J", "O", "O2"), links=("P", "D"))
                    actual = self.solve(directory, "written", parsed.to_document(normalize=True).text,
                                        nodes=("J", "O", "O2"), links=("P", "D"))
                    self.assertEqual(actual, expected)
                    self.assertGreater(max(row[3][1] for row in actual["history"]), 0)

    def test_divider_formulas_and_curves_convert_across_all_six_flow_units(self):
        with tempfile.TemporaryDirectory() as directory:
            for kind in ("OVERFLOW", "CUTOFF", "TABULAR", "WEIR"):
                for routing in ("STEADY", "KINWAVE", "DYNWAVE"):
                    base = divider_model(kind, routing)
                    expected = self.solve(directory, "baseline", base.to_document().text + "[DWF]\nJ FLOW 5\n",
                                          nodes=("J", "O", "O2"), links=("P", "D"))
                    sensitivity = None
                    if routing == "KINWAVE":
                        # Native's normalized-area Newton tolerance is .001.
                        # Measure an independent control: change only the CFS
                        # inflow by one ULP, with no model unit conversion.
                        sensitivity = [0.0] * 5
                        for direction in (-math.inf, math.inf):
                            control = self.solve(directory, "one_ulp", base.to_document().text +
                                                 f"[DWF]\nJ FLOW {math.nextafter(5, direction)!r}\n",
                                                 nodes=("J", "O", "O2"), links=("P", "D"))
                            for index in range(1, 5):
                                error = max(abs(a - b) for left, right in zip(expected["history"], control["history"])
                                            for a, b in zip(left[index], right[index]))
                                sensitivity[index - 1] = max(sensitivity[index - 1], error)
                            sensitivity[4] = max(sensitivity[4], *(abs(a-b) for a, b in zip(expected["balance"], control["balance"])))
                        self.assertGreater(sensitivity[2], .001)
                    for units in ("CFS", "GPM", "MGD", "CMS", "LPS", "MLD"):
                        with self.subTest(kind=kind, routing=routing, units=units):
                            model = base.copy()
                            model.convert_units(units)
                            lf = UnitContext().convert(1, dimension="length", to=model.units, rules=model.profile.unit_rules)
                            vf = UnitContext().convert(1, dimension="volume", to=model.units, rules=model.profile.unit_rules)
                            qf = UnitContext().convert(1, dimension="flow", to=model.units, rules=model.profile.unit_rules)
                            actual = self.solve(directory, "converted", model.to_document().text + f"[DWF]\nJ FLOW {5*qf!r}\n",
                                                nodes=("J", "O", "O2"), links=("P", "D"))
                            # Check native internal conduit geometry and full
                            # flows separately from convergence-sensitive routing.
                            for left, right in zip(expected["parameters"], actual["parameters"]):
                                for a, b, factor in zip(left, right, (lf, 1, lf, qf)):
                                    self.assertAlmostEqual(b / factor, a, delta=1e-10)
                            self.assertEqual(len(actual["history"]), len(expected["history"]))
                            for left, right in zip(expected["history"], actual["history"]):
                                self.assertEqual(left[0], right[0])
                                for index, factor in ((1, lf), (2, vf), (3, qf), (4, qf)):
                                    for item, (a, b) in enumerate(zip(left[index], right[index])):
                                        if sensitivity is not None and index == 1:
                                            diameter = (3, .5, 3)[item]
                                            if max(a, b / factor) > diameter:
                                                self.assertAlmostEqual(a, b / factor, delta=1e-8)
                                            else:
                                                # Use an area-based tolerance, matching native's convergence variable:
                                                # two solves, each with .001 normalized-area tolerance.
                                                self.assertAlmostEqual(circular_area_fraction(a, diameter),
                                                                       circular_area_fraction(b / factor, diameter), delta=.002)
                                        elif sensitivity is not None and index == 3:
                                            full_flow = expected["parameters"][item][3]
                                            self.assertAlmostEqual(a / full_flow, b / factor / full_flow, delta=.002)
                                        else:
                                            self.assertAlmostEqual(b / factor, a, delta=1e-8)
                            # For this low-flow kinematic fixture, bound cumulative
                            # continuity differences using the normalized flow/area
                            # tolerance over the known 600 s / 5 cfs water budget.
                            mass_limit = 1e-7
                            if sensitivity is not None:
                                capacity = sum(row[3] for row in expected["parameters"])
                                capacity_volume = sum(math.pi * row[2] ** 2 / 4 * row[0] for row in expected["parameters"])
                                mass_limit = .002 * (capacity / 5 + capacity_volume / (600 * 5)) * 100
                            for a, b in zip(actual["balance"], expected["balance"]):
                                self.assertAlmostEqual(a, b, delta=mass_limit)

    def test_dynamic_divider_matches_junction_and_minimal_defaults(self):
        model = divider_model("CUTOFF", "DYNWAVE")
        model.nodes.update("J", max_depth=None, initial_depth=None, surcharge_depth=None, ponded_area=None)
        with tempfile.TemporaryDirectory() as directory:
            extra = "[DWF]\nJ FLOW 5\n"
            original = self.solve(directory, "divider", model.to_document().text + extra,
                                  nodes=("J", "O", "O2"), links=("P", "D"))
            model.nodes.replace("J", n.Junction(id="J", elevation=10, max_depth=0, initial_depth=0, surcharge_depth=0, ponded_area=0))
            actual = self.solve(directory, "junction", model.to_document().text + extra,
                                nodes=("J", "O", "O2"), links=("P", "D"))
            self.assertEqual(actual, original)

    def test_storage_dimension_conversion_exposes_rounded_native_volume_factor(self):
        with tempfile.TemporaryDirectory() as directory:
            for kind, parameters in SHAPES.items():
                model = storage_model()
                native_kind = "PARABOLIC" if kind == "PARABOLOID" else kind
                source = "\n".join(f"J 10 5 2 {native_kind} {parameters}" if line.startswith("J 10 5 2 FUNCTIONAL") else line
                                   for line in model.to_document().text.splitlines()) + "\n" + (CURVE if kind == "TABULAR" else "")
                model = Model.from_document(InpDocument.from_text(source), strict=True)
                baseline = self.solve(directory, "us", source)
                model.convert_units("CMS")
                converted = self.solve(directory, "si", model.to_document().text)
                # Returned volumes are in model units. Geometry is scaled by
                # length**3, while the engine's output-volume factor is .02832.
                ratio = converted["initial"][0][2] / .02832 / baseline["initial"][0][2]
                self.assertAlmostEqual(ratio, .3048 ** 3 / .02832, delta=1e-12)
                self.assertIn("storage.native_volume_factor", {issue.code for issue in model.validate().diagnostics})
                model.convert_units("CFS")
                returned = self.solve(directory, "returned", model.to_document().text)
                for left, right in zip(baseline["history"], returned["history"]):
                    for i in range(1, 5):
                        for a, b in zip(left[i], right[i]):
                            self.assertAlmostEqual(a, b, delta=1e-8)

    def test_manual_paraboloid_keyword_requires_explicit_normalization(self):
        model = storage_model(n.ParaboloidStorage(top_major_axis=10, top_minor_axis=8, full_height=5))
        source = model.to_document().text.replace("PARABOLIC", "PARABOLOID")
        parsed = Model.from_document(InpDocument.from_text(source), strict=True)
        self.assertEqual(parsed.to_document().text, source)
        self.assertFalse(parsed.validate(for_run=True).is_valid)
        self.assertTrue(parsed.validate(for_run=True, normalize=True).is_valid)
        with tempfile.TemporaryDirectory() as directory:
            error, report = self.solve(directory, "manual", source, allow_error=True)
            self.assertNotEqual(error, 0)
            self.assertIn("ERROR 205", report)
            self.assertEqual(self.solve(directory, "normalized", parsed.to_document(normalize=True).text),
                             self.solve(directory, "created", model.to_document().text))

    def test_analytical_si_seepage_differs_from_equivalent_tabular_bottom(self):
        with tempfile.TemporaryDirectory() as directory:
            for units in ("CFS", "CMS"):
                model = storage_model(n.FunctionalStorage(coefficient=0, exponent=0, constant=100),
                                      seepage=n.Seepage(suction=3, conductivity=.2, initial_deficit=.3))
                model.reinterpret_units(units)
                analytical = self.solve(directory, "analytical", model.to_document().text)
                model.curves.add(Curve(id="Area", kind="STORAGE", points=(CurvePoint(x=0, y=100), CurvePoint(x=5, y=100))))
                model.nodes.update("J", shape=n.TabularStorage(curve=Ref(collection="swmm:curves", key="Area")))
                tabular = self.solve(directory, "tabular", model.to_document().text)
                if units == "CFS":
                    self.assertEqual(analytical, tabular)
                else:
                    self.assertEqual(analytical["initial"], tabular["initial"])
                    difference = max(abs(left[2][0] - right[2][0]) for left, right in zip(analytical["history"], tabular["history"]))
                    self.assertGreater(difference, 1e-6)

    def test_multiple_storage_node_order_survives_normalization_rename_and_other_edits(self):
        body = """[STORAGE]
A 10 5 2 CONICAL 10 8 1.5
[OUTFALLS]
OA 9 FREE
[STORAGE]
B 10 5 2 PYRAMIDAL 15 12 1.2
[OUTFALLS]
OB 9 FREE
[CONDUITS]
P A OA 100 .013 0 0
Q B OB 100 .013 0 0
[XSECTIONS]
P CIRCULAR 1 0 0 0
Q CIRCULAR 1 0 0 0
"""
        with tempfile.TemporaryDirectory() as directory:
            for routing in ("STEADY", "KINWAVE", "DYNWAVE"):
                source = f"[OPTIONS]\nFLOW_UNITS CFS\nFLOW_ROUTING {routing}\nEND_TIME 00:10:00\nROUTING_STEP 5\nVARIABLE_STEP 0\n" + body
                model = Model.from_document(InpDocument.from_text(source), strict=True)
                baseline = self.solve(directory, "source", source, nodes=("A", "B"), links=("P", "Q"))
                self.assertEqual(self.solve(directory, "normalized", model.to_document(normalize=True).text, nodes=("A", "B"), links=("P", "Q")), baseline)
                model.nodes.rename("B", "Tank")
                model.update_options(threads=1)
                self.assertEqual(self.solve(directory, "renamed", model.to_document().text, nodes=("A", "Tank"), links=("P", "Q")), baseline)
                reread = Model.from_document(model.to_document(), strict=True)
                self.assertEqual(list(reread.nodes), ["A", "OA", "Tank", "OB"])
                if routing != "DYNWAVE":
                    self.assertIn("storage.native_index_sensitive", {issue.code for issue in model.validate().diagnostics})
