"""Compare typed geometry/model output against the bundled SWMM 5.2.4 solver."""

from pathlib import Path
import tempfile
import unittest

from easysewer import get_native_capabilities
from easysewer.io.inp import InpDocument
from easysewer.io.inp.geometry import CrossSectionCodec
from easysewer.model import Model


CASES = {
    "CIRCULAR": "1 0 0 0", "FORCE_MAIN": "1 120 0 0", "FILLED_CIRCULAR": "2 .1 0 0",
    "RECT_CLOSED": "2 3 0 0", "RECT_OPEN": "2 3 1 0", "TRAPEZOIDAL": "2 3 .5 1.5",
    "TRIANGULAR": "2 4 0 0", "HORIZ_ELLIPSE": "2 3 0 0", "VERT_ELLIPSE": "3 2 0 0",
    "ARCH": "2 3 0 0", "PARABOLIC": "2 4 0 0", "POWER": "2 3 .5 0",
    "RECT_TRIANGULAR": "2 3 .5 0", "RECT_ROUND": "2 3 0 0", "MODBASKETHANDLE": "2 3 0 0",
    "EGG": "2 0 0 0", "HORSESHOE": "2 0 0 0", "GOTHIC": "2 0 0 0", "CATENARY": "2 0 0 0",
    "SEMIELLIPTICAL": "2 0 0 0", "BASKETHANDLE": "2 0 0 0", "SEMICIRCULAR": "2 0 0 0",
    "CUSTOM": "2 Shape1 0 0", "IRREGULAR": "Transect1", "STREET": "Street1", "DUMMY": "0 0 0 0",
}
SETTINGS = """[OPTIONS]
FLOW_UNITS CMS
FLOW_ROUTING DYNWAVE
START_DATE 01/01/2020
START_TIME 00:00:00
END_DATE 01/01/2020
END_TIME 00:10:00
REPORT_STEP 00:01:00
ROUTING_STEP 5
VARIABLE_STEP 0
[INFLOWS]
J FLOW Q FLOW 1 1
[TIMESERIES]
Q 0:00 0.3
Q 1:00 0.3
"""
NETWORK = """[JUNCTIONS]
J 2 5
[OUTFALLS]
O 1 FREE NO
[CONDUITS]
P J O 123.123456789 .013 0 0 0 0
[LOSSES]
P .1 .2 .3 YES .0001
[XSECTIONS]
P {shape}
"""
RESOURCES = {
    "CUSTOM": "[CURVES]\nShape1 SHAPE 0 0\nShape1 .5 1\nShape1 1 0\n",
    # 5.2.4 finalizes the last transect when the following section begins.
    "IRREGULAR": "[TRANSECTS]\nNC .03 .03 .02\nX1 Transect1 3 0 10 0 0 0 0 0 0\nGR 2 0 0 5 2 10\n[REPORT]\n",
    "STREET": "[STREETS]\nStreet1 5 .15 2 .016\n",
}


@unittest.skipUnless(get_native_capabilities()["swmm_solver"], "SWMM native library unavailable")
class NativeNetworkTests(unittest.TestCase):
    def solve(self, directory, name, text):
        from easysewer.runtime._solver_api import SWMMSolverAPI
        base = Path(directory) / name
        inp, rpt, out = (base.with_suffix(suffix) for suffix in (".inp", ".rpt", ".out"))
        inp.write_text(text, encoding="utf-8")
        solver = SWMMSolverAPI()
        if solver.get_version() != 52004:
            self.skipTest("These semantic fixtures target native SWMM 5.2.4")
        started = False
        try:
            error = solver.open(str(inp), str(rpt), str(out))
            if error:
                solver.close()  # Flush the native report before inspecting it.
                self.fail(f"SWMM open failed ({error}): " + rpt.read_text(errors="replace"))
            self.assertEqual(solver.start(1), 0)
            started = True
            node, link = solver.get_index(2, "J"), solver.get_index(3, "P")
            self.assertGreaterEqual(node, 0)
            self.assertGreaterEqual(link, 0)
            full_depth = solver.get_value(405, link)
            history = []
            for _ in range(10000):
                error, elapsed = solver.step()
                self.assertEqual(error, 0)
                if elapsed == 0:
                    break
                history.append((elapsed, solver.get_value(303, node), solver.get_value(410, link)))
            else:
                self.fail("Solver failed to finish within the fixture step bound")
            self.assertGreater(len(history), 10)
            self.assertGreater(max(row[2] for row in history), 0)
            self.assertEqual(solver.end(), 0)
            started = False
            balance = solver.get_mass_bal_err()
            return full_depth, history, balance
        finally:
            if started:
                solver.end()
            solver.close()

    def test_26_geometry_variants_preserve_native_hydraulics(self):
        codec = CrossSectionCodec()
        with tempfile.TemporaryDirectory() as directory:
            for kind, parameters in CASES.items():
                with self.subTest(shape=kind):
                    shape = (kind, *parameters.split())
                    source = NETWORK.format(shape=" ".join(shape))
                    if kind in RESOURCES:
                        # Shared-resource domain codecs are a later increment;
                        # this verifies the cross-section syntax against native.
                        rewritten = NETWORK.format(shape=" ".join(codec.format(codec.parse(shape))))
                    else:
                        parsed = Model.from_document(InpDocument.from_text(source), strict=True)
                        rebuilt = Model()
                        for node in parsed.nodes.values():
                            rebuilt.nodes.add(node)
                        for link in parsed.links.values():
                            rebuilt.links.add(link)
                        rewritten = rebuilt.to_document().text
                    extra = SETTINGS + RESOURCES.get(kind, "")
                    original = self.solve(directory, kind + "_original", source + extra)
                    exported = self.solve(directory, kind + "_exported", rewritten + extra)
                    self.assertEqual(exported, original)

    def test_standard_size_codes_preserve_native_hydraulics(self):
        codec = CrossSectionCodec()
        with tempfile.TemporaryDirectory() as directory:
            for kind in ("HORIZ_ELLIPSE", "VERT_ELLIPSE", "ARCH"):
                with self.subTest(shape=kind):
                    source = NETWORK.format(shape=f"{kind} 99 99 3 0") + SETTINGS
                    formatted = codec.format(codec.parse((kind, "99", "99", "3", "0")))
                    exported = NETWORK.format(shape=" ".join(formatted)) + SETTINGS
                    self.assertEqual(self.solve(directory, kind + "_original", source),
                                     self.solve(directory, kind + "_exported", exported))


if __name__ == "__main__":
    unittest.main()
