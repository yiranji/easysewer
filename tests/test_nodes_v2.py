from dataclasses import replace
from datetime import time, timedelta
import unittest

from easysewer.io.inp import InpDocument
from easysewer.model import Model, Ref
from easysewer.model import network as n
from easysewer.model.geometry import Circular, CrossSection
from easysewer.model.resources import Curve, CurvePoint
from easysewer.model.values import Point
from easysewer.validation import ValidationError


SHAPES = {
    "FUNCTIONAL": "2 1.7 50", "CYLINDRICAL": "10 8 0", "CONICAL": "10 8 1.5",
    "PARABOLOID": "20 16 5", "PYRAMIDAL": "10 8 1.5", "TABULAR": "Area",
}
CURVE = "[CURVES]\nArea STORAGE 0 50\nArea 2 70\nArea 5 100\n"


def storage_model(shape=None, **kwargs):
    model = Model()
    model.update_options(end_time=time(0, 10), routing_step=timedelta(seconds=5), variable_step=0)
    model.nodes.add(n.Storage(id="J", elevation=10, max_depth=5, initial_depth=2,
                              shape=shape or n.FunctionalStorage(coefficient=2, exponent=1.7, constant=50), **kwargs))
    model.nodes.add(n.Outfall(id="O", elevation=9, boundary=n.FreeBoundary()))
    model.links.add(n.Conduit(id="P", inlet=Ref(collection="swmm:nodes", key="J"), outlet=Ref(collection="swmm:nodes", key="O"),
                             length=100, roughness=.013, inlet_offset=0, outlet_offset=0,
                             section=CrossSection(geometry=Circular(diameter=1))))
    return model


def divider_model(kind="WEIR", routing="KINWAVE"):
    model = Model()
    model.update_options(end_time=time(0, 10), flow_routing=routing,
                         routing_step=timedelta(seconds=5), variable_step=0)
    laws = {"OVERFLOW": n.OverflowDivider(), "CUTOFF": n.CutoffDivider(cutoff_flow=.4),
            "TABULAR": n.TabularDivider(curve=Ref(collection="swmm:curves", key="Div")),
            "WEIR": n.WeirDivider(minimum_flow=.3, height=2, coefficient=1.5)}
    model.nodes.add(n.Divider(id="J", elevation=10, diverted_link=Ref(collection="swmm:links", key="D"),
                             law=laws[kind], max_depth=5, initial_depth=0, surcharge_depth=.2, ponded_area=20))
    for name, elevation in (("O", 9), ("O2", 8)):
        model.nodes.add(n.Outfall(id=name, elevation=elevation, boundary=n.FreeBoundary()))
    # Intentionally insert the diverted conduit first: native must reorder it.
    for name, endpoint, diameter in (("D", "O2", 3), ("P", "O", .5)):
        model.links.add(n.Conduit(id=name, inlet=Ref(collection="swmm:nodes", key="J"),
                                 outlet=Ref(collection="swmm:nodes", key=endpoint), length=100, roughness=.013,
                                 inlet_offset=0, outlet_offset=0, section=CrossSection(geometry=Circular(diameter=diameter))))
    if kind == "TABULAR":
        model.curves.add(Curve(id="Div", kind="DIVERSION", points=tuple(CurvePoint(x=x, y=y) for x, y in ((0, 0), (2, .4), (10, 6)))))
    return model


