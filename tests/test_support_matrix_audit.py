"""Acceptance records must reject failures in either unittest naming format."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('support_matrix_auditor', ROOT/'tools/audit_support_matrix.py')
auditor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(auditor)


class SupportMatrixFailureEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        source = self.root/'tests/test_example.py'
        source.parent.mkdir()
        source.write_text('class ExampleTests:\n    def test_roundtrip(self):\n        assert True\n', encoding='utf-8')
        self.method = 'test_example.ExampleTests.test_roundtrip'
        self.environments = [kind+'-'+label for kind in ('native','pure')
                             for label in ('windows-313','windows-310','linux-312')]
        self.record = dict(selected=[self.method], skipped=[], failures=[], errors=[],
                           test_digests={'tests/test_example.py':auditor.sha(source)})
        self.proof = dict(format='easysewer:variant-gate-evidence', version=1,
            accepted_sections=['TAGS'], package={'source_digests':{}}, scope='Auditor record-format regression fixture',
            reviewed_gates=[dict(section='TAGS',variant='Gage',gate='source_preservation',
                rationale='Verify failure-ID handling',input_selector='fixture',extra_lifecycle=False,
                assertions=[dict(method=self.method,file='tests/test_example.py',line=2,end_line=3,
                    source_sha256=auditor.sha(source),executed_in=self.environments)])])

    def accepted(self, record):
        path = self.root/'execution.json'
        path.write_text(json.dumps(record),encoding='utf-8')
        proof=deepcopy(self.proof)
        proof['records']={env:dict(record_path='execution.json',record_sha256=auditor.sha(path))
                          for env in self.environments}
        path=self.root/'proof.json';path.write_text(json.dumps(proof),encoding='utf-8')
        row=dict(section='TAGS',variant_groups=['Gage'],acceptance='candidate_gates_verified',
                 acceptance_evidence=dict(path='proof.json',sha256=auditor.sha(path)))
        with patch.object(auditor,'ROOT',self.root):
            return auditor.review_evidence(row,['source_preservation'])

    def test_successful_record_is_accepted(self):
        self.assertEqual(self.accepted(self.record)['gates'],1)

    def test_failed_or_errored_bound_method_is_rejected_in_all_unittest_formats(self):
        identities=(self.method,self.method+' (units=\'CMS\')',
                    'test_roundtrip (test_example.ExampleTests)',
                    'test_roundtrip (test_example.ExampleTests) (units=\'CMS\')')
        for field in ('failures','errors'):
            for identity in identities:
                with self.subTest(field=field,identity=identity):
                    record=deepcopy(self.record);record[field]=[[identity,'injected failure']]
                    with self.assertRaises(AssertionError):self.accepted(record)

    def test_unrelated_failed_method_does_not_invalidate_a_successful_bound_method(self):
        for identity in ('test_example.ExampleTests.test_other','test_other (test_example.ExampleTests)'):
            with self.subTest(identity=identity):
                record=deepcopy(self.record);record['failures']=[[identity,'other failure']]
                self.assertEqual(self.accepted(record)['gates'],1)

    def test_historical_proof_is_optional_and_never_current_acceptance(self):
        path=self.root/'docs/qualification/history.json';path.parent.mkdir(parents=True)
        path.write_text(json.dumps(self.proof),encoding='utf-8')
        row=dict(section='TAGS',variant_groups=['Gage'],acceptance='historical_gates_verified',
                 historical_evidence=dict(path='docs/qualification/history.json',sha256=auditor.sha(path)))
        self.assertIsNone(auditor.review_evidence(row,['source_preservation']))
        self.assertIsNone(auditor.review_evidence(row,['source_preservation'],evidence_root=self.root))
        path.write_text('{}',encoding='utf-8')
        with self.assertRaises(AssertionError):
            auditor.review_evidence(row,['source_preservation'],evidence_root=self.root)
        path.unlink()
        self.assertIsNone(auditor.review_evidence(row,['source_preservation']))
        with self.assertRaises(FileNotFoundError):
            auditor.review_evidence(row,['source_preservation'],evidence_root=self.root)

    def test_historical_reference_cannot_escape_archive(self):
        row=dict(acceptance='historical_gates_verified',historical_evidence=dict(
            path='../history.json',sha256='a'*64))
        with self.assertRaises(AssertionError):auditor.review_evidence(row,[])


if __name__=='__main__':unittest.main()
