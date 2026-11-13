"""Trusted parameter serialization without source reconstruction or validation.

Built-in codecs own parameter conversion and record order;
this layer performs no model correctness, writer ownership, or coverage checks.
"""
from copy import copy
from dataclasses import replace
from pathlib import Path
import re

from .._atomic import write_bytes

from ...model._trusted import trusted_scope
from ...model.file_resources import replace_path
from ...model.identity import canonical_key
from ...schema.structured import FeatureEncoding
from ...schema.registry import RegistryError
from ...validation._cooperative import checkpointed


_UNREPRESENTABLE_TOKEN = re.compile(r'[\r\n;"\x00]')


def _token(value):
    # These characters cannot be quoted in SWMM without changing the value.
    # This is an encoding limit, not a check of model or field validity.
    if _UNREPRESENTABLE_TOKEN.search(value):
        raise ValueError("INP cannot represent quotes, semicolons, NUL or newlines inside a token")
    if not value or value.startswith('[') or ' ' in value or '\t' in value:
        return '"' + value + '"'
    return value


def _line(row):
    if row.raw_text is None:
        return ' '.join(map(_token, row.values))
    text = row.raw_text
    if '\r' in text or '\n' in text or '\x00' in text or text.lstrip().startswith('['):
        raise ValueError("Raw INP data must be one physical line without a section header")
    return text


def _context(model):
    context = getattr(model, '_input_context', None)
    if context is not None:
        return context
    source = getattr(model, '_source', None)
    if source is None:
        return None
    # An explicitly checked import can use this writer without discarding its
    # unstructured records. Build only the lightweight anchors needed here.
    from bisect import bisect_left
    from ._fast_read import InputContext, RawRecord
    bindings = sorted((line, (feature, key)) for line, feature, key in source.bindings)
    numbers = [item[0] for item in bindings]
    raw = []
    for line in source.decoded.opaque_records:
        index = bisect_left(numbers, line.number)
        raw.append(RawRecord(section=line.section, content=line.content,
            after=bindings[index - 1][1] if index else None,
            before=bindings[index][1] if index < len(bindings) else None,
            order=line.number))
    document = source.decoded.document
    return InputContext(source_path=document.source, encoding=document.encoding,
                        passthrough=tuple(raw))


def _rebase_store(model, directory, path_policy, context):
    """Copy only records with declared file references that actually change."""
    store = model._store
    if directory is None or path_policy == 'preserve':
        return store
    directory = str(Path(directory).resolve())
    updates = {}
    # Built-in file consumers cover the built-in codec graph. Do not invoke the
    # schema wrapper: it validates consumers and walks every object again.
    for descriptor, codec in checkpointed(model._schema.bindings):
        hook = getattr(codec, 'file_uses', None)
        if model.profile.key not in descriptor.profiles or hook is None:
            continue
        for use in checkpointed(hook(store, model.profile)):
            file = use.file
            if use.base == 'working_directory' and file.base_directory is None:
                # RDII paths are based on the execution working directory,
                # independent of where the model INP itself is exported.
                if path_policy == 'relative':
                    continue
                if not file.path_type(file.path).is_absolute():
                    raise ValueError('Absolute export requires a resolved working-directory file reference')
            if file.base_directory is None and use.base == 'document' and context is not None and context.source_path:
                file = replace(file, base_directory=str(Path(context.source_path).parent))
            text = file.for_directory(directory, policy=path_policy)
            if text == use.file.path:
                continue
            replacement = replace(file, path=text, base_directory=directory)
            namespace = use.owner.collection
            key = canonical_key(use.owner.key)
            identity = namespace, key
            current = updates.get(identity, store._records[namespace][key])
            updates[identity] = replace_path(current, use.path, replacement)
    if not updates:
        return store
    result = copy(store)
    result._records = dict(store._records)
    for namespace in {namespace for namespace, key in updates}:
        result._records[namespace] = dict(store._records[namespace])
    for (namespace, key), value in updates.items():
        result._records[namespace][key] = value
    result._reference_index = None
    return result


def _encoded(model, store):
    available, writable, bindings = set(), set(), []
    for descriptor, codec in checkpointed(model._schema.bindings):
        if (model.profile.key not in descriptor.profiles
                or any(dependency not in available for dependency in descriptor.requires)):
            continue
        available.add(descriptor.key)
        writable.update(spec.key for spec in codec.collections)
        bindings.append((descriptor, codec))
    # This checks serializer availability, not record validity. A trusted model
    # must not silently lose a populated collection under a different profile.
    for key, records in store._records.items():
        if records and key not in writable:
            raise RegistryError(f"Model collection has no active INP writer: {key}")
    for descriptor, codec in bindings:
        rows = codec.encode(store, model.profile)
        if isinstance(rows, FeatureEncoding):
            rows = rows.rows
        for row in checkpointed(rows):
            yield (descriptor.key, row.key), row


def _merge_passthrough(encoded, passthrough):
    """Insert raw parameters beside surviving structured physical anchors."""
    encoded = list(encoded)
    indexes = {key: index for index, (key, row) in enumerate(encoded)}
    buckets = {}
    suffix = []
    for raw in checkpointed(passthrough):
        if raw.before in indexes:
            index = indexes[raw.before]
        elif raw.after in indexes:
            index = indexes[raw.after] + 1
        else:
            suffix.append(raw)
            continue
        buckets.setdefault(index, []).append(raw)
    for index, (key, row) in enumerate(encoded):
        for raw in buckets.get(index, ()):
            yield raw.section, raw.content
        yield row.section, _line(row)
    for raw in buckets.get(len(encoded), ()):
        yield raw.section, raw.content
    for raw in suffix:
        yield raw.section, raw.content


def render_text(model, *, directory=None, path_policy='relative', normalize=False):
    """Render effective parameters; never validate or rebuild an input document."""
    if path_policy not in ('relative', 'absolute', 'preserve'):
        raise ValueError("Path policy must be relative, absolute or preserve")
    with trusted_scope():
        context = _context(model)
        if (directory is not None and path_policy != 'preserve' and context is not None
                and context.passthrough and context.source_path
                and Path(directory).resolve() != Path(context.source_path).parent.resolve()):
            raise NotImplementedError('Cannot rebase external-file references in unsupported raw INP content; export in the original directory or use path_policy=preserve')
        store = _rebase_store(model, directory, path_policy, context)
        rows = _encoded(model, store)
        passthrough = () if context is None else context.passthrough
        if passthrough:
            lines = _merge_passthrough(rows, passthrough)
        else:
            lines = ((row.section, _line(row))
                     for key, row in rows)
        result = []
        previous = None
        for section, content in checkpointed(lines):
            if section != previous:
                if section is not None:
                    result.append('[' + section + ']\n')
                previous = section
            if content is not None:
                result.append(content + '\n')
        following = dict(model.profile.section_terminators).get(previous)
        if following:
            result.append('[' + following + ']\n')
        return ''.join(result)


def write_model(model, path, *, encoding=None, path_policy='relative', normalize=False):
    """Write directly from business values without a validation or parse pass."""
    path = Path(path).resolve()
    context = _context(model)
    codec = encoding or (context.encoding if context is not None else 'utf-8')
    text = render_text(model, directory=path.parent, path_policy=path_policy, normalize=normalize)
    write_bytes(path, text.encode(codec))
