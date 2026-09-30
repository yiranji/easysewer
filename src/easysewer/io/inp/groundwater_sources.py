"""Groundwater assignments and original arithmetic token provenance."""
from dataclasses import fields, is_dataclass


def groundwater_sources(source, owner, record, entries, section, aquifer_fields, binding_fields, optional_fields):
    def cover(value, path=()):
        if is_dataclass(value):
            for f in fields(value):
                p = path + (f.name,)
                source.cover(owner, p)
                cover(getattr(value, f.name), p)
    cover(record)
    for index, (line, row, spans) in enumerate(entries):
        active = index == len(entries) - 1
        def bind(path, cols, role='value', contributes=active):
            source.add(owner, path, line, cols, role=role, contributes=contributes, overwrite=False)
        def ref(name, column):
            bind((name,), (column,)); bind((name, 'key'), (column,))
            bind((name, 'collection'), (column,), 'derived')
        v = line.values
        if section == 'AQUIFERS':
            bind(('id',), (0,))
            for column, name in enumerate(aquifer_fields, 1):
                bind((name,), (column,))
            if len(v) > 13:
                ref('evaporation_pattern', 13)
            limit = 14
        elif section == 'GROUNDWATER':
            for column, name in enumerate(('subcatchment', 'aquifer', 'node')):
                ref(name, column)
            for column, name in enumerate(binding_fields, 3):
                bind((name,), (column,))
            for column, name in enumerate(optional_fields, 10):
                if column < len(v):
                    bind((name,), (column,), 'marker' if getattr(row, name) is None else 'value')
            limit = 14
        else:
            ref('subcatchment', 0); bind(('kind',), (1,))
            # Character positions refer to decoded values joined by one space,
            # exactly as parsed. Physical INP tokens can contain many AST leaves.
            intervals, start = [], 0
            for column, value in enumerate(v[2:], 2):
                intervals.append((column, start, start + len(value)))
                start += len(value) + 1
            for path, (start, end) in spans.items():
                columns = tuple(column for column, left, right in intervals if left < end and right > start)
                bind(('expression',) + path, columns, 'derived')
            limit = len(v)
        if len(v) > limit:
            bind(('id',) if section == 'AQUIFERS' else ('subcatchment',), range(limit, len(v)), 'retained', False)
