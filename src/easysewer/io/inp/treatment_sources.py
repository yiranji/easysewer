"""Original treatment assignments and shared-parser expression token positions."""
from dataclasses import fields, is_dataclass

from ...model.identity import Ref


def treatment_sources(source, record, entries):
    owner = Ref(collection='swmm:treatment', key=(record.node.key, record.pollutant.key))
    def cover(value, path=()):
        if is_dataclass(value):
            for f in fields(value):
                p = path + (f.name,); source.cover(owner, p); cover(getattr(value, f.name), p)
    cover(record)
    for index, (line, row, head, spans) in enumerate(entries):
        active = index == len(entries) - 1
        def bind(path, cols, role='value'):
            source.add(owner, path, line, cols, role=role, contributes=active, overwrite=False)
        for col, name in enumerate(('node', 'pollutant')):
            bind((name,), (col,)); bind((name, 'key'), (col,)); bind((name, 'collection'), (col,), 'derived')
        bind(('kind',), (2,), 'derived')
        if spans is None:
            source.block(owner, ('expression',))
            continue
        # _parts splits the joined decoded tail at its first '='. Traced
        # expression positions include its leading whitespace after that sign.
        offset = len(head) + 1
        intervals, start = [], 0
        for col, token in enumerate(line.values[2:], 2):
            intervals.append((col, start, start + len(token))); start += len(token) + 1
        for path, (left, right) in spans.items():
            cols = tuple(col for col, a, b in intervals if a < right + offset and b > left + offset)
            bind(('expression',) + path, cols, 'derived')
