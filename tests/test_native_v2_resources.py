"""Native oracles for shared-resource syntax, date anchors and hydraulic use."""

from datetime import date, time, timedelta
from pathlib import Path
import tempfile
import unittest

from easysewer import get_native_capabilities
from easysewer.io.inp import InpDocument
from easysewer.model import Model, Ref
from easysewer.model.geometry import CrossSection, Custom
from easysewer.model.network import SeriesBoundary
from easysewer.model.resources import CURVE_KINDS, Curve, CurvePoint, InlineTimeSeries, SeriesPoint
import test_native_v2_options as oracle
from test_options_v2 import network


@unittest.skipUnless(get_native_capabilities()["swmm_solver"], "Native solver unavailable")
class NativeResourceTests(unittest.TestCase):
    solve = oracle.NativeOptionsTests.solve

    def canonical(self, source):
        model = Model.from_document(InpDocument.from_text(source), strict=True)
        # Adding one explicitly defaulted factor changes the resource encoding
        # without changing physics, and forces all resource blocks through IO.
        model.patterns.update("unused", factors=(1, 1))
        return model.to_document().text

    def test_all_12_curve_types_and_4_pattern_types_roundtrip_native(self):
        source = network().to_document().text + "[CURVES]\n"
        for index, kind in enumerate(CURVE_KINDS):
            source += f"C{index} {kind} 0 0 1 1\nC{index} 2 0\n"
        source += "[PATTERNS]\nunused DAILY 1\nM MONTHLY\nH HOURLY 1 .5\nW WEEKEND 2\n"
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(self.solve(directory, "original", source),
                             self.solve(directory, "canonical", self.canonical(source)))

    def test_monthly_daily_hourly_weekend_patterns_preserve_actual_dwf(self):
        source_patterns = ("[DWF]\nJ FLOW .3 M D H W\n[PATTERNS]\nunused DAILY 1\n"
                           "M MONTHLY .8\nD DAILY .1 .2 .3 .4 .5 .6 .7\n"
                           "H HOURLY " + " ".join(str((i + 1) / 24) for i in range(24)) + "\n"
                           "W WEEKEND " + " ".join([".9"] * 24) + "\n")
        with tempfile.TemporaryDirectory() as directory:
            results = []
            for day in (1, 4):  # Wednesday and Saturday, with distinct hourly/weekend use.
                model = network()
                model.update_options(start_date=date(2020, 1, day), end_date=date(2020, 1, day),
                                     start_time=time(12), end_time=time(12, 10))
                source = model.to_document().text + source_patterns
                baseline = self.solve(directory, f"original_{day}", source)
                self.assertEqual(baseline, self.solve(directory, f"canonical_{day}", self.canonical(source)))
                results.append(baseline["history"])
            self.assertNotEqual(results[0], results[1])

    def test_custom_section_is_created_entirely_from_typed_model(self):
        model = network()
        model.curves.add(Curve(id="Shape", kind="SHAPE", points=(CurvePoint(x=0, y=0),
                         CurvePoint(x=.5, y=1), CurvePoint(x=1, y=0))))
        model.links.update("P", section=CrossSection(geometry=Custom(full_depth=2,
                           curve=Ref(collection="swmm:curves", key="Shape")), barrels=2))
        source = model.to_document().text
        imported = Model.from_document(InpDocument.from_text(source), strict=True)
        imported.curves.rename("Shape", "Renamed")
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(self.solve(directory, "fresh", source),
                             self.solve(directory, "renamed", imported.to_document().text))

    def test_relative_calendar_and_tidal_outfalls_preserve_full_native_history(self):
        model = network()
        model.update_options(start_time=time(12), end_time=time(12, 10))
        base = model.to_document().text
        cases = (
            ("TIMESERIES Stage", "[TIMESERIES]\nStage 0 9.5 .04 9.8\nStage 01/01/2020 12:05 9.6 12:10 9.7\n"),
            ("TIMESERIES Stage", "[TIMESERIES]\nStage 01/01/2020 12:00 9.5 12:02:24 9.8\nStage 12:05 9.6 12:10 9.7\n"),
            ("TIDAL Tide", "[CURVES]\nTide TIDAL 0 9.5 .04 9.8\nTide .08333333333333333 9.6 .16666666666666666 9.7\n"),
        )
        with tempfile.TemporaryDirectory() as directory:
            results = []
            for index, (boundary, resource) in enumerate(cases):
                with self.subTest(boundary=boundary, index=index):
                    source = base.replace("O 9 FREE", f"O 9 {boundary}") + resource + "[PATTERNS]\nunused DAILY 1\n"
                    before = self.solve(directory, f"original_{index}", source)
                    after = self.solve(directory, f"canonical_{index}", self.canonical(source))
                    self.assertEqual(after, before)
                    results.append(after)
            self.assertEqual(results[0], results[1])  # relative prefix is anchored at 12:00, not midnight

    def test_external_file_series_is_preserved_and_rebased_for_native_run(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / "stage data.dat"
            data.write_text("; external stage\n0 9.5\n0:05 9.8\n0:10 9.5\n", encoding="utf-8")
            source = network().to_document().text.replace("O 9 FREE", "O 9 TIMESERIES Stage")
            source += '[TIMESERIES]\nStage FILE "stage data.dat"\n[PATTERNS]\nunused DAILY 1\n'
            baseline = self.solve(directory, "original", source)
            model = Model.from_inp(root / "original.inp", strict=True)
            model.patterns.update("unused", factors=(1, 1))
            destination = root / "moved"
            destination.mkdir()
            model.to_inp(destination / "canonical.inp")
            result = self.solve(destination, "canonical", (destination / "canonical.inp").read_text(encoding="utf-8"))
            self.assertEqual(result, baseline)
            self.assertEqual(data.read_text(encoding="utf-8"), "; external stage\n0 9.5\n0:05 9.8\n0:10 9.5\n")

    def test_shared_stage_series_unit_conversion_preserves_native_quantities(self):
        model = network()
        model.timeseries.add(InlineTimeSeries(id="Stage", points=(SeriesPoint(time=timedelta(), value=9.5),
                             SeriesPoint(time=timedelta(minutes=5), value=9.8),
                             SeriesPoint(time=timedelta(minutes=10), value=9.5))))
        model.nodes.update("O", boundary=SeriesBoundary(series=Ref(collection="swmm:timeseries", key="Stage")))
        before = model.to_document().text
        model.convert_units("CMS")
        with tempfile.TemporaryDirectory() as directory:
            baseline = self.solve(directory, "CFS", before)
            actual = self.solve(directory, "CMS", model.to_document().text)
        for expected, row in zip(baseline["history"], actual["history"]):
            self.assertEqual(row[:3], expected[:3])
            self.assertAlmostEqual(row[3] / .3048, expected[3], delta=1e-9)
            self.assertAlmostEqual(row[4] / .02832, expected[4], delta=1e-9)
        self.assertEqual(len(actual["history"]), len(baseline["history"]))
        for expected, value in zip(baseline["balance"], actual["balance"]):
            self.assertAlmostEqual(value, expected, delta=1e-8)


if __name__ == "__main__":
    unittest.main()
