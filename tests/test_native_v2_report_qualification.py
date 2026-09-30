"""RPT reliability requirements use observed backend evidence before execution."""

from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from easysewer import get_native_capabilities
from easysewer.io.inp import InpDocument
from easysewer.model import FileReference, Model
from easysewer.runtime import FlexiblePondingBackend, RunConfig, Runner, StandardBackend
from test_native_v2_report_io import REPORT_SOURCE


class ReportQualificationChecks:
    def test_required_report_io_evidence_is_checked_before_publication(self):
        backend = self.backend_type()
        model = Model.from_document(InpDocument.from_text(REPORT_SOURCE.replace(
            'FLOW_UNITS CFS',
            'FLOW_UNITS CFS\nFLOW_ROUTING DYNWAVE\nALLOW_PONDING YES')), strict=True)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = RunConfig(
                backend=backend.key,
                output_directory=FileReference(path=str(root), direction='output'),
                required_capabilities=('easysewer:report-io:1',),
                keep_failed_artifacts=False,
            )
            result = Runner(backends={backend.key: backend}).run(model, config)
            self.assertTrue(result.succeeded, (result.failure, result.diagnostics))
            self.assertTrue(result.native_completed)
            self.assertIn('easysewer:report-io:1', result.backend.capabilities)
            previous = {p.relative_to(root): p.read_bytes() for p in root.rglob('*') if p.is_file()}

            class WithoutReportProof(self.backend_type):
                def execution_info(self, info):
                    known = super().execution_info(info)
                    return replace(known, capabilities=tuple(
                        value for value in known.capabilities if not value.startswith('easysewer:report-io:')))

            unproven = WithoutReportProof()
            failed = Runner(backends={unproven.key: unproven}).run(model, replace(config, overwrite=True))
            self.assertEqual(failed.status, 'rejected')
            self.assertFalse(failed.native_completed)
            self.assertIsNone(failed.retained_directory)
            self.assertTrue(any(d.code == 'run.capabilities' for d in failed.diagnostics.errors))
            self.assertEqual(previous, {p.relative_to(root): p.read_bytes()
                                        for p in root.rglob('*') if p.is_file()})


@unittest.skipUnless(get_native_capabilities()['swmm_solver'], 'Native solver unavailable')
class StandardReportQualificationTests(ReportQualificationChecks, unittest.TestCase):
    backend_type = StandardBackend


@unittest.skipUnless(get_native_capabilities()['flexible_ponding'], 'Custom solver unavailable')
class CustomReportQualificationTests(ReportQualificationChecks, unittest.TestCase):
    backend_type = FlexiblePondingBackend
