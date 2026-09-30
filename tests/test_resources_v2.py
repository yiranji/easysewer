from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from easysewer.io.inp import InpDocument
from easysewer.io.inp.network import default_schema
from easysewer.model import Model, Ref, FileReference, CollectionSpec
from easysewer.model.fields import reference, validate_fields
from easysewer.model.geometry import CrossSection, Custom
from easysewer.model.network import Outfall, SeriesBoundary
from easysewer.model.resources import (
    CURVE_KINDS, Curve, CurvePoint, FileTimeSeries, InlineTimeSeries, Pattern, SeriesPoint,
)
from easysewer.model.usage import ResourceUse
from easysewer.schema import FeatureDescriptor, RegistryError
from easysewer.validation import ValidationError
from test_options_v2 import network


class ResourceTests(unittest.TestCase):
    def test_all_curve_kinds_multiline_pairs_and_semantic_edits(self):
        source = "[CURVES]\n" + "\n".join(f"c{i} {kind}\nc{i} 0 2 1 3\nc{i} 2 4 ; tail"
                                               for i, kind in enumerate(CURVE_KINDS)) + "\n"
        model = Model.from_document(InpDocument.from_text(source), strict=True)
        self.assertEqual(len(model.curves), 12)
        self.assertFalse(model.support.opaque_records)
        self.assertEqual(model.to_document().text, source)
        model.curves.rename("C0", "Storage")
        model.curves.update("Storage", points=(CurvePoint(x=0, y=1), CurvePoint(x=4, y=5)))
        output = model.to_document()
        reread = Model.from_document(output, strict=True)
        self.assertEqual(list(reread.curves.values()), list(model.curves.values()))
        self.assertEqual(len(output.records("CURVES")), 12 * 4 - 1)
        self.assertEqual(output.text.count("; tail"), 12)
        self.assertEqual(model.to_document().text, output.text)

    def test_calendar_continuation_is_per_series_and_retains_relative_prefix(self):
        source = """[OPTIONS]
START_DATE 01/01/2020
START_TIME 12:00
[TIMESERIES]
A 0 -5 00:01 -4
B 01/03/2020 0:00 10
A 01/01/2020 12:05 -3 12:10 -2
[TIMESERIES]
b 1:00 11
a 12:15 -1
"""
        model = Model.from_document(InpDocument.from_text(source), strict=True)
        self.assertEqual(model.timeseries["A"].points[0].time, timedelta())
        self.assertEqual(model.timeseries["a"].points[-1].time, datetime(2020, 1, 1, 12, 15))
        self.assertEqual(model.timeseries["B"].points[-1].time, datetime(2020, 1, 3, 1))
        self.assertEqual(model.to_document().text, source)
        model.timeseries.rename("A", "temperature")
        reread = Model.from_document(model.to_document(), strict=True)
        self.assertEqual(list(reread.timeseries.values()), list(model.timeseries.values()))

    def test_subsecond_decimal_hours_negative_and_over_24_hour_points(self):
        source = "[TIMESERIES]\nT -1 5 0.0001388888888888889 -1 25:01:02 3\n"
        model = Model.from_document(InpDocument.from_text(source), strict=True)
        self.assertEqual(model.timeseries["T"].points[1].time, timedelta(seconds=.5))
        model.timeseries.rename("T", "New")
        self.assertEqual(Model.from_document(model.to_document(), strict=True).timeseries["New"], model.timeseries["New"])
        coerced = Model.from_document(InpDocument.from_text("[TIMESERIES]\nT 00:00:01.9 5\n"))
        self.assertEqual(coerced.timeseries["T"].points[0].time, timedelta(seconds=1))
        self.assertIn("resource.time_precision", [item.code for item in coerced.validate().diagnostics])

    def test_invalid_whole_resources_remain_source_owned(self):
        cases = ("[CURVES]\nC SHAPE\nC 0 0\nC 1\n", "[CURVES]\nC SHAPE 1 2 0 1\n",
                 "[CURVES]\nC SHAPE 0 0\nC SHAPE 1 1\n", "[TIMESERIES]\nT 01/01/2020 0:00 2\nT 0:00 3\n",
                 '[TIMESERIES]\nT FILE a.dat\nT 0 1\n', '[TIMESERIES]\nT FILE a.dat\nT FILE b.dat\n')
        for source in cases:
            with self.subTest(source=source):
                model = Model.from_document(InpDocument.from_text(source))
                self.assertFalse(model.validate().is_valid)
                self.assertEqual(model.document.text, source)
                self.assertEqual(len(model.curves) + len(model.timeseries), 0)
        model = Model.from_document(InpDocument.from_text("[CURVES]\nC FUTURE 0 0\nC 1 1\n"))
        self.assertTrue(model.validate().is_valid)
        self.assertEqual(len(model.support.opaque_records), 2)

    def test_invalid_nested_drafts_produce_diagnostics_without_crashing(self):
        for resource in (Curve(id="C", kind="SHAPE", points=(CurvePoint(x="bad", y=0), CurvePoint(x=1, y=0))),
                         InlineTimeSeries(id="T", points=(SeriesPoint(time="bad", value=0),)),
                         InlineTimeSeries(id="T", points=(SeriesPoint(time=datetime.now(timezone.utc), value=0),))):
            with self.subTest(resource=resource):
                model = Model()
                (model.curves if isinstance(resource, Curve) else model.timeseries).add(resource)
                self.assertFalse(model.validate().is_valid)
                with self.assertRaises(ValidationError):
                    model.to_document()

    def test_mixed_time_validation_uses_full_simulation_start(self):
        model = Model()
        model.update_options(start_date=date(2020, 1, 1), start_time=time(12))
        model.timeseries.add(InlineTimeSeries(id="T", points=(SeriesPoint(time=timedelta(hours=1), value=1),
                                SeriesPoint(time=datetime(2020, 1, 1, 12, 30), value=2))))
        self.assertFalse(model.validate().is_valid)

    def test_patterns_preserve_explicit_length_and_resolve_defaults(self):
        source = "[PATTERNS]\nP DAILY 0 .5\nP 2\nM MONTHLY\nH HOURLY " + " ".join(["1"] * 26) + "\nW WEEKEND 2\n"
        model = Model.from_document(InpDocument.from_text(source), strict=True)
        self.assertEqual(model.patterns["P"].factors, (0, .5, 2))
        self.assertEqual(model.patterns["P"].effective_factors, (0, .5, 2, 1, 1, 1, 1))
        self.assertEqual(model.patterns["M"].factors, ())
        self.assertEqual(model.patterns["M"].effective_factors, (1,) * 12)
        self.assertEqual(len(model.patterns["H"].factors), 24)
        model.patterns.rename("M", "Monthly")
        self.assertEqual(list(Model.from_document(model.to_document()).patterns.values()), list(model.patterns.values()))

    def test_file_series_rebases_without_loading_or_replacing_shared_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "new").mkdir()
            origin = root / "source.inp"
            origin.write_text('[TIMESERIES]\nF FILE "missing data.dat"\nT 0 -5 1 -4\n', encoding="utf-8")
            model = Model.from_inp(origin, strict=True)
            original = model.timeseries["F"]
            model.to_inp(root / "new" / "target.inp")
            reread = Model.from_inp(root / "new" / "target.inp", strict=True)
            self.assertEqual(reread.timeseries["F"].file.resolve(), original.file.resolve())
            self.assertEqual(model.timeseries["F"], original)
            self.assertEqual(reread.timeseries["T"], model.timeseries["T"])

    def test_shared_series_references_rename_and_delete_are_safe(self):
        model = Model()
        model.timeseries.add(InlineTimeSeries(id="T", points=(SeriesPoint(time=timedelta(), value=1),)))
        for name in ("O1", "O2"):
            model.nodes.add(Outfall(id=name, elevation=0, boundary=SeriesBoundary(series=Ref(collection="swmm:timeseries", key="T"))))
        with self.assertRaises(ValidationError):
            model.timeseries.remove("T")
        model.timeseries.rename("T", "Stage")
        self.assertEqual(len(model.resource_uses(Ref(collection="swmm:timeseries", key="Stage"))), 2)
        reread = Model.from_document(model.to_document(), strict=True)
        self.assertEqual(reread.nodes["O2"].boundary.series.key, "Stage")

    def test_curve_purpose_is_checked_at_each_consumer(self):
        model = network()
        model.curves.add(Curve(id="C", kind="TIDAL", points=(CurvePoint(x=0, y=0), CurvePoint(x=1, y=1))))
        model.links.update("P", section=CrossSection(geometry=Custom(full_depth=2, curve=Ref(collection="swmm:curves", key="C"))))
        self.assertIn("resource.wrong_purpose", [item.code for item in model.validate().errors])
        model.curves.update("C", kind="SHAPE")
        self.assertTrue(model.validate().is_valid)
        model.curves.rename("C", "Shape")
        self.assertEqual(model.links["P"].section.geometry.curve.key, "Shape")

    def test_curve_and_bound_series_dimensions_convert_once(self):
        model = network()
        model.curves.add(Curve(id="S", kind="STORAGE", points=(CurvePoint(x=2, y=20),)))
        model.curves.add(Curve(id="P", kind="PUMP1", points=(CurvePoint(x=10, y=2),)))
        model.timeseries.add(InlineTimeSeries(id="T", points=(SeriesPoint(time=timedelta(), value=10),)))
        for name in ("O", "O2"):
            value = Outfall(id=name, elevation=9, boundary=SeriesBoundary(series=Ref(collection="swmm:timeseries", key="T")))
            if name in model.nodes:
                model.nodes.replace(name, value)
            else:
                model.nodes.add(value)
        model.convert_units("CMS")
        self.assertAlmostEqual(model.curves["S"].points[0].x, 2 * .3048)
        self.assertAlmostEqual(model.curves["S"].points[0].y, 20 * .3048 ** 2)
        self.assertAlmostEqual(model.curves["P"].points[0].x, 10 * .02832)
        self.assertAlmostEqual(model.curves["P"].points[0].y, 2 * .02832)
        self.assertAlmostEqual(model.timeseries["T"].points[0].value, 10 * .3048)

    def test_unbound_or_external_series_conversion_fails_atomically(self):
        for external in (False, True):
            model = network()
            series = FileTimeSeries(id="T", file=FileReference(path="a.dat")) if external else InlineTimeSeries(
                id="T", points=(SeriesPoint(time=timedelta(), value=1),))
            model.timeseries.add(series)
            if external:
                model.nodes.update("O", boundary=SeriesBoundary(series=Ref(collection="swmm:timeseries", key="T")))
            before = model.links["P"]
            with self.assertRaises(ValidationError):
                model.convert_units("CMS")
            self.assertEqual(model.links["P"], before)
            self.assertEqual(model.options.flow_units, "CFS")
            self.assertEqual(model.timeseries["T"], series)

    def test_independent_resource_consumer_extends_units_without_core_switch(self):
        @dataclass(frozen=True, kw_only=True)
        class Probe:
            id: str
            series: Ref = reference("swmm:timeseries")

        class ProbeCodec:
            collections = (CollectionSpec(key="test:probes", record_type=Probe, key_of=lambda row: row.id,
                                           identity_field="id", validate=validate_fields),)
            def decode(self, document, profile):
                raise NotImplementedError  # This trial exercises graph/units only.
            def encode(self, store, profile):
                return ()
            def validate(self, store, profile):
                return ()
            def resource_uses(self, store, profile):
                for record in store.collection("test:probes").values():
                    yield ResourceUse(owner=Ref(collection="test:probes", key=record.id), target=record.series,
                                      path=("series",), role="sensor temperature", dimensions=("temperature",))
        schema = default_schema()
        schema.register(FeatureDescriptor(key="test:probes", sections={"PROBES"}), ProbeCodec())
        model = Model(schema=schema)
        model.timeseries.add(InlineTimeSeries(id="T", points=(SeriesPoint(time=timedelta(), value=32),)))
        model.collection("test:probes").add(Probe(id="p", series=Ref(collection="swmm:timeseries", key="T")))
        model.convert_units("CMS")
        self.assertEqual(model.timeseries["T"].points[0].value, 0)
        model.nodes.add(Outfall(id="O", elevation=0, boundary=SeriesBoundary(series=Ref(collection="swmm:timeseries", key="T"))))
        self.assertIn("resource.conflicting_dimensions", [issue.code for issue in model.validate().errors])

    def test_duplicate_transform_registration_does_not_mutate_schema(self):
        from easysewer.io.inp.resources import ResourcesCodec
        schema = default_schema()
        count = len(schema.descriptors)
        with self.assertRaises(RegistryError):
            schema.register(FeatureDescriptor(key="test:duplicate", sections={"CURVES"}), ResourcesCodec())
        self.assertEqual(len(schema.descriptors), count)


if __name__ == "__main__":
    unittest.main()
