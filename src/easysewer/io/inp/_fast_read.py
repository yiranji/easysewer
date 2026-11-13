"""Trusted parameter import without persistent document or provenance graphs.

The temporary line representation only exposes the small interface needed by
existing domain decoders. Values are tokenized once; rich lexical tokens are a
lazy compatibility fallback and are never part of the retained model state.
"""
import codecs
import re
from dataclasses import dataclass
from pathlib import Path

from ...model._trusted import trusted_scope
from ...model.identity import Ref, canonical_key
from ...model.provenance import RecordOrigin
from ...model.store import OpaqueConstraint
from ...validation import ValidationReport
from ...validation._cooperative import checkpointed

_HEADER = re.compile(r"^\s*\[\s*([^\[\]\r\n]+?)\s*\]\s*$")
_LINES = re.compile(r"[^\r\n]*(?:\r\n|\r|\n|$)")
_SPACE_TAB = re.compile(r"[ \t]+")
_EMPTY_REPORT = ValidationReport()


@dataclass(frozen=True, slots=True)
class RawRecord:
    section: str | None
    content: str
    after: tuple | None
    before: tuple | None
    order: int


@dataclass(frozen=True, slots=True)
class InputContext:
    source_path: str | None
    encoding: str
    passthrough: tuple[RawRecord, ...] = ()


def _values(data):
    # SWMM ends the record at the first semicolon, including inside quotes.
    # Common unquoted rows use a compiled separator. Only space/tab
    # delimit tokens; quoted paths retain literal backslashes.
    if '"' not in data:
        stripped = data.strip(' \t')
        return tuple(_SPACE_TAB.split(stripped)) if stripped else ()
    result = []
    cursor, size = 0, len(data)
    while cursor < size:
        if data[cursor] in ' \t':
            cursor += 1
            continue
        start = cursor
        if data[cursor] == '"':
            end = data.find('"', cursor + 1)
            if end < 0:
                raise ValueError('Unterminated quoted INP token')
            result.append(data[cursor + 1:end])
            cursor = end + 1
        else:
            while cursor < size and data[cursor] not in ' \t':
                cursor += 1
            result.append(data[start:cursor])
    return tuple(result)


class _Line:
    __slots__ = ('number', 'content', 'newline', 'start', 'section', 'kind',
                 'values', '_source', '_tokens')

    def __init__(self, number, content, newline, start, section, kind, values, source):
        self.number = number
        self.content = content
        self.newline = newline
        self.start = start
        self.section = section
        self.kind = kind
        self.values = values
        self._source = source
        self._tokens = None

    @property
    def raw(self):
        return self.content + self.newline

    @property
    def end(self):
        return self.start + len(self.content) + len(self.newline)

    @property
    def comment(self):
        pos = self.content.find(';')
        return None if pos < 0 else self.content[pos:]

    @property
    def tokens(self):
        # Compatibility only. Built-in source helpers are bypassed in the
        # trusted context, so ordinary parameter loading never needs this.
        if self._tokens is None:
            from .lexer import tokenize
            self._tokens = tokenize(self.content, offset=self.start,
                                    line=self.number, source=self._source,
                                    section=self.section)[0]
        return self._tokens


class _Section:
    __slots__ = ('name', 'header', 'lines')

    def __init__(self, name, header, lines):
        self.name, self.header, self.lines = name, header, tuple(lines)

    @property
    def records(self):
        return tuple(line for line in self.lines if line.kind in ('data', 'raw'))


