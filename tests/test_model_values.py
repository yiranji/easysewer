from dataclasses import replace
from datetime import timedelta
from decimal import localcontext
from pathlib import PureWindowsPath
import unittest

from easysewer.io.inp import DurationCodec
from easysewer.model import FileReference, Point, Ref, UnitContext
from easysewer.model.fields import validate_fields
from easysewer.model.geometry import Arch, Circular, CrossSection, FilledCircular, Trapezoidal
from easysewer.model.network import Conduit, Orifice
from easysewer.model.values import Offset


class ValueTests(unittest.TestCase):
    def test_duration_units_fractional_and_multiday(self):
        for unit, seconds in (("seconds", 1), ("minutes", 60), ("hours", 3600)):
            codec = DurationCodec(numeric_unit=unit)
            self.assertEqual(codec.parse("1.5"), timedelta(seconds=seconds * 1.5))
            duration = timedelta(hours=50, microseconds=123456)
            self.assertEqual(codec.parse(codec.format(duration)), duration)
        self.assertEqual(DurationCodec(numeric_unit="seconds").parse("1:30"), timedelta(minutes=90))

    def test_duration_rejects_invalid_or_unrepresentable_values(self):
        codec = DurationCodec(numeric_unit="seconds")
        for value in ("-1", "nan", "inf", "1:60", "1:00:60", "0.0000001", "1e9999999", "1e-9999999", " 1", "1_0", "１"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                codec.parse(value)
        with self.assertRaises(ValueError):
            DurationCodec(numeric_unit="seconds", resolution=timedelta(seconds=1)).parse("0.5")
        with self.assertRaises(ValueError):
            DurationCodec(numeric_unit="hours").format(timedelta(seconds=1), style="numeric")

    def test_duration_is_independent_of_callers_decimal_precision(self):
        codec = DurationCodec(numeric_unit="seconds")
        with localcontext() as context:
            context.prec = 3
            self.assertEqual(codec.parse("123456.123456"), timedelta(seconds=123456, microseconds=123456))
            self.assertEqual(codec.format(timedelta(seconds=123456, microseconds=123456), style="numeric"), "123456.123456")

    def test_physical_dimensions_do_not_conflate_areas_or_temperature(self):
        us, si = UnitContext(flow_units="CFS"), UnitContext(flow_units="CMS")
        self.assertAlmostEqual(us.convert(1, dimension="area", to=si), .09290304)
        self.assertAlmostEqual(us.convert(1, dimension="catchment_area", to=si), .40468564224)
        self.assertEqual(us.convert(32, dimension="temperature", to=si), 0)
        self.assertEqual(us.convert(1, dimension="rain_depth", to=si), 25.4)
        self.assertEqual(us.convert(1.5, dimension="run/rise", to=si), 1.5)
        with self.assertRaises(ValueError):
            us.convert(1, dimension="storage_coefficient", to=si)

    def test_all_flow_units_roundtrip(self):
        base = UnitContext(flow_units="CMS")
        for unit in ("CFS", "GPM", "MGD", "CMS", "LPS", "MLD"):
            other = UnitContext(flow_units=unit)
            value = base.convert(1, dimension="flow", to=other)
            self.assertAlmostEqual(other.convert(value, dimension="flow", to=base), 1)
        self.assertEqual(base.convert(1, dimension="flow", to=UnitContext(flow_units="LPS")), 1000)

    def test_file_rebasing_preserves_input_and_output_identity(self):
        for direction in ("input", "output"):
            reference = FileReference(path=r"雨量\data file.dat", base_directory=r"D:\old",
                                      flavor="windows", direction=direction)
            moved = reference.for_directory(r"D:\new\project")
            rebuilt = FileReference(path=moved, base_directory=r"D:\new\project", flavor="windows")
            self.assertEqual(reference.resolve(), rebuilt.resolve())
            self.assertEqual(reference.for_directory(r"C:\project"), r"D:\old\雨量\data file.dat")
            self.assertEqual(reference.for_directory(r"D:\new", policy="preserve"), reference.path)

    def test_posix_unc_and_unbound_paths(self):
        reference = FileReference(path="../data/a.dat", base_directory="/project/inp", flavor="posix")
        self.assertEqual(str(reference.resolve()), "/project/data/a.dat")
        self.assertEqual(reference.for_directory("/new"), "../project/data/a.dat")
        unc = FileReference(path=r"\\server\share\rain.dat", flavor="windows")
        self.assertEqual(unc.resolve(), PureWindowsPath(r"\\server\share\rain.dat"))
        self.assertEqual(unc.for_directory(r"D:\new"), unc.path)
        with self.assertRaises(ValueError):
            FileReference(path="file.dat").resolve()
        with self.assertRaises(ValueError):
            FileReference(path="D:rain.dat", flavor="windows")

    def test_geometry_cross_fields_and_type_validation(self):
        bad = (FilledCircular(diameter=1, filled_depth=1), Arch(),
               Arch(size_code=1, full_depth=2),
               Trapezoidal(full_depth=1, bottom_width=0, left_slope=0, right_slope=0),
               CrossSection(geometry="CIRCULAR"), Point(x=float("nan"), y=1))
        for value in bad:
            with self.subTest(value=value):
                self.assertTrue(tuple(validate_fields(value)))
        self.assertFalse(tuple(validate_fields(Arch(size_code=1))))

    def test_nested_diagnostics_and_offset_markers(self):
        conduit = Conduit(id="P", inlet=Ref(collection="swmm:nodes", key="A"),
                          outlet=Ref(collection="swmm:nodes", key="B"), length=10, roughness=.01,
                          inlet_offset=Offset.NODE_INVERT, outlet_offset=0,
                          section=CrossSection(geometry=Circular(diameter=-1)))
        issue = tuple(validate_fields(conduit))[0]
        self.assertEqual(issue.field, "section.geometry.diameter")
        self.assertEqual(issue.object_id, "P")
        self.assertFalse(tuple(validate_fields(replace(conduit, section=None))))

    def test_boolean_and_variant_values_are_not_numeric_placeholders(self):
        value = Orifice(id="O", inlet=Ref(collection="swmm:nodes", key="A"),
                        outlet=Ref(collection="swmm:nodes", key="B"), orientation="WRONG",
                        coefficient=.5, gated=1, opening_time=2)
        self.assertEqual({issue.field for issue in validate_fields(value)},
                         {"orientation", "gated", "opening_time"})


if __name__ == "__main__":
    unittest.main()
