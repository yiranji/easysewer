"""Immutable, lossless INP documents and revision-checked text patches.

This is a document API, not a hydraulic model. Copying a document neither
validates physics nor rebases external file references.
"""

import codecs
import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from ...validation import Diagnostic, SourceSpan, ValidationReport
from .lexer import Token, format_token, tokenize
from ...validation._cooperative import checkpointed
from ...validation._cooperative import checkpoint_scope

_HEADER = re.compile(r"^\s*\[\s*([^\[\]\r\n]+?)\s*\]\s*$")
_LINES = re.compile(r"[^\r\n]*(?:\r\n|\r|\n|$)")


def section_key(name: str) -> str:
    """Normalize a section name only; object IDs and token values stay intact."""
    stripped = name.strip()
    if stripped.startswith("[") and stripped.endswith("]"):
        stripped = stripped[1:-1].strip()
    if not stripped or any(char in stripped for char in checkpointed("[];\r\n\x00")):
        raise ValueError(f"Invalid section name: {name!r}")
    return stripped.upper()


@dataclass(frozen=True, kw_only=True)
class InpLine:
    number: int
    raw: str
    content: str
    newline: str
    start: int
    end: int
    section: str | None
    kind: str
    tokens: tuple[Token, ...] = ()
    comment: str | None = None

    @property
    def values(self) -> tuple[str, ...]:
        return tuple(token.value for token in checkpointed(self.tokens))


@dataclass(frozen=True, kw_only=True)
class InpSection:
    """One section occurrence. Repeated headers intentionally stay separate."""

    name: str
    header: InpLine
    lines: tuple[InpLine, ...]

    @property
    def records(self) -> tuple[InpLine, ...]:
        return tuple(line for line in checkpointed(self.lines) if line.kind in ("data", "raw"))


@dataclass(frozen=True, kw_only=True)
class TextEdit:
    start: int
    end: int
    replacement: str

    def __post_init__(self):
        if self.start < 0 or self.end < self.start:
            raise ValueError("Invalid edit range")
        if not isinstance(self.replacement, str):
            raise TypeError("Replacement must be text")


@dataclass(frozen=True, kw_only=True)
class DocumentPatch:
    revision: str
    edits: tuple[TextEdit, ...]

    def __post_init__(self):
        object.__setattr__(self, "edits", tuple(self.edits))


class PatchConflictError(ValueError):
    """A patch is stale, overlaps itself, or addresses another document."""