class StorageTests(unittest.TestCase):
    def test_all_shapes_and_all_optional_forms_roundtrip(self):
        for kind, parameters in SHAPES.items():
            for tail in ("", " 0", " .5 .7", " .5 .7 .2", " .5 .7 3 .2 .3", " 0 0 0 0 0"):
                with self.subTest(shape=kind, tail=tail):
                    source = f"[COORDINATES]\nS 123 456\n[STORAGE]\nS 10 5 1 {kind} {parameters}{tail} ; storage\n" + (CURVE if kind == "TABULAR" else "")
                    model = Model.from_document(InpDocument.from_text(source), strict=True)
                    self.assertEqual(model.to_document().text, source)
                    reread = Model.from_document(model.to_document(normalize=True), strict=True)
                    self.assertEqual(reread.nodes["S"], model.nodes["S"])
                    self.assertEqual(model.nodes["S"].position, Point(x=123, y=456))
                    if not tail:
                        self.assertIsNone(model.nodes["S"].surcharge_depth)
                        self.assertIsNone(model.nodes["S"].evaporation_fraction)
                    if tail == " .5 .7 .2":
                        self.assertIsInstance(model.nodes["S"].seepage, n.ConstantSeepage)

    def test_surcharge_is_length_and_interior_seepage_defaults_are_explicit(self):
        model = storage_model(surcharge_depth=2, seepage=n.Seepage(suction=3, conductivity=.2, initial_deficit=.3))
        model.convert_units("CMS")
        record = model.nodes["J"]
        self.assertAlmostEqual(record.surcharge_depth, 2 * .3048)
        self.assertAlmostEqual(record.seepage.suction, 3 * 25.4)
        self.assertAlmostEqual(record.seepage.conductivity, .2 * 25.4)
        self.assertEqual(record.seepage.initial_deficit, .3)
        self.assertIsNone(record.evaporation_fraction)
        reread = Model.from_document(model.to_document(), strict=True)
        self.assertEqual(reread.nodes["J"].evaporation_fraction, 0)
        self.assertEqual(reread.nodes["J"].seepage, record.seepage)
        self.assertFalse(hasattr(record, "ponded_area"))

    def test_functional_coefficient_and_all_analytical_dimensions(self):
        for kind, parameters in SHAPES.items():
            if kind == "TABULAR":
                continue
            source = f"[STORAGE]\nS 10 5 1 {kind} {parameters}\n"
            model = Model.from_document(InpDocument.from_text(source), strict=True)
            old = model.nodes["S"].shape
            model.convert_units("CMS")
            shape = model.nodes["S"].shape
            if kind == "FUNCTIONAL":
                self.assertAlmostEqual(shape.coefficient, old.coefficient * .3048 ** (2 - old.exponent))
                self.assertEqual(shape.exponent, old.exponent)
            elif kind in ("CONICAL", "PYRAMIDAL"):
                self.assertEqual(shape.side_slope, old.side_slope)
            elif kind == "PARABOLOID":
                self.assertAlmostEqual(shape.full_height, 5 * .3048)
            model.convert_units("CFS")
            self.assertEqual(type(model.nodes["S"].shape), type(old))
            self.assertAlmostEqual(model.nodes["S"].max_depth, 5)

    def test_shared_storage_curve_converts_once_and_renames_transactionally(self):
        model = Model.from_document(InpDocument.from_text("[STORAGE]\nA 0 5 0 TABULAR Area\nB 1 5 0 TABULAR Area\n" + CURVE), strict=True)
        model.curves.rename("Area", "Shared")
        with self.assertRaises(ValidationError):
            model.curves.remove("Shared")
        model.convert_units("CMS")
        self.assertAlmostEqual(model.curves["Shared"].points[1].y, 70 * .3048 ** 2)
        self.assertEqual(len(model.resource_uses(Ref(collection="swmm:curves", key="Shared"))), 2)
        self.assertEqual(model.nodes["B"].shape.curve.key, "Shared")
        model.curves.update("Shared", kind="RATING")
        self.assertIn("resource.wrong_purpose", {issue.code for issue in model.validate().errors})

    def test_unknown_invalid_and_duplicate_storage_remain_source_owned(self):
        for body in ("S 0 5 0 FUTURE 1 2 3", "S 0 5 0 CYLINDRICAL 1 2 -1",
                     "S 0 5 0 FUNCTIONAL 1 2 -1", "S 0 5 0 PARABOLOID 1 2 0",
                     "S 0 5 0 CONICAL 1 2 0 0 0 3 1", "S 0 5 0 CONICAL 1 2 0 0 0 3 1 2"):
            source = "[STORAGE]\n" + body + "\n"
            model = Model.from_document(InpDocument.from_text(source))
            self.assertEqual(model.document.text, source)
            self.assertFalse(model.nodes)
            self.assertTrue(model.support.opaque_records)
        source = "[STORAGE]\nS 0 5 0 CYLINDRICAL 1 2 0\n[JUNCTIONS]\ns 4\n"
        model = Model.from_document(InpDocument.from_text(source))
        self.assertFalse(model.validate().is_valid)
        self.assertEqual(len(model.nodes), 1)

    def test_cylinder_ignored_parameter_is_diagnosed_and_canonicalized(self):
        source = "[STORAGE]\nS 0 5 1 CYLINDRICAL 10 8 7\n"
        model = Model.from_document(InpDocument.from_text(source), strict=True)
        self.assertIn("storage.ignored_shape_parameter", {issue.code for issue in model.validate().diagnostics})
        self.assertEqual(model.to_document().text, source)
        self.assertEqual(model.to_document(normalize=True).records("STORAGE")[0].values[-1], "0")

    def test_storage_run_checks_and_fixed_native_seepage_limit(self):
        for shape, code in ((n.FunctionalStorage(coefficient=1, exponent=-1, constant=50), "storage.singular_function"),
                            (n.FunctionalStorage(coefficient=-20, exponent=1, constant=50), "storage.invalid_function"),
                            (n.FunctionalStorage(coefficient=0, exponent=1, constant=0), "storage.invalid_function")):
            model = storage_model(shape)
            self.assertTrue(model.validate().is_valid)
            self.assertIn(code, {issue.code for issue in model.validate(for_run=True).errors})
        model = storage_model(n.ParaboloidStorage(top_major_axis=10, top_minor_axis=8, full_height=5), seepage=n.ConstantSeepage(conductivity=.2))
        self.assertIn("storage.native_paraboloid_seepage", {issue.code for issue in model.validate(for_run=True).errors})
        model.nodes.update("J", seepage=n.ConstantSeepage(conductivity=0))
        self.assertTrue(model.validate(for_run=True).is_valid)
        model.nodes.update("J", initial_depth=9)
        self.assertIn("storage.initial_depth", {issue.code for issue in model.validate(for_run=True).errors})

    def test_coefficient_conversion_overflow_rolls_back_whole_model(self):
        model = storage_model(n.FunctionalStorage(coefficient=1, exponent=1000, constant=50))
        before = model.to_document().text
        with self.assertRaises(ValidationError) as raised:
            model.convert_units("CMS")
        self.assertIn("units.coefficient_range", str(raised.exception))
        self.assertEqual(model.to_document().text, before)

    def test_node_kind_change_preserves_identity_order(self):
        model = Model.from_document(InpDocument.from_text("[JUNCTIONS]\nA 0\nB 1\nC 2\n"), strict=True)
        model.nodes.replace("A", n.Storage(id="A", elevation=0, max_depth=5, initial_depth=0,
                                           shape=n.CylindricalStorage(major_axis=10, minor_axis=8)))
        self.assertEqual(list(Model.from_document(model.to_document(), strict=True).nodes), ["A", "B", "C"])
        model.nodes.rename("A", "First")
        self.assertEqual(list(Model.from_document(model.to_document(), strict=True).nodes), ["First", "B", "C"])

    def test_invalid_draft_link_does_not_crash_divider_run_validation(self):
        model = divider_model()
        model.links.update("D", inlet="invalid")
        self.assertFalse(model.validate(for_run=True).is_valid)


