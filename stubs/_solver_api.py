"""Explicit unavailable native solver interfaces for the pure Python profile."""
from ..utils import NativeCapabilityError

class SWMMSolverAPI:
    def __init__(self, *args, **kwargs):
        raise NativeCapabilityError("SWMM solver is unavailable in the pure Python build; use a supported native easysewer build for simulation.")

class FlexiblePondingSolverAPI(SWMMSolverAPI):
    def __init__(self, *args, **kwargs):
        raise NativeCapabilityError("FlexiblePonding solver is unavailable in the pure Python build; use a supported native easysewer build for simulation.")
