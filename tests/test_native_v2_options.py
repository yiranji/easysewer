"""Native behavioral oracles for rule scheduling and analysis-option profiles."""

from datetime import date, time, timedelta
from pathlib import Path
import tempfile
import unittest

from easysewer import get_native_capabilities
from easysewer.io.inp import InpDocument
from easysewer.model import Model
from test_native_v2_network import NETWORK, SETTINGS
from test_options_v2 import network


CONTROL = """[CONTROLS]
RULE close_pipe
IF SIMULATION TIME >= 00:00:07
THEN CONDUIT P STATUS = CLOSED
ELSE CONDUIT P STATUS = OPEN
"""


@unittest.skipUnless(get_native_capabilities()["swmm_solver"], "Native solver unavailable")
class NativeOptionsTests(unittest.TestCase):
    def solve(self, directory, name, source):
        from easysewer.runtime._solver_api import SWMMSolverAPI
        base = Path(directory) / name
        inp, rpt, out = (base.with_suffix(suffix) for suffix in (".inp", ".rpt", ".out"))
        inp.write_text(source, encoding="utf-8")
        solver = SWMMSolverAPI()
        if solver.get_version() != 52004:
            self.skipTest("Profile fixtures require SWMM 5.2.4")
        started = False
        try:
            error = solver.open(str(inp), str(rpt), str(out))
            if error:
                solver.close()
                self.fail(f"SWMM open {error}: {rpt.read_text(errors='replace')}")
            routing, report = solver.get_value(4, 0), solver.get_value(5, 0)
            self.assertEqual(solver.start(1), 0)
            started = True
            link, node = solver.get_index(3, "P"), solver.get_index(2, "J")
            previous = 0
            history = []
            for _ in range(10000):
                error, elapsed = solver.step()
                self.assertEqual(error, 0)
                if elapsed == 0:
                    break
                history.append((previous, elapsed * 86400, solver.get_value(407, link),
                                solver.get_value(303, node), solver.get_value(410, link)))
                previous = elapsed * 86400
            else:
                self.fail("Run exceeded fixture step bound")
            self.assertGreater(len(history), 1)
            self.assertEqual(solver.end(), 0)
            started = False
            return {"routing": routing, "report": report, "history": history,
                    "balance": solver.get_mass_bal_err()}
        finally:
            if started:
                solver.end()
            solver.close()

    def test_rule_step_roundtrip_preserves_action_times_and_full_history(self):
        cases = ((None, 10), ("0", 10), ("00:00:20", 20), ("0.005555555555555556", 20),
                 ("0.0004", 7), ("00:00:01.9", 7), ("25:00:00", None))
        with tempfile.TemporaryDirectory() as directory:
            for index, (token, expected_action) in enumerate(cases):
                with self.subTest(rule_step=token):
                    statement = "" if token is None else f"RULE_STEP {token}\n"
                    source = NETWORK.format(shape="CIRCULAR 2 0 0 0") + SETTINGS.replace(
                        "[OPTIONS]\n", "[OPTIONS]\n" + statement) + CONTROL
                    model = Model.from_document(InpDocument.from_text(source), strict=True)
                    model.update_options(threads=1)  # Forces the typed option writer; native default is already 1.
                    exported = model.to_document().text
                    baseline = self.solve(directory, f"original_{index}", source)
                    result = self.solve(directory, f"exported_{index}", exported)
                    self.assertEqual(result, baseline)
                    actions = [row[0] for row in result["history"] if row[2] == 0]
                    if expected_action is None:
                        self.assertEqual(actions, [])
                    else:
                        self.assertAlmostEqual(actions[0], expected_action, places=7)

    def test_subsecond_routing_step_survives_typed_writer(self):
        source = NETWORK.format(shape="CIRCULAR 2 0 0 0") + SETTINGS.replace("ROUTING_STEP 5", "ROUTING_STEP .25")
        model = Model.from_document(InpDocument.from_text(source), strict=True)
        model.update_options(threads=1)
        with tempfile.TemporaryDirectory() as directory:
            original = self.solve(directory, "original", source)
            exported = self.solve(directory, "exported", model.to_document().text)
            self.assertEqual(exported, original)
            self.assertEqual(exported["routing"], .25)

    def test_effective_report_and_routing_steps_match_native_clamps(self):
        model = network()
        model.update_options(end_time=time(0, 2), wet_step=timedelta(seconds=10),
                             routing_step=timedelta(seconds=20), minimum_step=timedelta(seconds=30))
        with tempfile.TemporaryDirectory() as directory:
            actual = self.solve(directory, "profile", model.to_document().text)
        self.assertEqual(actual["routing"], model.effective_options.values.routing_step.total_seconds())
        self.assertEqual(actual["report"], model.effective_options.values.report_step.total_seconds())

    def test_offset_conversion_keeps_complete_native_history(self):
        model = network()
        baseline = model.to_document().text
        model.convert_link_offsets("ELEVATION")
        exported = model.to_document().text
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(self.solve(directory, "depth", baseline), self.solve(directory, "elevation", exported))

    def test_all_six_flow_units_preserve_native_internal_hydrograph(self):
        # Independent native oracle: Qcf values belong to the pinned solver,
        # rather than using the conversion routine under test to normalize.
        factors = {"CFS": 1, "GPM": 448.831, "MGD": .64632,
                   "CMS": .02832, "LPS": 28.317, "MLD": 2.4466}
        baseline_model = network()
        with tempfile.TemporaryDirectory() as directory:
            baseline = self.solve(directory, "CFS", baseline_model.to_document().text)
            for unit, flow_factor in factors.items():
                with self.subTest(unit=unit):
                    model = baseline_model.copy()
                    model.convert_units(unit)
                    actual = self.solve(directory, unit, model.to_document().text)
                    self.assertEqual(len(actual["history"]), len(baseline["history"]))
                    length_factor = 1 if unit in ("CFS", "GPM", "MGD") else .3048
                    for expected, row in zip(baseline["history"], actual["history"]):
                        self.assertEqual(row[:3], expected[:3])
                        self.assertAlmostEqual(row[3] / length_factor, expected[3], delta=1e-10)
                        self.assertAlmostEqual(row[4] / flow_factor, expected[4], delta=1e-10)
                    for expected, observed in zip(baseline["balance"], actual["balance"]):
                        self.assertAlmostEqual(observed, expected, delta=1e-8)


if __name__ == "__main__":
    unittest.main()
