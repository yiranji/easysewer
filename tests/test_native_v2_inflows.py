"""Independent native trajectories and formula oracles for FLOW and DWF."""

from dataclasses import replace
from datetime import datetime, timedelta
from itertools import product, permutations
from pathlib import Path
import tempfile
import unittest

from easysewer import get_native_capabilities
from easysewer.io.inp import InpDocument
from easysewer.model import Model
from easysewer.model.inflows import FlowInflow, DryWeatherFlow
from easysewer.model.network import Storage, FunctionalStorage, Divider, OverflowDivider
from easysewer.model.resources import InlineTimeSeries, SeriesPoint, Pattern
from test_inflows_v2 import inflow_model, add_series, rebuild, ref


@unittest.skipUnless(get_native_capabilities()["swmm_solver"] and get_native_capabilities()["swmm_output"], "Native solver/output unavailable")
class NativeInflowTests(unittest.TestCase):
    def solve(self, directory, name, source, *, allow_error=False):
        from easysewer.runtime._solver_api import SWMMSolverAPI
        from easysewer.runtime._output_api import SWMMOutputAPI
        base = Path(directory)/name
        inp, rpt, out = (base.with_suffix(suffix) for suffix in (".inp", ".rpt", ".out"))
        inp.write_text(source+'[REPORT]\nNODES ALL\nLINKS ALL\n', encoding="utf-8")
        solver = SWMMSolverAPI()
        if solver.get_version() != 52004:
            self.skipTest("Inflow oracle requires SWMM 5.2.4")
        started = False
        try:
            error = solver.open(str(inp), str(rpt), str(out))
            if not error:
                error = solver.start(1)
                started = not error
            if error:
                solver.close()  # Flush the native report before inspecting it.
                if allow_error:
                    return error, rpt.read_text(errors="replace")
                self.fail(f"SWMM {error}: {rpt.read_text(errors='replace')}")
            nodes = tuple(solver.get_index(2, name) for name in ("J", "O"))
            link = solver.get_index(3, "P")
            history, previous = [], 0
            for _ in range(30000):
                error, elapsed = solver.step()
                self.assertEqual(error, 0)
                if elapsed == 0:
                    break
                history.append((previous, elapsed*86400, *(solver.get_value(306, node) for node in nodes),
                                solver.get_value(303, nodes[0]), solver.get_value(410, link)))
                previous = elapsed*86400
            else:
                self.fail("Inflow fixture exceeded step bound")
            self.assertGreater(len(history), 40)
            self.assertEqual(solver.end(), 0)
            started = False
            balance = solver.get_mass_bal_err()
        finally:
            if started:
                solver.end()
            solver.close()
        with SWMMOutputAPI() as output:
            output.open(str(out))
            periods = output.get_times(1)
            self.assertGreater(periods, 40)
            lateral = tuple(output.get_node_series(node, 3, 0, periods) for node in nodes)
        return dict(history=history, balance=balance, lateral=lateral)

    def test_all_node_kinds_optional_fields_and_us_si_rebuild_full_history(self):
        tails = ('Q', 'Q FLOW', 'Q FLOW 1', 'Q FLOW 1 0', 'Q FLOW 1 .5 0',
                 'Q FLOW 1 .5 .1 M', '"" FLOW 1 1 .2 M', '"" MASS ignored 1 .2')
        with tempfile.TemporaryDirectory() as directory:
            for kind, units, tail in product(("JUNCTION", "STORAGE", "DIVIDER", "OUTFALL"), ("CFS", "CMS"), tails):
                with self.subTest(kind=kind, units=units, fields=tail):
                    model = inflow_model()
                    model.reinterpret_units(units)
                    if kind == "STORAGE":
                        model.nodes.replace("J", Storage(id="J", elevation=10, max_depth=5, initial_depth=0,
                            shape=FunctionalStorage(coefficient=0, exponent=1, constant=50)))
                    elif kind == "DIVIDER":
                        model.nodes.replace("J", Divider(id="J", elevation=10, max_depth=5,
                            law=OverflowDivider(), diverted_link=ref("links", "P")))
                    node = "O" if kind == "OUTFALL" else "J"
                    source = model.to_document().text + f'[TIMESERIES]\nQ 0 .1 96 .1\n[PATTERNS]\nM MONTHLY 2 3\n[INFLOWS]\n{node} FLOW {tail}\n[DWF]\n{node} FLOW .1 M\n'
                    parsed = Model.from_document(InpDocument.from_text(source), strict=True)
                    expected = self.solve(directory, "original", source)
                    self.assertEqual(self.solve(directory, "rebuilt", rebuild(parsed).to_document().text), expected)
                    self.assertGreater(max(row[3 if node == "O" else 2] for row in expected["history"]), 0)

    def test_series_scale_baseline_and_dwf_weekend_replacement_cross_month(self):
        model = inflow_model()
        model.timeseries.add(InlineTimeSeries(id="Q", points=(SeriesPoint(time=timedelta(), value=.3), SeriesPoint(time=timedelta(hours=96), value=.3))))
        for name, kind, factors in (("M", "MONTHLY", (2, 4)), ("D", "DAILY", (1,2,3,4,5,6,7)),
                                    ("H", "HOURLY", (3,)*24), ("W", "WEEKEND", (4,)*24)):
            model.patterns.add(Pattern(id=name, kind=kind, factors=factors))
        model.inflows.add(FlowInflow(node=ref("nodes", "J"), series=ref("timeseries", "Q"), scale_factor=2, baseline=.1, pattern=ref("patterns", "M")))
        model.dwf.add(DryWeatherFlow(node=ref("nodes", "J"), baseline=.05, patterns=tuple(ref("patterns", p) for p in ("W","H","D","M"))))
        with tempfile.TemporaryDirectory() as directory:
            result = self.solve(directory, "formula", model.to_document().text)
        for row in result["history"]:
            when = datetime(2020,1,30)+timedelta(seconds=row[0])
            if when.minute not in (15, 30, 45):
                continue  # Avoid native floating date truncation at hour boundaries.
            monthly = 2 if when.month == 1 else 4
            daily = (when.weekday()+1)%7+1  # Native DAILY starts on Sunday.
            hourly = 4 if when.weekday() >= 5 else 3
            self.assertAlmostEqual(row[2], .3*2+.1*monthly+.05*monthly*daily*hourly, delta=1e-9)

    def test_all_dwf_pattern_orders_match_and_same_kind_last_wins(self):
        model = inflow_model()
        for name, kind, factors in (("M","MONTHLY",(2,3)), ("D","DAILY",(1,2,3,4,5,6,7)),
                                    ("H","HOURLY",(.5,)*24), ("W","WEEKEND",(.8,)*24), ("H2","HOURLY",(.7,)*24)):
            model.patterns.add(Pattern(id=name, kind=kind, factors=factors))
        model.dwf.add(DryWeatherFlow(node=ref("nodes", "J"), baseline=.1, patterns=tuple(ref("patterns", p) for p in ("M","D","H","W"))))
        with tempfile.TemporaryDirectory() as directory:
            expected = self.solve(directory, "reference", model.to_document().text)
            for order in permutations(("M","D","H","W")):
                model.dwf.update(("J", "FLOW"), patterns=tuple(ref("patterns", p) for p in order))
                self.assertEqual(self.solve(directory, "permuted", model.to_document().text), expected)
            model.dwf.update(("J", "FLOW"), patterns=(ref("patterns", "H"), None, ref("patterns", "H2")))
            repeated = self.solve(directory, "repeated", model.to_document().text)
            model.dwf.update(("J", "FLOW"), patterns=(ref("patterns", "H2"),))
            self.assertEqual(self.solve(directory, "final_kind", model.to_document().text), repeated)
            self.assertTrue(all(abs(row[2]-.07)<1e-12 for row in repeated["history"]))

    def test_external_baseline_accepts_each_pattern_kind_and_weekend_only(self):
        with tempfile.TemporaryDirectory() as directory:
            for kind in ("MONTHLY", "DAILY", "HOURLY", "WEEKEND"):
                model = inflow_model()
                model.patterns.add(Pattern(id="P", kind=kind, factors=(2,)*24 if kind in ("HOURLY", "WEEKEND") else (2,)*(12 if kind=="MONTHLY" else 7)))
                model.inflows.add(FlowInflow(node=ref("nodes", "J"), baseline=.1, pattern=ref("patterns", "P")))
                actual = self.solve(directory, kind, model.to_document().text)
                for row in actual["history"]:
                    when = datetime(2020,1,30)+timedelta(seconds=row[0])
                    if when.hour != 12:
                        continue
                    expected = .1 if kind == "WEEKEND" and when.weekday()<5 else .2
                    self.assertAlmostEqual(row[2], expected, delta=1e-12)

    def test_negative_series_scaling_baselines_and_dwf_are_not_clipped(self):
        with tempfile.TemporaryDirectory() as directory:
            for negative in ("series", "scale", "baseline", "dwf"):
                model = inflow_model()
                model.timeseries.add(InlineTimeSeries(id="Q", points=(SeriesPoint(time=timedelta(), value=-.1 if negative=="series" else .1),
                    SeriesPoint(time=timedelta(hours=96), value=-.1 if negative=="series" else .1))))
                model.inflows.add(FlowInflow(node=ref("nodes", "J"), series=ref("timeseries", "Q"),
                    scale_factor=-1 if negative=="scale" else 1, baseline=-.5 if negative=="baseline" else .5))
                model.dwf.add(DryWeatherFlow(node=ref("nodes", "J"), baseline=-.2 if negative=="dwf" else .2))
                self.assertTrue(model.validate(for_run=True).is_valid)
                result = self.solve(directory, negative, model.to_document().text)
                expected = {"series":.6, "scale":.6, "baseline":-.2, "dwf":.4}[negative]
                self.assertTrue(all(abs(row[2]-expected)<1e-12 for row in result["history"]))

    def test_series_interpolation_and_zero_outside_record_range(self):
        model = inflow_model()
        model.timeseries.add(InlineTimeSeries(id="Q", points=(SeriesPoint(time=timedelta(hours=24), value=.2),
            SeriesPoint(time=timedelta(hours=48), value=.4))))
        model.inflows.add(FlowInflow(node=ref("nodes", "J"), series=ref("timeseries", "Q"), scale_factor=2, baseline=.1))
        with tempfile.TemporaryDirectory() as directory:
            expected = self.solve(directory, "interpolate", model.to_document().text)
            self.assertEqual(self.solve(directory, "rebuilt", rebuild(Model.from_document(model.to_document())).to_document().text), expected)
        for row in expected["history"]:
            hour = row[0]/3600
            if 24.01 < hour < 47.99:
                self.assertAlmostEqual(row[2], .1+2*(.2+(hour-24)/24*.2), delta=1e-8)
            elif hour < 23.9 or hour > 48.1:
                self.assertAlmostEqual(row[2], .1, delta=1e-12)

    def test_shared_flow_series_and_baselines_all_six_units_native(self):
        model = inflow_model()
        q = add_series(model)
        for node in ("J", "O"):
            model.inflows.add(FlowInflow(node=ref("nodes", node), series=q, scale_factor=.5, baseline=.1))
            model.dwf.add(DryWeatherFlow(node=ref("nodes", node), baseline=.05))
        with tempfile.TemporaryDirectory() as directory:
            expected = self.solve(directory, "CFS", model.to_document().text)
            for unit, factor in (("GPM",448.831),("MGD",.64632),("CMS",.02832),("LPS",28.317),("MLD",2.4466)):
                converted = model.copy()
                converted.convert_units(unit)
                actual = self.solve(directory, unit, converted.to_document().text)
                self.assertEqual(len(actual["history"]), len(expected["history"]))
                for before, after in zip(expected["history"], actual["history"]):
                    self.assertEqual(before[:2], after[:2])
                    for index in (2,3,5):
                        self.assertAlmostEqual(after[index]/factor, before[index], delta=1e-9)
                    self.assertAlmostEqual(after[4]/(1 if unit in ("GPM","MGD") else .3048), before[4], delta=1e-9)
                for expected_node, actual_node in zip(expected["lateral"], actual["lateral"]):
                    for before, after in zip(expected_node, actual_node):
                        self.assertAlmostEqual(after/factor, before, delta=1e-7)

    def test_external_series_file_relocation_matches_inline_and_source_rebuild(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/"saved").mkdir()
            data = root/"flow data.dat"
            data.write_text("01/30/2020 00:00 .1\n01/31/2020 00:00 .3\n02/01/2020 00:00 .2\n02/03/2020 00:00 .1\n", encoding="ascii")
            model = inflow_model()
            q = add_series(model)
            model.inflows.add(FlowInflow(node=ref("nodes", "J"), series=q, scale_factor=.5, baseline=.1))
            expected = self.solve(directory, "inline", model.to_document().text)
            source = root/"file.inp"
            text = model.to_document().text
            doc = InpDocument.from_text(text)
            for line in doc.records("TIMESERIES"):
                text = text.replace(line.content+'\n', '')
            text += '[TIMESERIES]\nQ FILE "flow data.dat"\n'
            source.write_text(text, encoding="utf-8")
            parsed = Model.from_inp(source, strict=True)
            parsed.to_inp(root/"saved"/"moved.inp")
            moved = Model.from_inp(root/"saved"/"moved.inp", strict=True)
            # Write/run in the corresponding directory: relative paths are INP-relative.
            self.assertEqual(self.solve(root/"saved", "moved_run", moved.to_document().text), expected)
            self.assertEqual(self.solve(directory, "file_rebuilt", rebuild(parsed).to_document().text), expected)

    def test_overridden_missing_references_error_until_explicit_normalization(self):
        with tempfile.TemporaryDirectory() as directory:
            for section, rows in (("INFLOWS", 'J FLOW Missing\nJ FLOW "" FLOW 1 1 .2'),
                                  ("DWF", 'J FLOW .1 Missing\nJ FLOW .2')):
                source = inflow_model().to_document().text+f"[{section}]\n{rows}\n"
                parsed = Model.from_document(InpDocument.from_text(source), strict=True)
                error, report = self.solve(directory, "bad_reference", source, allow_error=True)
                self.assertNotEqual(error, 0)
                self.assertIn("ERROR 209", report)
                normalized = self.solve(directory, "normalized", parsed.to_document(normalize=True).text)
                self.assertTrue(all(abs(row[2]-.2)<1e-12 for row in normalized["history"]))

    def test_duplicate_assignments_last_wins_and_node_series_pattern_renames(self):
        model = inflow_model()
        add_series(model)
        model.patterns.add(Pattern(id="M", kind="MONTHLY", factors=(2,3)))
        source = model.to_document().text + '[INFLOWS]\nJ FLOW Q FLOW 1 1 .1 M\nJ FLOW Q FLOW 1 2 .2 M\n[DWF]\nJ FLOW .1 M\nJ FLOW .2 M\n'
        parsed = Model.from_document(InpDocument.from_text(source), strict=True)
        with tempfile.TemporaryDirectory() as directory:
            expected = self.solve(directory, "duplicates", source)
            self.assertEqual(self.solve(directory, "normalized", parsed.to_document(normalize=True).text), expected)
            parsed.timeseries.rename("Q", "Hydrograph")
            parsed.patterns.rename("M", "Monthly")
            # Rename the node away and back to exercise relation rekeying while
            # retaining the helper's fixed result lookup IDs.
            parsed.nodes.rename("J", "Junction")
            parsed.nodes.rename("Junction", "J")
            self.assertEqual(self.solve(directory, "renamed", parsed.to_document().text), expected)

    def test_overridden_inflow_retains_native_rainfall_series_usage_flag(self):
        from test_hydrology_v2 import hydrology_model
        model = hydrology_model()
        source = model.to_document().text + '[INFLOWS]\nJ FLOW Rain\nJ FLOW "" FLOW 1 1 .2\n'
        parsed = Model.from_document(InpDocument.from_text(source), strict=True)
        self.assertIsNone(parsed.inflows[("J", "FLOW")].series)
        self.assertIn("inflow.native_rainfall_series", {d.code for d in parsed.validate(for_run=True).errors})
        self.assertTrue(parsed.validate(for_run=True, normalize=True).is_valid)
        with tempfile.TemporaryDirectory() as directory:
            error, report = self.solve(directory, "rainfall_conflict", source, allow_error=True)
            self.assertNotEqual(error, 0)
            self.assertIn("time series", report.lower())
            self.solve(directory, "rainfall_fixed", parsed.to_document(normalize=True).text)


if __name__ == "__main__":
    unittest.main()
