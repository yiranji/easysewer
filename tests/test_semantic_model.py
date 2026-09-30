from dataclasses import dataclass, replace
from pathlib import Path
import tempfile
import unittest

from easysewer.io.inp import InpDocument
from easysewer.io.inp.geometry import CrossSectionCodec, GeometrySyntax, UnsupportedGeometry
from easysewer.model import Model, Ref
from easysewer.model import geometry as g
from easysewer.model.network import Junction, Conduit
from easysewer.validation import ValidationError


NETWORK = """; source header
[XSECTIONS]
P trapezoidal 1.8 2.3 0.5 1.5 2 4 ; shape
[VERTICES]
P 0 1
[JUNCTIONS]
J 2.0000000 4 ; junction
[CONDUITS]
P j O 123.12345678901234 .013 0 0
[OUTFALLS]
O 1 FREE NO
[VERTICES]
P 2 3
[LOSSES]
P 0.1 0.2 0.3 YES 0.000123456789
"""


class SemanticModelTests(unittest.TestCase):
    def read(self, text=NETWORK):
        return Model.from_document(InpDocument.from_text(text), strict=True)

    def test_out_of_order_relations_repeated_sections_and_unchanged_bytes(self):
        original = NETWORK.replace("\n", "\r\n").encode("utf-8-sig")
        model = Model.from_document(InpDocument.from_bytes(original), strict=True)
        self.assertEqual(model.to_document().to_bytes(), original)
        self.assertEqual(model.links["p"].section.geometry.left_slope, .5)
        self.assertEqual(len(model.links["p"].vertices), 2)
        self.assertEqual(model.links["p"].section.barrels, 2)
        self.assertEqual(model.links["p"].section.culvert, 4)
        self.assertEqual(model.links["p"].losses.seepage, .000123456789)

    def test_edit_preserves_unrelated_tokens_comments_and_opaque_records(self):
        model = self.read(NETWORK + "[FUTURE]\nexact  mystery   line ; untouched\n")
        old = model.links["P"]
        model.links.update("P", section=replace(old.section, geometry=replace(old.section.geometry, left_slope=.75)))
        output = model.to_document()
        self.assertIn("J 2.0000000 4 ; junction", output.text)
        self.assertIn("P j O 123.12345678901234 .013 0 0", output.text)
        self.assertIn("exact  mystery   line ; untouched", output.text)
        self.assertIn("; shape", output.text)
        reread = Model.from_document(output, strict=True)
        self.assertEqual(reread.links["P"].section.geometry.left_slope, .75)
        self.assertEqual(len(output.records("XSECTIONS")), 1)

    def test_rename_then_export_updates_references_without_duplicate_records(self):
        model = self.read()
        model.nodes.rename("j", "J-new")
        model.links.rename("P", "P-new")
        output = model.to_document()
        reread = Model.from_document(output, strict=True)
        self.assertEqual(list(reread.links), ["P-new"])
        self.assertEqual(reread.links["P-new"].inlet.key, "J-new")
        for section in ("JUNCTIONS", "CONDUITS", "XSECTIONS", "LOSSES"):
            self.assertEqual(len(output.records(section)), 1)
        self.assertEqual(len(output.records("VERTICES")), 2)
        self.assertIn("; shape", output.text)

    def test_delete_removes_all_owned_rows_and_preserves_comments(self):
        model = self.read()
        model.links.remove("P")
        output = model.to_document()
        for section in ("CONDUITS", "XSECTIONS", "LOSSES", "VERTICES"):
            self.assertEqual(len(output.records(section)), 0)
        self.assertIn("; shape", output.text)

    def test_unknown_data_prevents_unproven_identity_mutations(self):
        model = self.read(NETWORK + "[FUTURE]\nJ hidden-reference\n")
        for operation in (lambda: model.nodes.rename("J", "New"),
                          lambda: model.links.remove("P"),
                          lambda: model.nodes.add(Junction(id="New", elevation=0))):
            with self.assertRaises(ValidationError):
                operation()
        self.assertIn("model.partial_support", [item.code for item in model.validate().diagnostics])

    def test_invalid_draft_stays_inspectable_but_cannot_export(self):
        source = NETWORK.replace("trapezoidal", "future_shape")
        model = Model.from_document(InpDocument.from_text(source))
        self.assertIn("future_shape", model.document.text)
        self.assertIsNone(model.links["P"].section)
        with self.assertRaises(ValidationError):
            model.to_document()

    def test_duplicate_and_invalid_records_remain_source_owned_with_errors(self):
        for extra in ("[JUNCTIONS]\nj 8\n", "[LOSSES]\nP -1 0 0\n", "[XSECTIONS]\nP CIRCULAR 1 0 0 0\n"):
            model = Model.from_document(InpDocument.from_text(NETWORK + extra))
            self.assertFalse(model.validate().is_valid)
            self.assertTrue(model.support.opaque_records)
            self.assertEqual(model.document.text, NETWORK + extra)

    def test_new_model_semantic_roundtrip(self):
        model = Model()
        with model.transaction():
            model.nodes.add(Junction(id="A", elevation=1))
            model.nodes.add(Junction(id="B", elevation=0))
            model.links.add(Conduit(id="Pipe", inlet=Ref(collection="swmm:nodes", key="A"),
                                   outlet=Ref(collection="swmm:nodes", key="B"), length=12.34, roughness=.012,
                                   section=g.CrossSection(geometry=g.RectOpen(full_depth=2, width=3))))
        output = model.to_document()
        reread = Model.from_document(output, strict=True)
        self.assertEqual(reread.links["Pipe"].section.geometry.width, 3)
        self.assertEqual(reread.links["Pipe"].length, 12.34)

    def test_model_transaction_rolls_back_incomplete_network(self):
        model = self.read()
        with self.assertRaises(ValidationError):
            with model.transaction():
                model.nodes.rename("J", "New")
                model.links.update("P", section=None)
        self.assertIn("J", model.nodes)
        self.assertIsNotNone(model.links["P"].section)

    def test_source_and_graph_cloning_have_independent_exports(self):
        model = self.read()
        copied = model.copy()
        copied.links.update("P", length=888)
        self.assertEqual(model.to_document().text, NETWORK)
        self.assertEqual(Model.from_document(copied.to_document()).links["P"].length, 888)

    def test_export_failure_preserves_existing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "network.inp"
            path.write_bytes(b"original")
            model = self.read()
            model.links.update("P", length=float("nan"))
            with self.assertRaises(ValidationError):
                model.to_inp(path)
            self.assertEqual(path.read_bytes(), b"original")

    def test_new_rows_after_missing_terminal_newline(self):
        model = self.read(NETWORK.rstrip())
        model.links.rename("P", "Renamed")
        output = model.to_document()
        self.assertEqual(len(Model.from_document(output, strict=True).links), 1)
        self.assertEqual(len(output.records("LOSSES")), 1)