class _Document:
    __slots__ = ('text', 'encoding', 'source', 'lines', 'sections', '_records')
    report = _EMPTY_REPORT

    def __init__(self, text, encoding, source):
        self.text, self.encoding, self.source = text, encoding, source
        lines, sections, records = [], [], {}
        current = None
        header, body = None, []
        for match in checkpointed(_LINES.finditer(text)):
            raw = match.group()
            if not raw:
                continue
            content = raw.rstrip('\r\n')
            newline = raw[len(content):]
            data = content.split(';', 1)[0]
            parsed_header = _HEADER.fullmatch(data)
            values = ()
            if parsed_header:
                if header is not None:
                    sections.append(_Section(current, header, body))
                current = parsed_header[1].strip().upper()
                kind = 'header'
            elif not content.strip(' \t'):
                kind = 'blank'
            elif content.lstrip(' \t').startswith(';'):
                kind = 'comment'
            elif current == 'TITLE' and not content.lstrip().startswith('['):
                kind = 'raw'
            else:
                kind = 'data'
                values = _values(data)
            line = _Line(len(lines) + 1, content, newline, match.start(), current,
                         kind, values, source)
            lines.append(line)
            if kind == 'header':
                header, body = line, []
            else:
                if header is not None:
                    body.append(line)
                if kind in ('data', 'raw'):
                    records.setdefault(current, []).append(line)
        if header is not None:
            sections.append(_Section(current, header, body))
        self.lines, self.sections = tuple(lines), tuple(sections)
        self._records = {key: tuple(value) for key, value in records.items()}

    def records(self, name):
        return self._records.get(name.strip().strip('[]').upper(), ())

    def find_sections(self, name):
        key = name.strip().strip('[]').upper()
        return tuple(section for section in self.sections if section.name == key)

    def to_bytes(self):
        return self.text.encode(self.encoding)


def read_into(model, path, *, encoding=None):
    path = Path(path).resolve()
    raw = path.read_bytes()
    name = encoding or ('utf-8-sig' if raw.startswith(codecs.BOM_UTF8) else 'utf-8')
    name = codecs.lookup(name).name
    if name == 'utf-8' and raw.startswith(codecs.BOM_UTF8):
        name = 'utf-8-sig'
    document = _Document(raw.decode(name), name, str(path))
    return read_document_into(model, document)


def read_document_into(model, document):
    """Decode known parameters and retain only unclaimed parameter rows."""
    with trusted_scope():
        store = model._store
        claimed, outputs, available = set(), [], set()
        origin = RecordOrigin(kind='untracked')
        for descriptor, codec in checkpointed(model._schema.bindings):
            if (model.profile.key not in descriptor.profiles
                    or any(dependency not in available for dependency in descriptor.requires)):
                continue
            output = codec.decode(document, model.profile)
            # Conversion failures must not silently produce a partial model.
            # Unsupported but otherwise valid syntax stays unclaimed below.
            if output.report.errors:
                issue = output.report.errors[0]
                raise ValueError(f'Unable to decode INP parameters: {issue.message}')
            available.add(descriptor.key)
            claimed.update(output.claimed_lines)
            outputs.append((descriptor.key, output.value.bindings))
            for item in checkpointed(output.value.records):
                spec = store._specs[item.collection]
                key = canonical_key(spec.key_of(item.value))
                store._records[item.collection][key] = item.value
                store._origins[Ref(collection=item.collection, key=key)] = origin
        store._changed()
        opaque = [line for line in checkpointed(document.lines)
                  if line.kind in ('data', 'raw', 'invalid_header')
                  and line.number not in claimed]
        passthrough = ()
        if opaque:
            anchors = {binding.line: (feature, binding.key)
                       for feature, bindings in outputs for binding in bindings}
            before, next_anchor = {}, None
            for line in checkpointed(reversed(document.lines)):
                if line.number in anchors:
                    next_anchor = anchors[line.number]
                before[line.number] = next_anchor
            rows, previous = [], None
            for line in checkpointed(document.lines):
                anchor = anchors.get(line.number)
                if anchor is not None:
                    previous = anchor
                elif line.kind in ('data', 'raw', 'invalid_header'):
                    rows.append(RawRecord(line.section, line.content, previous,
                                          before[line.number], line.number))
            passthrough = tuple(rows)
        # Unknown records can contain references that the structured graph
        # cannot rewrite. Preserve the normal editing guard without building
        # source locations or running input validity checks.
        store._opaque = tuple(
            OpaqueConstraint(description=f"Unparsed [{row.section}] at line {row.order}")
            for row in passthrough if row.section != "TITLE"
        )
        model._input_context = InputContext(document.source, document.encoding, passthrough)
        model._source = None
        model._json_source = None
        return model
