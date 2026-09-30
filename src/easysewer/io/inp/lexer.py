"""Lossless tokens with SWMM's comment and double-quote conventions.

The v5.2.4 engine ends a line at its first semicolon, even inside quotes.
Backslashes are literal path characters, not Python or shell escapes.
"""

from dataclasses import dataclass

from ...validation import Diagnostic, SourceSpan
from ...validation._cooperative import checkpoint


@dataclass(frozen=True, kw_only=True)
class Token:
    value: str
    raw: str
    start: int
    end: int
    quoted: bool
    span: SourceSpan


def tokenize(
    content: str, *, offset: int, line: int, source: str | None, section: str | None
) -> tuple[tuple[Token, ...], str | None, tuple[Diagnostic, ...]]:
    """Return tokens, the untouched comment, and lexical diagnostics.

    Offsets refer to characters in the decoded document, not encoded bytes.
    Only spaces and tabs delimit tokens; physical newlines are removed upstream.
    """
    comment_start = content.find(";")
    comment = None if comment_start < 0 else content[comment_start:]
    data = content if comment_start < 0 else content[:comment_start]
    tokens = []
    diagnostics = []
    cursor = 0
    while cursor < len(data):
        if cursor % 256 == 0:checkpoint()
        if data[cursor] in " \t":
            cursor += 1
            continue
        start = cursor
        quoted = data[cursor] == '"'
        if quoted:
            end_quote = data.find('"', cursor + 1)
            if end_quote < 0:
                cursor = len(data)
                value = data[start + 1:]
                diagnostics.append(Diagnostic(
                    code="inp.unterminated_quote",
                    message="Double-quoted token has no closing quote before the comment or line end.",
                    span=SourceSpan(source=source, line=line, column=start + 1,
                                    end_column=cursor + 1),
                    section=section,
                ))
            else:
                cursor = end_quote + 1
                value = data[start + 1:end_quote]
        else:
            while cursor < len(data) and data[cursor] not in " \t":
                if cursor % 256 == 0:checkpoint()
                cursor += 1
            value = data[start:cursor]
        tokens.append(Token(
            value=value, raw=data[start:cursor], start=offset + start,
            end=offset + cursor, quoted=quoted,
            span=SourceSpan(source=source, line=line, column=start + 1,
                            end_column=cursor + 1),
        ))
    return tuple(tokens), comment, tuple(diagnostics)


def format_token(value: str) -> str:
    """Encode one token, rejecting characters SWMM cannot quote losslessly."""
    if not isinstance(value, str):
        raise TypeError("An INP token must be a string")
    if any(char in value for char in '\r\n;"\x00'):
        raise ValueError("An INP token cannot contain quotes, semicolons, NUL, or newlines")
    # A first-column display name such as [North] must not become a header.
    if not value or value.startswith('[') or any(char in value for char in " \t"):
        return f'"{value}"'
    return value
