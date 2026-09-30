"""Native correction claims follow known bytes, never caller assertions."""
from dataclasses import replace
import unittest

from easysewer.runtime import StandardBackend, FlexiblePondingBackend
from easysewer.runtime import native, flexible


class BackendIdentityTests(unittest.TestCase):
    def info(self, backend, digest):
        return replace(backend._unavailable('fixture'), available=True, reason=None,
                       sha256=digest, engine_version=52004,
                       capabilities=('extension:keep', 'easysewer:checkpoint:999',
                           'easysewer:standard-fixes:999', 'easysewer:native-io-fixes:999',
                           'easysewer:report-io:999', 'easysewer:path-io:999', 'easysewer:horton-capacity:999'))

    def test_horton_claim_requires_new_family_bytes_and_changes_numerical_identity(self):
        for cls, module in ((StandardBackend,native),(FlexiblePondingBackend,flexible)):
            backend=cls()
            for system,digest in module._HORTON_HASHES.items():
                new=backend.execution_info(self.info(backend,digest))
                old=backend.execution_info(self.info(backend,module._PATH_HASHES[system]))
                self.assertIn('easysewer:horton-capacity:1',new.capabilities)
                self.assertNotIn('easysewer:horton-capacity:1',old.capabilities)
                self.assertEqual(dict(new.output_semantics)['swmm:modified-horton'],'easysewer:horton-capacity:1')
                self.assertNotIn('swmm:modified-horton',dict(old.output_semantics))
                self.assertNotEqual(new.numerical_policy,old.numerical_policy)
                self.assertEqual(backend.execution_info(new),new)
                self.assertFalse(any(c.endswith(':999') for c in new.capabilities))
            foreign=flexible if cls is StandardBackend else native
            for digest in foreign._HORTON_HASHES.values():
                self.assertFalse(any(c.startswith(native._CORRECTION_PREFIXES) for c in
                    backend.execution_info(self.info(backend,digest)).capabilities))

    def test_known_revisions_reclassify_stale_claims_and_are_idempotent(self):
        for cls, old, new, paths, prefix, revision in (
                (StandardBackend, native._LEGACY_HASHES, native._CHECKPOINT_HASHES, native._PATH_HASHES,
                 'easysewer:standard-fixes:', 13),
                (FlexiblePondingBackend, flexible._LEGACY_HASHES, flexible._CHECKPOINT_HASHES, flexible._PATH_HASHES,
                 'easysewer:native-io-fixes:', 11)):
            for hashes, expected, checkpoint in ((old, revision-1, False), (new, revision, True),
                                                 (paths, revision+1, True)):
                for digest in hashes.values():
                    with self.subTest(backend=cls.__name__, revision=expected, digest=digest):
                        backend=cls(); info=backend.execution_info(self.info(backend,digest))
                        self.assertIn(prefix+str(expected),info.capabilities)
                        self.assertIn('easysewer:report-io:1',info.capabilities)
                        self.assertIn('extension:keep',info.capabilities)
                        self.assertEqual('easysewer:checkpoint:2' in info.capabilities,checkpoint)
                        self.assertEqual('easysewer:path-io:1' in info.capabilities, hashes is paths)
                        self.assertFalse(any(v.endswith(':999') for v in info.capabilities))
                        self.assertEqual(backend.execution_info(info),info)
                        if cls is StandardBackend:
                            self.assertEqual(info.numerical_policy,f'easysewer:standard:5.2.4:{expected}')

    def test_expected_digest_does_not_grant_corrections_to_unknown_bytes(self):
        for cls in (StandardBackend,FlexiblePondingBackend):
            backend=cls(expected_sha256='a'*64)
            info=backend.execution_info(self.info(backend,'a'*64))
            self.assertIn('extension:keep',info.capabilities)
            self.assertFalse(any(c.startswith(native._CORRECTION_PREFIXES) for c in info.capabilities))
            self.assertNotIn('swmm:runoff-replay',dict(info.output_semantics))
            if cls is StandardBackend:self.assertEqual(info.numerical_policy,'user-library:unverified')

    def test_other_family_digest_cannot_borrow_checkpoint_claim(self):
        for cls, foreign in ((StandardBackend,flexible._CHECKPOINT_HASHES),
                             (FlexiblePondingBackend,native._CHECKPOINT_HASHES),
                             (StandardBackend,flexible._PATH_HASHES),
                             (FlexiblePondingBackend,native._PATH_HASHES)):
            for digest in foreign.values():
                backend=cls(expected_sha256=digest)
                info=backend.execution_info(self.info(backend,digest))
                self.assertFalse(any(c.startswith(native._CORRECTION_PREFIXES) for c in info.capabilities))
