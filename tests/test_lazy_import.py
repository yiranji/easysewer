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

    def test_import_model_does_not_touch_cdll(self):
        code = f"""
import sys
sys.path.insert(0, r"{self.src_path}")
import ctypes
def fail_cdll(*args, **kwargs):
    raise RuntimeError("ctypes.CDLL should not be called during Model import")
ctypes.CDLL = fail_cdll
from easysewer import Model
print(Model.__name__)
"""
        result = self._run_python(code)
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn("Model", result.stdout)

    def test_v2_document_and_registry_do_not_import_native_or_legacy_model_modules(self):
        code = f"""
import importlib.abc
import sys
sys.path.insert(0, r"{self.src_path}")
class RejectNativeAndLegacy(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in {{"ctypes", "easysewer.runtime._solver_api", "easysewer.runtime._output_api",
                        "easysewer.UDM", "easysewer.ModelAPI"}}:
            raise AssertionError("unexpected dependency: " + fullname)
sys.meta_path.insert(0, RejectNativeAndLegacy())
from easysewer.io.inp import InpDocument
from easysewer.schema import SchemaRegistry
from easysewer.model import Model as CandidateModel
from easysewer import Model
assert Model is CandidateModel
document = InpDocument.from_text("[FUTURE]\\nx y\\n")
decoded = SchemaRegistry().decode(document)
assert decoded.document.to_bytes() == b"[FUTURE]\\nx y\\n"
assert len(decoded.opaque_records) == 1
assert CandidateModel().to_document().text == ""
print("ok")
"""
        result = self._run_python(code)
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn("ok", result.stdout)

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
        from easysewer.runtime._solver_api import SWMMSolverAPI
        from easysewer.utils import NativeCapabilityError
        with patch("easysewer.runtime._solver_api.require_native_capability", side_effect=NativeCapabilityError("native unavailable")):
            with self.assertRaises(NativeCapabilityError):
                SWMMSolverAPI()

    def test_output_constructor_raises_unified_error_when_native_unavailable(self):
        sys.path.insert(0, str(self.src_path))
        from easysewer.runtime._output_api import SWMMOutputAPI
        from easysewer.utils import NativeCapabilityError
        with patch("easysewer.runtime._output_api.require_native_capability", side_effect=NativeCapabilityError("native unavailable")):
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
