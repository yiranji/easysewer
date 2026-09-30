"""Known new bytes acquire corrections while historical and foreign bytes keep their identities."""
from dataclasses import replace
import unittest
from easysewer.runtime import StandardBackend,FlexiblePondingBackend,native,flexible


class HortonOutfallIdentityTests(unittest.TestCase):
    def info(self,backend,digest):
        return replace(backend._unavailable('identity fixture'),available=True,reason=None,
            sha256=digest,engine_version=52004,numerical_policy='stale',
            capabilities=('extension:keep','easysewer:horton-state:999','easysewer:outfall-gate:999'),
            output_semantics=(('swmm:modified-horton','stale'),('swmm:outfall-gate','stale')))

    def test_old_and_new_revisions_keep_exact_family_policy_and_semantics(self):
        for cls,module,old_revision,new_revision in ((StandardBackend,native,15,16),(FlexiblePondingBackend,flexible,13,14)):
            backend=cls()
            for system,digest in module._HORTON_OUTFALL_HASHES.items():
                with self.subTest(backend=cls.__name__,system=system):
                    new=backend.execution_info(self.info(backend,digest))
                    old=backend.execution_info(self.info(backend,module._HORTON_HASHES[system]))
                    prefix='easysewer:standard:5.2.4:' if cls is StandardBackend else flexible.POLICY+':native:'
                    self.assertEqual(new.numerical_policy,prefix+str(new_revision))
                    self.assertEqual(old.numerical_policy,prefix+str(old_revision))
                    for item in (old,new):
                        self.assertEqual(backend.execution_info(item),item)
                        self.assertIn('extension:keep',item.capabilities)
                        self.assertIn('easysewer:horton-capacity:1',item.capabilities)
                        self.assertFalse(any(c.endswith(':999') for c in item.capabilities))
                    self.assertIn('easysewer:horton-state:1',new.capabilities)
                    self.assertIn('easysewer:outfall-gate:1',new.capabilities)
                    self.assertNotIn('easysewer:horton-state:1',old.capabilities)
                    self.assertNotIn('easysewer:outfall-gate:1',old.capabilities)
                    self.assertEqual(dict(old.output_semantics)['swmm:modified-horton'],'easysewer:horton-capacity:1')
                    self.assertNotIn('swmm:outfall-gate',dict(old.output_semantics))
                    self.assertEqual(dict(new.output_semantics)['swmm:modified-horton'],'easysewer:horton-state:1')
                    self.assertEqual(dict(new.output_semantics)['swmm:outfall-gate'],'easysewer:outfall-gate:1')

    def test_foreign_and_unknown_bytes_cannot_acquire_either_correction(self):
        for cls,foreign in ((StandardBackend,flexible),(FlexiblePondingBackend,native)):
            for digest in (*foreign._HORTON_OUTFALL_HASHES.values(),'f'*64):
                backend=cls(expected_sha256=digest)
                item=backend.execution_info(self.info(backend,digest))
                self.assertFalse(any(c.startswith(native._CORRECTION_PREFIXES) for c in item.capabilities))
                self.assertNotIn('swmm:modified-horton',dict(item.output_semantics))
                self.assertNotIn('swmm:outfall-gate',dict(item.output_semantics))
                self.assertIn('extension:keep',item.capabilities)
