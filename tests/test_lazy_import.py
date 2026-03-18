import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


class TestLazyImportAndCapabilities(unittest.TestCase):
    """Validate lazy import and native capability probing behavior."""

    @classmethod
    def setUpClass(cls):
        cls.project_root = Path(__file__).resolve().parents[1]
        cls.src_path = cls.project_root / "src"

    def _run_python(self, code: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-c", code],
            text=True,
            capture_output=True,
            cwd=self.project_root,
        )

    def test_import_easysewer_does_not_touch_cdll(self):
        code = f"""
import sys
sys.path.insert(0, r"{self.src_path}")
import ctypes
def fail_cdll(*args, **kwargs):
    raise RuntimeError("ctypes.CDLL should not be called during import")
ctypes.CDLL = fail_cdll
import easysewer
print("ok")
"""
        result = self._run_python(code)
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn("ok", result.stdout)

    def test_import_modelapi_does_not_touch_cdll(self):
        code = f"""
import sys
sys.path.insert(0, r"{self.src_path}")
import ctypes
def fail_cdll(*args, **kwargs):
    raise RuntimeError("ctypes.CDLL should not be called during ModelAPI import")
ctypes.CDLL = fail_cdll
from easysewer.ModelAPI import Model
print(Model.__name__)
"""
        result = self._run_python(code)
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn("Model", result.stdout)

    def test_capability_probe_does_not_load_cdll(self):
        code = f"""
import sys
sys.path.insert(0, r"{self.src_path}")
import ctypes
def fail_cdll(*args, **kwargs):
    raise RuntimeError("capability probe should not load CDLL")
ctypes.CDLL = fail_cdll
import easysewer
caps = easysewer.get_native_capabilities()
assert isinstance(caps, dict), "capabilities must be dict"
assert {{"swmm_solver", "swmm_output", "flexible_ponding"}}.issubset(caps.keys())
print("ok")
"""
        result = self._run_python(code)
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn("ok", result.stdout)

    def test_solver_constructor_raises_unified_error_when_native_unavailable(self):
        sys.path.insert(0, str(self.src_path))
        from easysewer.SolverAPI import SWMMSolverAPI
        from easysewer.utils import NativeCapabilityError
        with patch("easysewer.SolverAPI.require_native_capability", side_effect=NativeCapabilityError("native unavailable")):
            with self.assertRaises(NativeCapabilityError):
                SWMMSolverAPI()

    def test_output_constructor_raises_unified_error_when_native_unavailable(self):
        sys.path.insert(0, str(self.src_path))
        from easysewer.OutputAPI import SWMMOutputAPI
        from easysewer.utils import NativeCapabilityError
        with patch("easysewer.OutputAPI.require_native_capability", side_effect=NativeCapabilityError("native unavailable")):
            with self.assertRaises(NativeCapabilityError):
                SWMMOutputAPI()

    def test_get_native_capabilities_on_emscripten_returns_false_flags(self):
        sys.path.insert(0, str(self.src_path))
        from easysewer.utils import get_native_capabilities
        with patch("easysewer.utils.platform.system", return_value="Emscripten"):
            caps = get_native_capabilities()
        self.assertEqual(caps, {
            "swmm_solver": False,
            "swmm_output": False,
            "flexible_ponding": False,
        })


if __name__ == "__main__":
    unittest.main()
