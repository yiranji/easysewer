"""Candidate 2.0 INP document API; independent of the 1.x Model reader."""

from .document import (
    DocumentPatch, InpDocument, InpLine, InpSection, PatchConflictError, TextEdit,
)
from .lexer import Token, format_token
from .durations import DurationCodec

__all__ = [
    "DocumentPatch", "InpDocument", "InpLine", "InpSection", "PatchConflictError",
    "TextEdit", "Token", "format_token", "DurationCodec",
]
