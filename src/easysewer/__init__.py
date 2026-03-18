"""
EasySewer: An urban drainage modeling toolkit.
"""

__version__ = "1.0.3"

from importlib import import_module
from typing import TYPE_CHECKING, Any

__all__ = [
    "UrbanDrainageModel",
    "Model",
    "SWMMOutputAPI",
    "SWMMSolverAPI",
    "FlexiblePondingSolverAPI",
    "JsonHandler",
    "ControlList",
    "ControlRule",
    "get_native_capabilities",
    "NativeCapabilityError",
]

_EXPORT_MAP = {
    "UrbanDrainageModel": (".UDM", "UrbanDrainageModel"),
    "Model": (".ModelAPI", "Model"),
    "SWMMOutputAPI": (".OutputAPI", "SWMMOutputAPI"),
    "SWMMSolverAPI": (".SolverAPI", "SWMMSolverAPI"),
    "FlexiblePondingSolverAPI": (".SolverAPI", "FlexiblePondingSolverAPI"),
    "JsonHandler": (".JsonHandler", "JsonHandler"),
    "ControlList": (".Control", "ControlList"),
    "ControlRule": (".Control", "ControlRule"),
    "get_native_capabilities": (".utils", "get_native_capabilities"),
    "NativeCapabilityError": (".utils", "NativeCapabilityError"),
}

if TYPE_CHECKING:
    from .Control import ControlList, ControlRule
    from .JsonHandler import JsonHandler
    from .ModelAPI import Model
    from .OutputAPI import SWMMOutputAPI
    from .SolverAPI import SWMMSolverAPI, FlexiblePondingSolverAPI
    from .UDM import UrbanDrainageModel
    from .utils import NativeCapabilityError, get_native_capabilities


def __getattr__(name: str) -> Any:
    if name not in _EXPORT_MAP:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attr_name = _EXPORT_MAP[name]
    module = import_module(module_name, __name__)
    value = getattr(module, attr_name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(list(globals().keys()) + __all__)
