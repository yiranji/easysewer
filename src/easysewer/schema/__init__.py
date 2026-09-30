"""Candidate 2.0 profile and record ownership contracts."""

from .profiles import EPA_SWMM_5_2_4, SwmmProfile
from .registry import (
    DecodedDocument, DecodedFeature, FeatureDecoder, FeatureDescriptor,
    RegistryError, SchemaRegistry, SupportLevel,
)

__all__ = [
    "EPA_SWMM_5_2_4", "SwmmProfile", "DecodedDocument", "DecodedFeature",
    "FeatureDecoder", "FeatureDescriptor", "RegistryError", "SchemaRegistry", "SupportLevel",
]
