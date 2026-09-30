"""Immutable UTF-8 JSON source, strict parsing and atomic persistence."""

from dataclasses import dataclass
from bisect import bisect_right
import json
import math
from pathlib import Path
import re

from ...validation import Diagnostic, SourceSpan, ValidationError, ValidationReport
from ...validation._cooperative import checkpoint, checkpointed
from .._record_work import record_dumps


def json_path(path):
    """An unambiguous display path from explicit JSON member/index components."""
    return '$' + ''.join(f'[{part}]' if type(part) is int else '.'+part
        if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', part) else '['+json.dumps(part, ensure_ascii=False)+']'
        for part in path)


def fail(code, message, *, path=None, source=None, subject=None, related=()):
    error = ValidationError(ValidationReport(diagnostics=(Diagnostic(
        code=code, message=message, field=json_path(path) if type(path) is tuple else path, feature="easysewer:json",
        subject=subject, related=related,
        span=SourceSpan(source=source, line=1, column=1, end_column=1) if source else None,
    ),)))
    error._json_paths = (path if type(path) is tuple else None,)
    raise error


class JsonLocations:
    """Lazily index an already validated document once; never guess key offsets."""
    def __init__(self, document):
        self.document = document
        self.positions = None

    def _index(self):
        text = self.document.text
        decoder = json.JSONDecoder()
        positions = {}

        def whitespace(index):
            start = index
            while index < len(text) and text[index] in ' \t\r\n':
                index += 1
                if (index - start) % 256 == 0: checkpoint()
            return index

        def value(index, path):
            checkpoint()
            index = whitespace(index)
            start = index
            if text[index] == '{':
                index = whitespace(index + 1)
                while text[index] != '}':
                    key, index = decoder.raw_decode(text, index)
                    index = whitespace(index)
                    index = value(index + 1, (*path, key))  # validated colon
                    index = whitespace(index)
                    if text[index] != ',': break
                    index = whitespace(index + 1)
                index += 1
            elif text[index] == '[':
                index = whitespace(index + 1)
                count = 0
                while text[index] != ']':
                    index = whitespace(value(index, (*path, count)))
                    count += 1
                    if text[index] != ',': break
                    index = whitespace(index + 1)
                index += 1
            else:
                _, index = decoder.raw_decode(text, index)
            positions[path] = start, index
            return index

        value(0, ())
        newlines = [-1, *(i for i, char in checkpointed(enumerate(text)) if char == '\n')]
        checkpoint()
        self.positions, self.newlines = positions, newlines

    def span(self, path=()):
        if self.positions is None: self._index()
        # A missing member has no token. Locate its existing parent container.
        while path not in self.positions and path: path = path[:-1]
        start, end = self.positions[path]
        line = bisect_right(self.newlines, start)
        line_start = self.newlines[line - 1] + 1
        newline = self.document.text.find('\n', start, end)
        if newline >= 0: end = newline
        return SourceSpan(source=self.document.source, line=line, column=start-line_start+1,
                          end_column=end-line_start+1)


def check_json(value, path="$", depth=0):
    if depth > 128:
        fail("json.depth", "JSON nesting exceeds the supported limit", path=path)
    if value is None or type(value) in (str, bool):
        return
    if type(value) in (float, int):
        if type(value) is int and abs(value) > 9007199254740991:
            fail("json.integer_precision", "Integer exceeds interoperable JSON precision", path=path)
        if not math.isfinite(value):
            fail("json.number", "JSON numbers must be finite", path=path)
        return
    if type(value) is list:
        for index, item in enumerate(checkpointed(value)):
            check_json(item, f"{path}[{index}]", depth+1)
        return
    if type(value) is dict and all(type(key) is str for key in checkpointed(value)):
        for key, item in checkpointed(value.items()):
            check_json(item, f"{path}.{key}", depth+1)
        return
    fail("json.value_type", "Expected a JSON scalar, array or string-keyed object", path=path)


def _pairs(pairs):
    result = {}
    for key, value in checkpointed(pairs):
        if key in result:
            fail("json.duplicate_key", f"Duplicate JSON member {key!r}")
        result[key] = value
    return result


def parse(text, *, source=None):
    try:
        checkpoint()
        value = json.loads(text, object_pairs_hook=_pairs,
                           parse_constant=lambda value: fail("json.number", f"Non-finite JSON value {value}"))
        checkpoint()
        check_json(value)
        checkpoint()
        return value
    except json.JSONDecodeError as error:
        raise ValidationError(ValidationReport(diagnostics=(Diagnostic(
            code="json.syntax", message=error.msg, feature="easysewer:json",
            span=SourceSpan(source=source,line=error.lineno,column=error.colno,end_column=error.colno+1),
        ),))) from error
    except (RecursionError, UnicodeError) as error:
        fail("json.syntax", str(error))


@dataclass(frozen=True, kw_only=True)
class JsonDocument:
    """Retain original bytes; each data access returns an independent value tree."""
    text: str
    source: str | None = None
    _bytes: bytes = b""

    def __post_init__(self):
        if type(self.text) is not str or type(self._bytes) is not bytes:
            raise TypeError("JSON documents require immutable text and bytes")
        parse(self.text, source=self.source)
        checkpoint()
        data = self._bytes or self.text.encode("utf-8")
        if data.decode("utf-8-sig") != self.text:
            raise ValueError("JSON source bytes do not match its text")
        object.__setattr__(self, "_bytes", data)
        checkpoint()

    @classmethod
    def from_text(cls, text, *, source=None):
        return cls(text=text, source=source)

    @classmethod
    def from_data(cls, value, *, source=None):
        check_json(value)
        return cls(text=record_dumps(value)+"\n", source=source)

    @classmethod
    def from_bytes(cls, data, *, source=None):
        try:
            text = data.decode("utf-8-sig")
        except UnicodeError as error:
            fail("json.encoding", str(error), source=source)
        return cls(text=text, source=source, _bytes=data)

    @classmethod
    def read(cls, path):
        path = Path(path).resolve()
        return cls.from_bytes(path.read_bytes(), source=str(path))

    @property
    def data(self):
        return parse(self.text, source=self.source)

    def to_bytes(self):
        return self._bytes

    def write(self, path):
        from .._atomic import write_bytes
        return write_bytes(path, self.to_bytes())
