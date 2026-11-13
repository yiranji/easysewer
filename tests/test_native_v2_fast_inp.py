"""Fast parameter IO keeps native execution and captured input context intact."""

from datetime import timedelta
import hashlib
from pathlib import Path
import tempfile
import unittest

from easysewer import get_native_capabilities
from easysewer.model import FileReference, Model, Ref
from easysewer.model.network import SeriesBoundary
from easysewer.model.report import ReportSelection
from easysewer.model.resources import FileTimeSeries
from easysewer.runtime import ReportReadOptions, Runner
from test_options_v2 import network
from test_runner_v2 import config


@unittest.skipUnless(get_native_capabilities()["swmm_solver"] and
                     get_native_capabilities()["swmm_output"], "Native solver/output unavailable")
class NativeFastInputTests(unittest.TestCase):
    def test_fast_export_runner_and_snapshot_match_checked_native_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / "stage data.dat"
            data.write_text("0 9.5\n0:05 9.8\n0:10 9.5\n", encoding="utf-8")
            data_bytes = data.read_bytes()
            model = network()
            model.update_options(threads=4, report_step=timedelta(seconds=30))
            model.update_report(nodes=ReportSelection(mode="ALL"), links=ReportSelection(mode="ALL"))
            model.timeseries.add(FileTimeSeries(id="Stage", file=FileReference(
                path=data.name, base_directory=str(root))))
            model.nodes.update("O", boundary=SeriesBoundary(series=Ref(
                collection="swmm:timeseries", key="Stage")))
            source = root / "original.inp"
            source.write_text(model.to_document().text, encoding="utf-8")
            source_bytes = source.read_bytes()

            fast = Model.from_inp(source)
            emitted = root / "exported.inp"
            fast.to_inp(emitted)
            fast = Model.from_inp(emitted)
            checked = Model.from_inp(source, strict=True)
            self.assertIsNone(fast.document)
            self.assertEqual(tuple(fast.nodes.values()), tuple(checked.nodes.values()))
            self.assertEqual(tuple(fast.links.values()), tuple(checked.links.values()))
            self.assertEqual(tuple(fast.timeseries.values()), tuple(checked.timeseries.values()))

            runner = Runner()
            results = [runner.run(value, config(root / label, backend="swmm:standard",
                report_read=ReportReadOptions(encoding="cp1252"), keep_failed_artifacts=False))
                for label, value in (("checked", checked), ("fast", fast))]
            for result in results:
                self.assertTrue(result.succeeded, repr(result.failure) + repr(result.diagnostics.errors))
                self.assertTrue(result.native_completed)
                self.assertEqual(result.backend.key, "swmm:standard")
                self.assertEqual(result.snapshot.input_bytes, result.input.read_bytes())
                self.assertEqual(len(result.snapshot.resources), 1)
                resource = result.snapshot.resources[0]
                self.assertEqual(resource.original_path, str(data.resolve()))
                self.assertEqual(resource.sha256, hashlib.sha256(data_bytes).hexdigest())
            baseline, actual = results
            self.assertEqual(actual.output.read_bytes(), baseline.output.read_bytes())
            self.assertEqual(actual.engine_objects, baseline.engine_objects)
            # Result contexts deliberately bind different model snapshots; the
            # native values, including all recorded output periods, must match.
            for field in ("runoff_percent", "flow_percent", "quality_percent", "raw_percentages"):
                self.assertEqual(getattr(actual.mass_balance, field), getattr(baseline.mass_balance, field))

            restored = actual.snapshot.model()
            self.assertIsNone(restored.document)
            self.assertEqual(restored.source_path, str(emitted.resolve()))
            self.assertEqual(restored._input_context, fast._input_context)
            self.assertEqual(restored.to_json_document().to_bytes(), fast.to_json_document().to_bytes())
            self.assertEqual(actual.artifact("run:model").read_bytes(), actual.snapshot.model_json)
            self.assertEqual(source.read_bytes(), source_bytes)
            self.assertEqual(data.read_bytes(), data_bytes)


if __name__ == "__main__":
    unittest.main()
