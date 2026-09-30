from dataclasses import dataclass, fields, replace
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from easysewer.io.inp import InpDocument
from easysewer.model import Model, Point, Ref
from easysewer.model.geometry import CrossSection, ForceMain, Trapezoidal
from easysewer.model.network import Conduit, Junction, Outfall, FreeBoundary, ConduitLosses
from easysewer.model.options import DayTime, Options
from easysewer.model.store import CollectionSpec
from easysewer.schema.option_profile import OPTION_DEFINITIONS
from easysewer.validation import ValidationError


ALL_OPTIONS = """[OPTIONS]
FLOW_UNITS CMS
INFILTRATION MODIFIED_GREEN_AMPT
FLOW_ROUTING DYNWAVE
LINK_OFFSETS DEPTH
FORCE_MAIN_EQUATION H-W
IGNORE_RAINFALL NO
IGNORE_SNOWMELT YES
IGNORE_GROUNDWATER NO
IGNORE_RDII NO
IGNORE_ROUTING NO
IGNORE_QUALITY YES
ALLOW_PONDING YES
SKIP_STEADY_STATE NO
SYS_FLOW_TOL 4
LAT_FLOW_TOL 3
START_DATE Jan-01-2020
START_TIME 00:00:00
END_DATE 01/02/2020
END_TIME 24:00:00
REPORT_START_DATE 01/01/2020
REPORT_START_TIME 00:00:00
SWEEP_START 01/01
SWEEP_END 12/31
DRY_DAYS 0.5
REPORT_STEP 00:01:00
WET_STEP 00:05:00
DRY_STEP 01:00:00
RULE_STEP 0.5
ROUTING_STEP .25
LENGTHENING_STEP .5
MINIMUM_STEP .1
VARIABLE_STEP .75
INERTIAL_DAMPING PARTIAL
NORMAL_FLOW_LIMITED BOTH
SURCHARGE_METHOD SLOT
MIN_SURFAREA 1.25
MIN_SLOPE .1
MAX_TRIALS 8
HEAD_TOLERANCE .0015
THREADS 1
SLOPE_WEIGHTING YES
COMPATIBILITY 4
TEMPDIR "temporary files"
"""


def network():
    model = Model()
    model.update_options(flow_units="CFS", start_date=date(2020, 1, 1), end_date=date(2020, 1, 1),
                         end_time=time(0, 10), routing_step=timedelta(seconds=5), variable_step=0)
    model.nodes.add(Junction(id="J", elevation=10, max_depth=5, initial_depth=1,
                             ponded_area=20, position=Point(x=1000, y=2000)))
    model.nodes.add(Outfall(id="O", elevation=9, boundary=FreeBoundary()))
    model.links.add(Conduit(id="P", inlet=Ref(collection="swmm:nodes", key="J"),
                           outlet=Ref(collection="swmm:nodes", key="O"), length=100, roughness=.013,
                           inlet_offset=.5, outlet_offset=.25, initial_flow=.1, maximum_flow=3,
                           section=CrossSection(geometry=Trapezoidal(full_depth=2, bottom_width=3,
                                                                     left_slope=.5, right_slope=1), barrels=2),
                           losses=ConduitLosses(entry=.1, exit=.2, average=.3, seepage=.04)))
    return model


