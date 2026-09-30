"""Real isolated Runner transactions with detailed report failures."""
from dataclasses import replace
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from easysewer.io.inp import InpDocument
from easysewer.model import Model, FileReference
from easysewer.runtime import FlexiblePondingBackend, RunConfig, Runner, StandardBackend
from test_native_v2_lid_report_io import cycle_fixture

EVIDENCE = []


def digest_tree(folder):
    return {str(p.relative_to(folder)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in folder.rglob('*') if p.is_file()}


class LidReportRunnerTests(unittest.TestCase):
    def test_failure_preserves_published_reports_and_recovery_with_both_retention_policies(self):
        # Two header streams contain 59 writes each; hit 119 is a real step row.
        cases = [(1, 1, 306, 'start'), (1, 60, 306, 'start'), (1, 119, 306, 'step'),
                 (2, 1, 306, 'start'), (2, 3, 306, 'step'), (3, 1, 306, 'close'),
                 (3, 2, 306, 'close'), (4, 1, 306, 'step'), (5, 1, 306, 'step'),
                 (6, 1, 200, 'open'), (6, 4, 200, 'open'),
                 (7, 1, 101, 'open'), (7, 4, 200, 'open'), (8, 2, 200, 'open')]
        with tempfile.TemporaryDirectory() as directory:
            for family, backend_type in (('standard', StandardBackend), ('custom', FlexiblePondingBackend)):
                path = os.environ.get('EASYSEWER_LID_REPORT_FAULT_' + family.upper())
                if not path:
                    raise unittest.SkipTest('LID report fault candidate not selected')
                backend = backend_type(library=path, expected_sha256=hashlib.sha256(Path(path).read_bytes()).hexdigest())
                runner = Runner(backends={backend.key: backend})
                for keep in (False, True):
                    root = Path(directory) / family / str(keep)
                    root.mkdir(parents=True)
                    model = Model.from_document(InpDocument.from_text(cycle_fixture()), strict=True)
                    model.update_options(flow_routing='DYNWAVE', allow_ponding=True)
                    details = []
                    for n, key in enumerate(model.lid_usage):
                        target = root / 'external' / f'detail-{n}.txt'
                        details.append(target)
                        model.lid_usage.update(key, report_file=FileReference(path=str(target), direction='output'))
                    config = RunConfig(backend=backend.key,
                        output_directory=FileReference(path=str(root / 'published'), direction='output'),
                        keep_failed_artifacts=keep)
                    with patch.dict(os.environ, {'ES_LID_IO_FAULT': '0', 'ES_LID_IO_HIT': '0'}):
                        success = runner.run(model, config)
                    self.assertTrue(success.succeeded, (success.failure, success.diagnostics))
                    original_out = (root / 'published/model.out').read_bytes()
                    original_details = [p.read_bytes() for p in details]
                    for kind, hit, expected, stage in cases + [(1, 1, 101, 'start')]:
                        original = digest_tree(root / 'published')
                        detail_before = [p.read_bytes() for p in details]
                        env = {'ES_LID_IO_FAULT': str(kind), 'ES_LID_IO_HIT': str(hit)}
                        primary = expected == 101 and kind == 1
                        if primary:
                            env['ES_LID_IO_PRIMARY'] = '1'
                        with self.subTest(family=family, keep=keep, kind=kind, hit=hit, primary=primary):
                            with patch.dict(os.environ, env):
                                result = runner.run(model, replace(config, overwrite=True))
                            self.assertEqual(result.status, 'failed', result.failure)
                            self.assertFalse(result.native_completed)
                            self.assertIsNotNone(result.failure.native)
                            self.assertEqual(result.failure.native.code, expected, result.failure)
                            native_stage = 'exec_routing' if family == 'custom' and stage == 'step' else stage
                            self.assertEqual(result.failure.native.stage, native_stage, result.failure)
                            self.assertIn(f'lid-fault kind={kind} hit={hit} fired=1', result.failure.stderr)
                            if primary:
                                self.assertIn('prior error 101', result.failure.native.message)
                                self.assertTrue(any(e.stage == 'close' and e.code == 306 for e in result.failure.cleanup))
                            after = digest_tree(root / 'published')
                            self.assertEqual({p: after.get(p) for p in original}, original)
                            # The documented keep policy retains a new workspace
                            # under the output directory; only its files may be new.
                            added = set(after) - set(original)
                            if keep:
                                self.assertIsNotNone(result.retained_directory)
                                prefix = Path(result.retained_directory).relative_to(root / 'published').as_posix() + '/'
                                self.assertTrue(all(p.replace('\\', '/').startswith(prefix) for p in added), added)
                            else:
                                self.assertFalse(added)
                            self.assertEqual([p.read_bytes() for p in details], detail_before)
                            self.assertEqual(result.retained_directory is not None, keep)
                            if keep:
                                retained = Path(result.retained_directory)
                                self.assertTrue(retained.is_dir())
                                self.assertTrue(result.artifacts)
                                self.assertTrue(all(not a.complete for a in result.artifacts))
                            self.assertFalse(list(root.rglob('.easysewer-lock-*')))
                            with patch.dict(os.environ, {'ES_LID_IO_FAULT': '0', 'ES_LID_IO_HIT': '0'}):
                                recovered = runner.run(model, replace(config, overwrite=True))
                            self.assertTrue(recovered.succeeded, (recovered.failure, recovered.diagnostics))
                            self.assertEqual((root / 'published/model.out').read_bytes(), original_out)
                            self.assertEqual([p.read_bytes() for p in details], original_details)
                            EVIDENCE.append(dict(family=family, keep=keep, kind=kind, hit=hit,
                                code=expected, stage=stage, primary=primary, old_artifacts_preserved=True,
                                recovered=True))


if __name__ == '__main__':
    unittest.main()
