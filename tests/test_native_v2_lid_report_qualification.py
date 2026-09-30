"""Installed LID report capability must follow qualified bytes, not symbols."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from easysewer import get_native_capabilities
from easysewer.io.inp import InpDocument
from easysewer.model import FileReference, Model
from easysewer.runtime import FlexiblePondingBackend, RunConfig, Runner, StandardBackend
from test_native_v2_lid_report_io import fixture
from test_native_v2_standard_io import direct_library

CAPABILITY = 'easysewer:lid-report-io:1'


class LidReportQualificationChecks:
    def test_build_marker_record_and_known_hash_agree(self):
        backend = self.backend_type()
        path, expected = backend._selection()
        lib, path = direct_library(path, revision_symbol=self.revision_symbol)
        self.assertEqual(lib.swmm_getEasySewerLidReportIO(), 1)
        record = json.loads(path.with_name(self.stem + '.build.json').read_bytes())
        self.assertEqual(record['lid_report_io'], 1)
        self.assertEqual(record['source']['lid_report_io'], 1)
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), expected)
        self.assertEqual(record['sha256'], expected)
        info = backend.probe()
        self.assertTrue(info.available, info.reason)
        self.assertIn(CAPABILITY, info.capabilities)
        unproven = backend.execution_info(replace(info, sha256='0' * 64))
        self.assertNotIn(CAPABILITY, unproven.capabilities)

    def test_required_evidence_rejects_before_changing_any_published_report(self):
        backend = self.backend_type()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            detail = root / 'external detail.txt'
            model = Model.from_document(InpDocument.from_text(fixture(detail=str(detail))), strict=True)
            model.update_options(flow_routing='DYNWAVE', allow_ponding=True)
            config = RunConfig(backend=backend.key,
                output_directory=FileReference(path=str(root / 'published'), direction='output'),
                required_capabilities=(CAPABILITY,), keep_failed_artifacts=False)
            result = Runner(backends={backend.key: backend}).run(model, config)
            self.assertTrue(result.succeeded, (result.failure, result.diagnostics))
            self.assertIn(CAPABILITY, result.backend.capabilities)
            previous = {p.relative_to(root): p.read_bytes() for p in root.rglob('*') if p.is_file()}

            class WithoutProof(self.backend_type):
                def execution_info(self, info):
                    known = super().execution_info(info)
                    return replace(known, capabilities=tuple(c for c in known.capabilities if c != CAPABILITY))

            unproven = WithoutProof()
            rejected = Runner(backends={unproven.key: unproven}).run(model, replace(config, overwrite=True))
            self.assertEqual(rejected.status, 'rejected')
            self.assertFalse(rejected.native_completed)
            self.assertIsNone(rejected.retained_directory)
            self.assertTrue(any(d.code == 'run.capabilities' for d in rejected.diagnostics.errors))
            self.assertEqual(previous, {p.relative_to(root): p.read_bytes() for p in root.rglob('*') if p.is_file()})


@unittest.skipUnless(get_native_capabilities()['swmm_solver'], 'Native solver unavailable')
class StandardLidReportQualificationTests(LidReportQualificationChecks, unittest.TestCase):
    backend_type = StandardBackend
    revision_symbol = 'swmm_getEasySewerStandardFixes'
    stem = 'swmm5'


@unittest.skipUnless(get_native_capabilities()['flexible_ponding'], 'Custom solver unavailable')
class CustomLidReportQualificationTests(LidReportQualificationChecks, unittest.TestCase):
    backend_type = FlexiblePondingBackend
    revision_symbol = 'swmm_getEasySewerNativeIOFixes'
    stem = 'flexible_ponding'