class OptionsTests(unittest.TestCase):
    def test_all_43_options_are_typed_and_roundtrip(self):
        model = Model.from_document(InpDocument.from_text(ALL_OPTIONS), strict=True)
        self.assertEqual(len(OPTION_DEFINITIONS), 43)
        self.assertEqual({item.name for item in fields(Options)}, {item.field for item in OPTION_DEFINITIONS})
        self.assertFalse(model.support.opaque_records)
        self.assertEqual(model.to_document().text, ALL_OPTIONS)
        self.assertEqual(model.options.rule_step, timedelta(minutes=30))
        self.assertEqual(model.options.routing_step, timedelta(seconds=.25))
        self.assertEqual(model.options.end_time, DayTime(day_offset=1))
        model.update_options(rule_step=timedelta(seconds=20))
        exported = model.to_document()
        self.assertEqual(len(exported.records("OPTIONS")), 43)
        reread = Model.from_document(exported, strict=True)
        self.assertEqual(reread.options, model.options)

    def test_missing_and_explicit_zero_stay_distinct(self):
        model = Model()
        self.assertIsNone(model.options.rule_step)
        self.assertEqual(model.effective_options.values.rule_step, timedelta())
        self.assertIn("rule_step", model.effective_options.defaults_used)
        self.assertNotIn("RULE_STEP", model.to_document().text)
        model.update_options(rule_step=timedelta())
        self.assertNotIn("rule_step", model.effective_options.defaults_used)
        self.assertIn("RULE_STEP 00:00:00", model.to_document().text)
        model.update_options(rule_step=None)
        self.assertEqual(model.to_document().text, "")

    def test_explicit_defaults_do_not_depend_on_truthiness(self):
        model = Model.from_document(InpDocument.from_text("[OPTIONS]\nRULE_STEP 0\nVARIABLE_STEP 0\nALLOW_PONDING NO\n"))
        model.update_options(threads=1)
        exported = Model.from_document(model.to_document()).options
        self.assertEqual(exported.rule_step, timedelta())
        self.assertEqual(exported.variable_step, 0)
        self.assertIs(exported.allow_ponding, False)

    def test_native_time_precision_and_units(self):
        cases = {"0.5": timedelta(minutes=30), "25:01:02": timedelta(hours=25, minutes=1, seconds=2),
                 "0.0004": timedelta(seconds=1), "00:00:01.9": timedelta(seconds=1)}
        for token, expected in cases.items():
            with self.subTest(token=token):
                model = Model.from_document(InpDocument.from_text(f"[OPTIONS]\nRULE_STEP {token}\nROUTING_STEP 0.5\n"), strict=True)
                self.assertEqual(model.options.rule_step, expected)
                self.assertEqual(model.options.routing_step, timedelta(seconds=.5))
                if token in ("0.0004", "00:00:01.9"):
                    self.assertIn("options.native_coercion", [issue.code for issue in model.validate().diagnostics])
        model = Model.from_document(InpDocument.from_text("[OPTIONS]\nROUTING_STEP 00:00:02.9\n"), strict=True)
        self.assertEqual(model.options.routing_step, timedelta(seconds=2))

    def test_programmatic_unrepresentable_steps_are_rejected_before_mutation(self):
        model = Model()
        for changes in ({"rule_step": timedelta(seconds=.5)}, {"wet_step": timedelta()},
                        {"routing_step": timedelta(seconds=-1)}, {"min_slope": 100},
                        {"start_time": time(tzinfo=timezone.utc)}, {"start_date": datetime(2020, 1, 1)}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                model.update_options(**changes)
        self.assertEqual(model.options, Options())

    def test_repeated_options_preserve_source_then_collapse_on_edit(self):
        source = "[options]\nRULE_STEP .5 ; first\n[OPTIONS]\nRULE_STEP 00:00:20 ; final\n"
        model = Model.from_document(InpDocument.from_text(source), strict=True)
        self.assertEqual(model.options.rule_step, timedelta(seconds=20))
        self.assertEqual(model.to_document().text, source)
        model.update_options(rule_step=timedelta(seconds=30))
        output = model.to_document()
        self.assertEqual(len(output.records("OPTIONS")), 1)
        self.assertIn("; first", output.text)
        self.assertIn("; final", output.text)
        self.assertEqual(Model.from_document(output).options.rule_step, timedelta(seconds=30))

    def test_routing_none_side_effect_obeys_source_order_and_canonical_edits(self):
        for tail, ignored in (("FLOW_ROUTING NONE\nIGNORE_ROUTING NO\n", False),
                              ("IGNORE_ROUTING NO\nFLOW_ROUTING NONE\n", True)):
            source = "[OPTIONS]\nFLOW_ROUTING STEADY\n" + tail
            model = Model.from_document(InpDocument.from_text(source), strict=True)
            self.assertEqual(model.options.flow_routing, "STEADY")
            self.assertIs(model.options.ignore_routing, ignored)
            self.assertEqual(model.to_document().text, source)
            model.update_options(rule_step=timedelta(seconds=20))
            reread = Model.from_document(model.to_document(), strict=True)
            self.assertEqual(reread.options.flow_routing, "STEADY")
            self.assertIs(reread.options.ignore_routing, ignored)

    def test_calendar_and_subsecond_clock_roundtrip(self):
        model = Model()
        model.update_options(start_date=date(2020, 1, 1), start_time=time(0, 0, 0, 500000),
                             end_date=date(2020, 1, 1), end_time=DayTime(day_offset=1))
        reread = Model.from_document(model.to_document(), strict=True)
        self.assertEqual(reread.options, model.options)
        self.assertEqual(reread.effective_options.end, datetime(2020, 1, 2))

    def test_out_of_range_combined_calendar_returns_diagnostics(self):
        model = Model()
        model.update_options(end_date=date.max, end_time=DayTime(day_offset=1))
        self.assertIn("options.calendar_overflow", [issue.code for issue in model.validate(for_run=True).errors])
        with self.assertRaises(ValidationError):
            _ = model.effective_options

    def test_profile_defaults_adjustments_and_run_validation(self):
        model = Model()
        self.assertEqual(model.effective_options.values.variable_step, .75)
        self.assertEqual(model.effective_options.values.max_trials, 8)
        self.assertTrue(model.validate().is_valid)
        self.assertFalse(model.validate(for_run=True).is_valid)
        model.update_options(flow_units="CMS", start_date=date(2020, 1, 1), end_date=date(2020, 1, 1),
                             end_time=time(0, 2), dry_step=timedelta(seconds=1), wet_step=timedelta(seconds=10),
                             routing_step=timedelta(seconds=20), minimum_step=timedelta(seconds=30))
        effective = model.effective_options
        # Native double-precision date subtraction floors this period to 119 s.
        self.assertEqual(effective.values.report_step, timedelta(seconds=119))
        self.assertEqual(effective.values.routing_step, timedelta(seconds=10))
        self.assertEqual(effective.values.minimum_step, timedelta(seconds=10))
        self.assertEqual(effective.values.dry_step, timedelta(seconds=10))
        self.assertAlmostEqual(effective.values.min_surface_area, 12.566 * .3048 ** 2)
        self.assertAlmostEqual(effective.values.head_tolerance, .005 * .3048)
        self.assertIsNone(model.options.max_trials)
        self.assertTrue(model.validate(for_run=True).is_valid)

    def test_invalid_options_are_retained_and_unknown_options_block_unsafe_operations(self):
        for token in ("NaN", "-1", "00:60:00", "00:00:-1", "1_0"):
            source = f"[OPTIONS]\nRULE_STEP {token}\n"
            model = Model.from_document(InpDocument.from_text(source))
            self.assertEqual(model.document.text, source)
            self.assertFalse(model.validate().is_valid)
        model = Model.from_document(InpDocument.from_text("[OPTIONS]\nFUTURE_OPTION 5\nRULE_STEP 0\n"))
        model.update_options(rule_step=timedelta(seconds=1))
        self.assertIn("FUTURE_OPTION 5", model.to_document().text)
        with self.assertRaises(ValidationError):
            model.convert_units("CMS")

    def test_temporary_directory_rebases_without_touching_original_model(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "old").mkdir()
            (root / "new").mkdir()
            source = root / "old" / "model.inp"
            source.write_text('[OPTIONS]\nTEMPDIR "work files"\n', encoding="utf-8")
            model = Model.from_inp(source, strict=True)
            target = root / "new" / "model.inp"
            model.to_inp(target)
            moved = Model.from_inp(target, strict=True)
            self.assertEqual(moved.options.temp_directory.resolve(), model.options.temp_directory.resolve())
            self.assertEqual(model.options.temp_directory.path, "work files")


class PhysicalTransformTests(unittest.TestCase):
    def test_context_changes_cannot_bypass_explicit_operation(self):
        model = network()
        for field, value in (("flow_units", "CMS"), ("link_offsets", "ELEVATION"), ("force_main_equation", "D-W")):
            with self.subTest(field=field), self.assertRaises(ValidationError):
                model.collection("swmm:options").update("settings", **{field: value})
        self.assertEqual(model.units.flow_units, "CFS")
        model.reinterpret_units("CMS")
        self.assertEqual(model.links["P"].length, 100)
        with self.assertRaises(ValidationError):
            model.collection("swmm:options").remove("settings")

    def test_complete_network_unit_conversion_preserves_dimensions(self):
        model = network()
        model.convert_units("CMS")
        pipe = model.links["P"]
        self.assertAlmostEqual(pipe.length, 30.48)
        self.assertAlmostEqual(model.nodes["J"].ponded_area, 20 * .3048 ** 2)
        self.assertEqual(model.nodes["J"].position, Point(x=1000, y=2000))
        self.assertAlmostEqual(pipe.section.geometry.bottom_width, 3 * .3048)
        self.assertEqual(pipe.section.geometry.left_slope, .5)
        self.assertEqual(pipe.section.barrels, 2)
        self.assertAlmostEqual(pipe.losses.seepage, .04 * 25.4)
        self.assertAlmostEqual(pipe.initial_flow, .1 * .02832)
        model.convert_units("CFS")
        self.assertAlmostEqual(model.links["P"].length, 100)
        self.assertAlmostEqual(model.links["P"].initial_flow, .1)

    def test_engine_and_physical_unit_policies_are_explicit(self):
        model = network()
        model.convert_units("CMS", basis="physical")
        self.assertAlmostEqual(model.links["P"].initial_flow, .1 * .028316846592, places=15)
        before = model.links["P"]
        with self.assertRaises(ValueError):
            model.convert_units("CFS", basis="unknown")
        self.assertEqual(model.links["P"], before)

    def test_force_main_roughness_depends_on_equation(self):
        for equation, roughness, expected in (("H-W", 120, 120), ("D-W", .01, .254)):
            model = network()
            model.reinterpret_force_main_equation(equation)
            model.links.update("P", section=CrossSection(geometry=ForceMain(diameter=2, roughness=roughness)))
            model.convert_units("CMS")
            self.assertAlmostEqual(model.links["P"].section.geometry.roughness, expected)

    def test_offset_conversion_uses_each_endpoint_elevation(self):
        model = network()
        original = model.links["P"]
        model.convert_link_offsets("ELEVATION")
        self.assertEqual(model.links["P"].inlet_offset, 10.5)
        self.assertEqual(model.links["P"].outlet_offset, 9.25)
        model.convert_link_offsets("DEPTH")
        self.assertEqual(model.links["P"], original)

    def test_offset_marker_and_mode_validate_together(self):
        from easysewer.model import Offset
        model = network()
        model.reinterpret_link_offsets("ELEVATION")
        model.links.update("P", inlet_offset=Offset.NODE_INVERT)
        self.assertTrue(model.validate().is_valid)
        with self.assertRaises(ValidationError):
            model.reinterpret_link_offsets("DEPTH")
        self.assertEqual(model.options.link_offsets, "ELEVATION")
        model.convert_link_offsets("DEPTH")
        self.assertEqual(model.links["P"].inlet_offset, 0)

    def test_conversion_failure_rolls_back_before_any_numeric_change(self):
        @dataclass(frozen=True)
        class Unspecified:
            id: str
            coefficient: float
        model = network()
        model._store.register(CollectionSpec(key="test:unknown", record_type=Unspecified, key_of=lambda row: row.id))
        model.collection("test:unknown").add(Unspecified("x", 1))
        before = model.links["P"]
        with self.assertRaises(ValidationError):
            model.convert_units("CMS")
        self.assertEqual(model.units.flow_units, "CFS")
        self.assertEqual(model.links["P"], before)


if __name__ == "__main__":
    unittest.main()
