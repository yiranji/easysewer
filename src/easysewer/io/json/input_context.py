"""Portable input context without claiming original-document provenance."""

import codecs

from ...validation._cooperative import checkpointed
from ._work import copy_json
from .document import fail


FORMAT = "easysewer:inp-context"
VERSION = "1.0"
_FIELDS = {"format", "schema_version", "source_path", "encoding", "passthrough"}
_ROW_FIELDS = {"section", "content", "after", "before", "order"}


def _error(message, path=()):
    fail("json.input_context", message, path=("input_context", *path))


def _anchor(value, path):
    if value is None:
        return None
    if (type(value) is not list or len(value) != 2 or type(value[0]) is not str
            or type(value[1]) is not list or not value[1]
            or any(type(part) is not str for part in checkpointed(value[1]))):
        _error("Input anchors require a feature and an array of row-key strings", path)
    from ...model.identity import namespace_key
    try:
        namespace_key(value[0])
    except ValueError as error:
        _error(str(error), path)
    return value[0], tuple(checkpointed(value[1]))


def read_context(value):
    """Return context and whether unknown context content requires preservation."""
    if type(value) is not dict:
        _error("Input context must be an object")
    if value.get("format") != FORMAT or value.get("schema_version") != VERSION:
        return None, True
    if set(value) - _FIELDS:
        return None, True
    if _FIELDS - set(value):
        _error("Input context is missing required members")
    source = value["source_path"]
    if source is not None and (type(source) is not str or "\x00" in source):
        _error("Source path must be a string without NUL or null", ("source_path",))
    encoding = value["encoding"]
    if type(encoding) is not str:
        _error("Input encoding must be a string", ("encoding",))
    try:
        codecs.lookup(encoding)
    except LookupError as error:
        _error(str(error), ("encoding",))
    rows = value["passthrough"]
    if type(rows) is not list:
        _error("Passthrough must be an array", ("passthrough",))
    from ..inp._fast_read import InputContext, RawRecord
    from ..inp.document import section_key
    result, previous = [], -1
    for index, row in checkpointed(enumerate(rows)):
        path = ("passthrough", index)
        if type(row) is not dict:
            _error("Passthrough entries must be objects", path)
        if set(row) - _ROW_FIELDS:
            return None, True
        if _ROW_FIELDS - set(row):
            _error("Passthrough entry is missing required members", path)
        section = row["section"]
        if section is not None:
            if type(section) is not str:
                _error("Passthrough section must be a string or null", (*path, "section"))
            try:
                canonical = section_key(section)
            except ValueError as error:
                _error(str(error), (*path, "section"))
            if canonical != section:
                _error("Passthrough section must use its canonical spelling", (*path, "section"))
        content = row["content"]
        if (type(content) is not str or any(char in content for char in "\r\n\x00")
                or content.lstrip().startswith("[")):
            _error("Passthrough content must be one physical non-header line", (*path, "content"))
        order = row["order"]
        if type(order) is not int or order <= previous:
            _error("Passthrough order must be nonnegative and strictly increasing", (*path, "order"))
        previous = order
        result.append(RawRecord(section=section, content=content,
            after=_anchor(row["after"], (*path, "after")),
            before=_anchor(row["before"], (*path, "before")), order=order))
    return InputContext(source_path=source, encoding=encoding, passthrough=tuple(result)), False


def write_context(context):
    def anchor(value):
        return None if value is None else [value[0], list(checkpointed(value[1]))]
    return dict(format=FORMAT, schema_version=VERSION, source_path=context.source_path,
        encoding=context.encoding, passthrough=[dict(section=row.section, content=row.content,
            after=anchor(row.after), before=anchor(row.before), order=row.order)
            for row in checkpointed(context.passthrough)])


def context_schema():
    anchor = {"anyOf": [{"type": "null"}, {"type": "array", "minItems": 2, "maxItems": 2,
        "prefixItems": [{"type": "string"}, {"type": "array", "minItems": 1,
            "items": {"type": "string"}}]}]}
    row = {"type": "object", "required": sorted(_ROW_FIELDS), "properties": {
        "section": {"type": ["string", "null"]}, "content": {"type": "string"},
        "after": copy_json(anchor), "before": copy_json(anchor),
        "order": {"type": "integer", "minimum": 0}}}
    return {"type": "object", "required": sorted(_FIELDS), "properties": {
        "format": {"const": FORMAT}, "schema_version": {"const": VERSION},
        "source_path": {"type": ["string", "null"]}, "encoding": {"type": "string"},
        "passthrough": {"type": "array", "items": row}}}