class GeometryCodecTests(unittest.TestCase):
    def test_all_26_shapes_have_typed_syntax_and_roundtrip(self):
        inputs = {
            "CIRCULAR": "1 0 0 0", "FORCE_MAIN": "1 120 0 0", "FILLED_CIRCULAR": "2 .1 0 0",
            "RECT_CLOSED": "2 3 0 0", "RECT_OPEN": "2 3 1 0", "TRAPEZOIDAL": "2 3 .5 1.5",
            "TRIANGULAR": "2 4 0 0", "HORIZ_ELLIPSE": "2 3 0 0", "VERT_ELLIPSE": "3 2 0 0",
            "ARCH": "2 3 0 0", "PARABOLIC": "2 4 0 0", "POWER": "2 3 .5 0",
            "RECT_TRIANGULAR": "2 3 .5 0", "RECT_ROUND": "2 3 0 0", "MODBASKETHANDLE": "2 3 0 0",
            "EGG": "2 0 0 0", "HORSESHOE": "2 0 0 0", "GOTHIC": "2 0 0 0", "CATENARY": "2 0 0 0",
            "SEMIELLIPTICAL": "2 0 0 0", "BASKETHANDLE": "2 0 0 0", "SEMICIRCULAR": "2 0 0 0",
            "CUSTOM": "2 Shape1 0 0", "IRREGULAR": "Transect1", "STREET": "Street1", "DUMMY": "0 0 0 0",
        }
        codec = CrossSectionCodec()
        self.assertEqual(set(inputs), codec.kinds)
        for kind, parameters in inputs.items():
            with self.subTest(kind=kind):
                section = codec.parse((kind, *parameters.split()))
                self.assertEqual(codec.parse(codec.format(section)), section)

    def test_standard_size_code_has_no_unit_conversion_or_mixed_dimensions(self):
        codec = CrossSectionCodec()
        for kind in ("HORIZ_ELLIPSE", "VERT_ELLIPSE", "ARCH"):
            first = codec.parse((kind, "3", "0", "0", "0"))
            alternate = codec.parse((kind, "99", "99", "3", "0"))
            self.assertEqual(first, alternate)
            self.assertEqual(first.geometry.size_code, 3)
            self.assertIsNone(first.geometry.full_depth)
            self.assertEqual(codec.parse(codec.format(first)), first)

    def test_numeric_tokens_follow_inp_grammar(self):
        codec = CrossSectionCodec()
        for token in ("1_0", "１", "nan", "inf"):
            with self.subTest(token=token), self.assertRaises(ValueError):
                codec.parse(("CIRCULAR", token, "0", "0", "0"))
        section = codec.parse(("RECT_OPEN", "1", "2", "0.5", "0"))
        self.assertEqual(section.geometry.ignored_sides, .5)

    def test_new_geometry_extension_requires_no_domain_changes(self):
        @dataclass(frozen=True, kw_only=True)
        class NewShape(g.Geometry):
            kind = "NEW_SHAPE"
            depth: float
        codec = CrossSectionCodec((GeometrySyntax(kind="NEW_SHAPE", record_type=NewShape, fields=("depth",)),))
        shape = codec.parse(("NEW_SHAPE", "2", "0", "0", "0"))
        self.assertEqual(shape.geometry.depth, 2)
        self.assertEqual(codec.parse(codec.format(shape)), shape)
        with self.assertRaises(UnsupportedGeometry):
            CrossSectionCodec().parse(codec.format(shape))


if __name__ == "__main__":
    unittest.main()
