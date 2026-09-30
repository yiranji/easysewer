"""Shared native text-file constraints and immutable source-byte ownership."""

from dataclasses import dataclass, field
from pathlib import Path
import re

from ..validation import Diagnostic, SourceSpan, ValidationReport
from ..validation._cooperative import checkpoint, checkpointed
from ._atomic import write_bytes

SPACE = ' \t\v\f\r\n'


def tokens(line):
    return tuple(re.split('[ \\t\\v\\f\\r\\n]+', line.strip(SPACE))) if line.strip(SPACE) else ()


def error(message, *, source=None, line=1, text='', code='data.invalid_text'):
    ValidationReport(diagnostics=(Diagnostic(code=code, message=message,
        span=SourceSpan(source=source, line=line, column=1, end_column=len(text)+1)),)).raise_for_errors()


def native_encoding(encoding):
    probe='SWMM5 ;0123456789:/-.eE+\t\r\n'
    if probe.encode(encoding) != probe.encode('ascii'):
        raise ValueError('Native data files require a BOM-free ASCII-compatible encoding')


def decode_lines(data, *, encoding, source=None, token_limit=None):
    if not isinstance(data, bytes):
        raise TypeError('Data source must be immutable bytes')
    native_encoding(encoding)
    if data.startswith((b'\xef\xbb\xbf', b'\xff\xfe', b'\xfe\xff')):
        error('Native external data files do not support a byte-order mark', source=source)
    checkpoint()
    text=data.decode(encoding, errors='strict')
    checkpoint()
    # Only LF is a physical delimiter here. Keep the trailing empty row and
    # reject embedded CR/control characters exactly as the native file grammar.
    def rows():
        start = 0
        while True:
            end = text.find('\n', start)
            if end < 0:
                yield text[start:]
                return
            yield text[start:end]
            start = end + 1

    for number, row in enumerate(checkpointed(rows()), 1):
        if row.endswith('\r'):
            row=row[:-1]
        if any(c in row for c in ('\r','\x00','\x1a')) or len(row.encode(encoding)) > 1022:
            error('Row violates native text/buffer boundaries', source=source, line=number, text=row)
        if token_limit is not None:
            # sscanf reads these tokens before the native comment check.
            count, width=token_limit
            if any(len(token.encode(encoding)) > width for token in tokens(row)[:count]):
                error('Token exceeds the fixed native reader buffer', source=source, line=number, text=row)
        yield number, row


def encode_lines(lines, encoding, *, token_limit=None):
    native_encoding(encoding)
    data=('\n'.join(lines)+'\n').encode(encoding, errors='strict')
    tuple(decode_lines(data, encoding=encoding, token_limit=token_limit))
    return data


@dataclass(frozen=True, kw_only=True)
class TextData:
    _original: bytes | None = field(default=None, init=False, repr=False, compare=False)
    _encoding: str = field(default='utf-8', init=False, repr=False, compare=False)
    _diagnostics: tuple[Diagnostic, ...] = field(default=(), init=False, repr=False, compare=False)

    @property
    def report(self):
        return ValidationReport(diagnostics=self._diagnostics)

    @classmethod
    def read(cls, path, **kwargs):
        path=Path(path)
        return cls.from_bytes(path.read_bytes(), source=str(path), **kwargs)

    def write(self, path, **kwargs):
        return write_bytes(path, self.to_bytes(**kwargs))

    def _retain(self, data, encoding, issues):
        object.__setattr__(self, '_original', data)
        object.__setattr__(self, '_encoding', encoding)
        object.__setattr__(self, '_diagnostics', tuple(issues))
        return self
