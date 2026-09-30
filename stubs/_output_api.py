"""Explicit unavailable native OUT interface for the pure Python profile."""
from ..utils import NativeCapabilityError

class SWMMOutputAPI:
    def __init__(self, *args, **kwargs):
        raise NativeCapabilityError("Native SWMM output access is unavailable in the pure Python build; use easysewer.io.output for pure Python OUT reading.")
