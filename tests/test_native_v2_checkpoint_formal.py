"""Formal checkpoint artifacts must work through ordinary registered backends."""
import ctypes
import hashlib
import os
from pathlib import Path
import unittest

from easysewer.runtime import StandardBackend, FlexiblePondingBackend
from test_native_v2_runner_checkpoint import backend

EVIDENCE=[]


@unittest.skipUnless(os.environ.get('EASYSEWER_CHECKPOINT_STANDARD') and
                     os.environ.get('EASYSEWER_CHECKPOINT_CUSTOM'),'Requires formal ABI2 libraries')
class FormalCheckpointTests(unittest.TestCase):
    def test_actual_library_identity_and_ordinary_backend(self):
        for family, cls, symbol, revision, capability in (
                ('standard',StandardBackend,'swmm_getEasySewerStandardFixes',16,'standard-fixes'),
                ('custom',FlexiblePondingBackend,'swmm_getEasySewerNativeIOFixes',14,'native-io-fixes')):
            with self.subTest(family=family):
                value=backend(family)
                self.assertIs(type(value),cls)
                info=value.probe()
                self.assertTrue(info.available,info.reason)
                self.assertEqual(info.sha256,hashlib.sha256(Path(info.library).read_bytes()).hexdigest())
                if os.environ.get('EASYSEWER_CHECKPOINT_PACKAGED')=='1':
                    self.assertIsNone(value.library)
                    self.assertEqual(info.origin,'packaged-bytes')
                self.assertIn(f'easysewer:{capability}:{revision}',info.capabilities)
                self.assertIn('easysewer:checkpoint:2',info.capabilities)
                self.assertIn('easysewer:path-io:1',info.capabilities)
                lib=ctypes.CDLL(info.library)
                self.assertEqual(getattr(lib,symbol)(),revision)
                self.assertEqual(lib.swmm_checkpointVersion(),2)
                EVIDENCE.append(dict(family=family,backend_class=type(value).__name__,
                    sha256=info.sha256,origin=info.origin,numerical_policy=info.numerical_policy,capabilities=info.capabilities))
