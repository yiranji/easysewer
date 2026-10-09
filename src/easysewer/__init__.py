"""
EasySewer: An urban drainage modeling toolkit.
"""

__version__ = "2.0.1"

from importlib import import_module
from typing import TYPE_CHECKING, Any

__all__ = [
    "Model",
    "get_native_capabilities",
    "NativeCapabilityError",
]

_EXPORT_MAP = {
    "Model": (".model", "Model"),
    "get_native_capabilities": (".utils", "get_native_capabilities"),
    "NativeCapabilityError": (".utils", "NativeCapabilityError"),
}

if TYPE_CHECKING:
    from .model import Model
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
