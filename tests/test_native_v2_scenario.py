"""Native process oracles for persisted 2.0 scenario edits."""

from datetime import timedelta
from pathlib import Path
import tempfile
import unittest

from easysewer import get_native_capabilities
from easysewer.scenario import ScenarioPatch
from test_controls_v2 import controlled
from test_scenario_v2 import fields, portable
import test_native_v2_controls as native_controls


@unittest.skipUnless(get_native_capabilities()["swmm_solver"] and get_native_capabilities()["swmm_output"], "Native solver/output unavailable")
class NativeScenarioTests(unittest.TestCase):
    def test_persisted_rule_step_patch_changes_actual_action_schedule(self):
        with tempfile.TemporaryDirectory() as directory:
            baseline = controlled("RULE R\nIF SIMULATION TIME >= 00:00:07\nTHEN CONDUIT P STATUS = CLOSED\nELSE CONDUIT P STATUS = OPEN\n")
            before = baseline.to_document().to_bytes()
            for seconds, expected in ((None, 10), (0, 10), (1, 7), (20, 20)):
                p = ScenarioPatch(operations=(fields("options", "settings", rule_step=None if seconds is None else timedelta(seconds=seconds)),))
                path = Path(directory) / "scenario.json"; p.to_json(path)
                model = portable(ScenarioPatch.from_json(path).apply(baseline).model)
                result = native_controls.NativeControlTests.solve(self, directory, "scenario", model.to_document().text)
                oracle = baseline.copy(); oracle.update_options(rule_step=None if seconds is None else timedelta(seconds=seconds))
                self.assertEqual(result, native_controls.NativeControlTests.solve(self, directory, "oracle", oracle.to_document().text))
                first = next(row[0] for row in result["history"] if row[2] == 0)
                self.assertAlmostEqual(first, expected, places=7)
            self.assertEqual(baseline.to_document().to_bytes(), before)



if __name__ == "__main__":
    unittest.main()