@dataclass(frozen=True, kw_only=True)
class InpDocument:
    text: str
    encoding: str
    source: str | None
    lines: tuple[InpLine, ...]
    sections: tuple[InpSection, ...]
    report: ValidationReport
    _bytes: bytes

    @classmethod
    def from_bytes(
        cls, data: bytes, *, encoding: str | None = None, source: str | None = None, checkpoint=None
    ) -> "InpDocument":
        """Strict decoding; detect a UTF-8 BOM, otherwise default to UTF-8.

        Other encodings require an explicit argument. Undecodable input is
        never repaired by dropping bytes or silently guessing a code page.
        """
        with checkpoint_scope(checkpoint):
            if not isinstance(data, bytes):
                raise TypeError("Expected bytes")
            name = encoding or ("utf-8-sig" if data.startswith(codecs.BOM_UTF8) else "utf-8")
            name = codecs.lookup(name).name
            # An explicitly requested UTF-8 codec must still recognize its BOM.
            if name == "utf-8" and data.startswith(codecs.BOM_UTF8):
                name = "utf-8-sig"
            text = data.decode(name, errors="strict")
            return cls._parse(text, encoding=name, source=source, data=data)

    @classmethod
    def from_text(
        cls, text: str, *, encoding: str = "utf-8", source: str | None = None, checkpoint=None
    ) -> "InpDocument":
        with checkpoint_scope(checkpoint):
            return cls.from_bytes(text.encode(encoding, errors="strict"),
                                  encoding=encoding, source=source)

    @classmethod
    def read(cls, path: str | Path, *, encoding: str | None = None) -> "InpDocument":
        path = Path(path).resolve()
        return cls.from_bytes(path.read_bytes(), encoding=encoding, source=str(path))

    @classmethod
    def _parse(cls, text: str, *, encoding: str, source: str | None, data: bytes):
        lines = []
        diagnostics = []
        current_section = None
        for match in checkpointed(_LINES.finditer(text)):
            raw = match.group()
            if not raw:
                continue
            number = len(lines) + 1
            content = raw.rstrip("\r\n")
            newline = raw[len(content):]
            data_text = content.split(";", 1)[0]
            header = _HEADER.fullmatch(data_text)
            if header and (not header.group(1).strip() or "\x00" in header.group(1)):
                # Invalid but decodable input still belongs to the document.
                # Diagnose it below instead of raising from section_key().
                header = None
            tokens, comment, issues = (), None, ()
            if header:
                current_section = section_key(header.group(1))
                kind = "header"
                if ";" in content:
                    comment = content[content.index(";"):]
            elif not content.strip(" \t"):
                kind = "blank"
            elif content.lstrip(" \t").startswith(";"):
                kind = "comment"
                comment = content[content.index(";"):]
            elif current_section == "TITLE" and not content.lstrip().startswith("["):
                # TITLE is prose, not a quoted-token grammar or embedded JSON.
                kind = "raw"
            else:
                kind = "data"
                tokens, comment, issues = tokenize(
                    content, offset=match.start(), line=number,
                    source=source, section=current_section,
                )
                if content.lstrip(" \t").startswith("["):
                    # Do not attribute following records to the preceding section.
                    current_section = None
                    kind = "invalid_header"
                    diagnostics.append(Diagnostic(
                        code="inp.invalid_section_header",
                        message="Malformed section header; subsequent records have no section.",
                        span=SourceSpan(source=source, line=number, column=1,
                                        end_column=len(content) + 1),
                    ))
                elif current_section is None:
                    diagnostics.append(Diagnostic(
                        code="inp.record_without_section", message="Data occurs outside a section.",
                        span=SourceSpan(source=source, line=number, column=1,
                                        end_column=len(content) + 1),
                    ))
            diagnostics.extend(issues)
            if "\x00" in content:
                column = content.index("\x00") + 1
                diagnostics.append(Diagnostic(
                    code="inp.nul_character", message="NUL is not valid INP text.",
                    span=SourceSpan(source=source, line=number, column=column,
                                    end_column=column + 1), section=current_section,
                ))
            lines.append(InpLine(
                number=number, raw=raw, content=content, newline=newline,
                start=match.start(), end=match.end(), section=current_section,
                kind=kind, tokens=tokens, comment=comment,
            ))
        sections = []
        header_line = None
        body = []
        for line in checkpointed(lines):
            if line.kind in ("header", "invalid_header"):
                if header_line is not None:
                    sections.append(InpSection(name=header_line.section,
                                               header=header_line, lines=tuple(body)))
                header_line = line if line.kind == "header" else None
                body = []
            elif header_line is not None:
                body.append(line)
        if header_line is not None:
            sections.append(InpSection(name=header_line.section, header=header_line,
                                       lines=tuple(body)))
        return cls(text=text, encoding=encoding, source=source, lines=tuple(lines),
                   sections=tuple(sections), report=ValidationReport(diagnostics=tuple(diagnostics)),
                   _bytes=data)

    @property
    def revision(self) -> str:
        # Identical bytes can have different character offsets under different
        # encodings. A patch must bind to both the bytes and their interpretation.
        return hashlib.sha256(self.encoding.encode("ascii") + b"\0" + self._bytes).hexdigest()

    @property
    def newline(self) -> str:
        """The first existing physical newline, or LF for an empty document."""
        return next((line.newline for line in checkpointed(self.lines) if line.newline), "\n")

    def find_sections(self, name: str) -> tuple[InpSection, ...]:
        key = section_key(name)
        return tuple(section for section in checkpointed(self.sections) if section.name == key)

    def records(self, name: str) -> tuple[InpLine, ...]:
        return tuple(line for section in checkpointed(self.find_sections(name)) for line in checkpointed(section.records))

    def patch(self, edits: Iterable[TextEdit]) -> DocumentPatch:
        return DocumentPatch(revision=self.revision, edits=tuple(edits))

    def replace_token(self, line: int, index: int, value: str) -> "InpDocument":
        if line < 1 or line > len(self.lines):
            raise IndexError("Line numbers are one-based")
        tokens = self.lines[line - 1].tokens
        if index < 0 or index >= len(tokens):
            raise IndexError("Token index is out of range")
        token = tokens[index]
        return self.apply(self.patch((TextEdit(start=token.start, end=token.end,
                                              replacement=format_token(value)),)))

    def apply(self, patch: DocumentPatch, *, checkpoint=None) -> "InpDocument":
        """Apply all edits or none, then reparse; diagnostics remain inspectable.

        This low-level operation does not claim semantic validation or update
        object references. Saving an invalid source for inspection is permitted.
        """
        with checkpoint_scope(checkpoint):
            if patch.revision != self.revision:
                raise PatchConflictError("Patch revision does not match this document")
            edits = sorted(patch.edits, key=lambda edit: (edit.start, edit.end))
            previous = None
            for edit in checkpointed(edits):
                if edit.end > len(self.text):
                    raise PatchConflictError("Edit range exceeds the document")
                if previous is not None and (edit.start < previous.end or edit.start == previous.start):
                    raise PatchConflictError("Edit ranges overlap or have ambiguous insertion order")
                previous = edit
            if not edits:
                return self
            pieces = []
            cursor = 0
            for edit in checkpointed(edits):
                pieces.extend((self.text[cursor:edit.start], edit.replacement))
                cursor = edit.end
            pieces.append(self.text[cursor:])
            text = "".join(pieces)
            if text == self.text:
                return self
            return type(self).from_text(text, encoding=self.encoding, source=self.source)

    def to_bytes(self, *, encoding: str | None = None) -> bytes:
        if encoding is None or codecs.lookup(encoding).name == self.encoding:
            return self._bytes
        return self.text.encode(encoding, errors="strict")

    def write(self, path: str | Path, *, encoding: str | None = None) -> Path:
        """Atomically save the document, without rebasing resource paths.

        Encoding happens before filesystem writes. Failure removes only this
        operation's temporary file and leaves the existing destination intact.
        """
        data = self.to_bytes(encoding=encoding)
        from .._atomic import write_bytes
        return write_bytes(path, data)
