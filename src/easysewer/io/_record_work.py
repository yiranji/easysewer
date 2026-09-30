"""Cooperative construction of the existing pretty-printed record format.

Checks bound built-in container batches, not a single scalar allocation,
system call or arbitrary extension operation. Inactive callers retain the
standard-library path. These helpers do not define a new JSON wire format.
"""

from copy import deepcopy
from dataclasses import asdict as standard_asdict, dataclass, fields, is_dataclass
import json

from ..validation._cooperative import _active, checkpoint, checkpointed
import hashlib
from pathlib import Path


@dataclass
class _Fallback:
    value: object


def record_asdict(value):
    if _active.get() is None:
        return standard_asdict(value)
    if not is_dataclass(value) or isinstance(value, type):
        raise TypeError('asdict() should be called on dataclass instances')
    count = 0

    def checked_dict(items):
        checkpoint()
        return dict(items)

    def walk(item):
        nonlocal count
        kind = type(item)
        if kind in (str, int, float, bool, bytes, type(None)):
            return item
        count += 1
        if count % 256 == 0:
            checkpoint()
        if is_dataclass(item) and not isinstance(item, type):
            return {field.name: walk(getattr(item, field.name)) for field in fields(item)}
        if kind is tuple:
            return tuple(walk(v) for v in checkpointed(item))
        if kind is list:
            return [walk(v) for v in checkpointed(item)]
        if kind is dict:
            return {walk(k): walk(v) for k, v in checkpointed(item.items())}
        checkpoint()
        # Container subclasses have interpreter-specific constructor semantics.
        # Preserve standard asdict conversion of dataclasses nested inside them,
        # including errors, rather than treating the whole container as a leaf.
        result = (standard_asdict(_Fallback(item), dict_factory=checked_dict)['value']
                  if isinstance(item, (tuple, list, dict)) else deepcopy(item))
        checkpoint()
        return result

    checkpoint()
    result = walk(value)
    checkpoint()
    return result


def record_dumps(value):
    options = dict(ensure_ascii=False, allow_nan=False, indent=2)
    if _active.get() is None:
        return json.dumps(value, **options)

    def bounded(item):
        budget = 512
        pending = [item]
        while pending:
            child = pending.pop()
            if isinstance(child, (dict, list, tuple)):
                budget -= len(child)
                if budget < 0:
                    return False
                pending.extend(child.values() if isinstance(child, dict) else child)
        return True

    ancestors = set()

    def render(item, level):
        if bounded(item):
            # Retain the C encoder on runtimes that support it with indent=2.
            # Actual newlines delimit JSON syntax; string newlines are escaped.
            yield json.dumps(item, **options).replace('\n', '\n' + '  ' * level)
            return
        identity = id(item)
        if identity in ancestors:
            raise ValueError('Circular reference detected')
        ancestors.add(identity)
        try:
            if type(item) in (list, tuple):
                # Encode bounded groups together, retaining standard pretty JSON
                # indentation and avoiding one encoder/checkpoint call per atom.
                yield '[\n'
                first = True
                for start in range(0, len(item), 128):
                    checkpoint()
                    group = item[start:start + 128]
                    if bounded(group):
                        encoded = json.dumps(group, **options)
                        content = encoded[2:-2].replace('\n', '\n' + '  ' * level)
                        yield ('' if first else ',\n') + '  ' * level + content
                        first = False
                    else:
                        for child in group:
                            yield ('' if first else ',\n') + '  ' * (level + 1)
                            first = False
                            yield from render(child, level + 1)
                yield '\n' + '  ' * level + ']'
                return
            mapping = isinstance(item, dict)
            yield '{\n' if mapping else '[\n'
            first = True
            for key, child in (item.items() if mapping else enumerate(item)):
                yield ('' if first else ',\n') + '  ' * (level + 1)
                first = False
                if mapping:
                    encoded = json.dumps({key: None}, ensure_ascii=False, allow_nan=False)
                    yield encoded[1:-7] + ': '
                yield from render(child, level + 1)
            yield '\n' + '  ' * level + ('}' if mapping else ']')
        finally:
            ancestors.remove(identity)

    chunks = []
    checkpoint()
    for piece in render(value, 0):
        checkpoint()
        chunks.append(piece)
    result = ''.join(chunks)
    checkpoint()
    return result


def write_record_bytes(path, data, *, description='execution record'):
    """Write a private, unpublished artifact; its owner handles failure cleanup."""
    checkpoint()
    with path.open('wb') as stream:
        view = memoryview(data)
        for offset in range(0, len(view), 65536):
            checkpoint()
            chunk = view[offset:offset + 65536]
            if stream.write(chunk) != len(chunk):
                raise OSError(f'Short write while saving {description}')
        checkpoint()
    checkpoint()


def read_record_bytes(path):
    """Read unpublished input/result bytes; preserve the complete-bytes API."""
    if _active.get() is None:
        return Path(path).read_bytes()
    chunks = []
    checkpoint()
    with Path(path).open('rb') as stream:
        while True:
            checkpoint()
            chunk = stream.read(65536)
            if not chunk:
                break
            chunks.append(chunk)
    checkpoint()
    data = b''.join(chunks)
    checkpoint()
    return data


def record_digest(data):
    """Hash immutable captured bytes in bounded batches in an active scope."""
    if _active.get() is None:
        return hashlib.sha256(data).hexdigest()
    state = hashlib.sha256()
    view = memoryview(data)
    checkpoint()
    for start in range(0, len(view), 65536):
        checkpoint()
        state.update(view[start:start+65536])
    checkpoint()
    return state.hexdigest()