class DividerTests(unittest.TestCase):
    def test_all_laws_native_placeholders_and_graph_renaming(self):
        for kind in ("OVERFLOW", "CUTOFF", "TABULAR", "WEIR"):
            with self.subTest(kind=kind):
                model = divider_model(kind)
                model.nodes.rename("J", "Split")
                model.links.rename("D", "Diverted")
                if kind == "TABULAR":
                    model.curves.rename("Div", "Diversion")
                reread = Model.from_document(model.to_document(), strict=True)
                self.assertEqual(reread.nodes["Split"], model.nodes["Split"])
                self.assertEqual(reread.nodes["Split"].diverted_link.key, "Diverted")
                self.assertTrue(reread.validate(for_run=True).is_valid)
                with self.assertRaises(ValidationError):
                    model.links.remove("Diverted")
        source = "[DIVIDERS]\nJ 0 * OVERFLOW\n"
        model = Model.from_document(InpDocument.from_text(source), strict=True)
        self.assertIsNone(model.nodes["J"].diverted_link)
        self.assertEqual(model.to_document(normalize=True).records("DIVIDERS")[0].values[2], "*")
        self.assertIn("divider.missing_link", {issue.code for issue in model.validate(for_run=True).errors})

    def test_weir_coefficient_has_flow_over_height_power_units(self):
        model = divider_model()
        model.convert_units("GPM")
        self.assertAlmostEqual(model.nodes["J"].law.coefficient, 1.5 * 448.831)
        model.convert_units("CMS")
        self.assertAlmostEqual(model.nodes["J"].law.coefficient, 1.5 * .02832 / .3048 ** 1.5)
        self.assertAlmostEqual(model.nodes["J"].law.height, 2 * .3048)
        self.assertAlmostEqual(model.nodes["J"].ponded_area, 20 * .3048 ** 2)
        self.assertAlmostEqual(model.nodes["J"].surcharge_depth, .2 * .3048)
        self.assertTrue(model.validate(for_run=True).is_valid)

    def test_routing_topology_and_weir_range_checks(self):
        model = divider_model()
        model.nodes.update("J", law=n.WeirDivider(minimum_flow=10, height=1, coefficient=2))
        self.assertIn("divider.invalid_weir_range", {issue.code for issue in model.validate(for_run=True).errors})
        model.nodes.update("J", law=n.OverflowDivider())
        model.links.add(replace(model.links["P"], id="Third"))
        self.assertIn("divider.too_many_outlets", {issue.code for issue in model.validate(for_run=True).errors})
        model.update_options(flow_routing="DYNWAVE")
        self.assertNotIn("divider.too_many_outlets", {issue.code for issue in model.validate(for_run=True).errors})
        self.assertIn("divider.inactive_law", {issue.code for issue in model.validate().diagnostics})
        model.links.update("D", inlet=Ref(collection="swmm:nodes", key="O"), outlet=Ref(collection="swmm:nodes", key="O2"))
        self.assertIn("divider.unattached_link", {issue.code for issue in model.validate(for_run=True).errors})

    def test_partial_optional_values_and_invalid_laws(self):
        model = divider_model("CUTOFF")
        model.nodes.update("J", max_depth=None, initial_depth=None, surcharge_depth=None, ponded_area=20)
        row = model.to_document().records("DIVIDERS")[0].values
        self.assertEqual(row[-4:], ("0", "0", "0", "20"))
        for body in ("J 0 * FUTURE 2", "J 0 * CUTOFF", "J 0 * WEIR 1 2", "J 0 * OVERFLOW 0 0 0 0 5"):
            model = Model.from_document(InpDocument.from_text("[DIVIDERS]\n" + body + "\n"))
            self.assertFalse(model.nodes)
            self.assertTrue(model.support.opaque_records)
