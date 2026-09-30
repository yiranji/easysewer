"""Diagnostics shared by the 2.0 model and file formats (candidate API)."""

from .diagnostics import (Diagnostic, DiagnosticSubject, DiagnosticLocation, Severity,
                          SourceSpan, ValidationError, ValidationReport)

__all__ = ["Diagnostic", "DiagnosticSubject", "DiagnosticLocation", "Severity", "SourceSpan", "ValidationError", "ValidationReport"]
