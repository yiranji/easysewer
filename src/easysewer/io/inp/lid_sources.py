"""Stateful layer assignments, ordered usages and removal-pair sources."""
from dataclasses import fields, is_dataclass

from ...model import lid as l
from ...model.identity import Ref, canonical_key


def cover(source, owner, value, path=()):
    if is_dataclass(value):
        for f in fields(value):
            p = path + (f.name,); source.cover(owner, p)
            cover(source, owner, getattr(value, f.name), p)
    elif isinstance(value, tuple):
        for index, child in enumerate(value):
            p = path + (index,); source.cover(owner, p)
            cover(source, owner, child, p)


def control_sources(source, row, lines, keyword, required, optional):
    owner = Ref(collection='swmm:lid_controls', key=row.id)
    cover(source, owner, row)
    entries, last, removal_last = [], {}, {}
    state = None
    for index, line in enumerate(lines):
        v = line.values; kind = keyword(v[1])
        ignored = kind == 'DRAINMAT' and state != 'GR'
        if kind in l.LID_KINDS:
            state = kind; last['kind'] = index
        elif kind == 'REMOVALS':
            for col in range(2, len(v), 2): removal_last[canonical_key(v[col])] = index, col
        elif not ignored:
            last[kind] = index
        entries.append((line, kind, ignored))
    removal_indexes = {value.pollutant.canonical.key: index for index, value in enumerate(row.removals)}
    for index, (line, kind, ignored) in enumerate(entries):
        v = line.values
        def bind(path, cols, role='value', active=True):
            source.add(owner, path, line, cols, role=role, contributes=active, overwrite=False)
        def ref(path, column, active):
            bind(path, (column,), active=active); bind(path + ('key',), (column,), active=active)
            bind(path + ('collection',), (column,), 'derived', active)
        bind(('id',), (0,), active=index == 0)
        if ignored:
            bind(('drain_mat',), range(1, len(v)), 'retained', False)
            continue
        if kind in l.LID_KINDS:
            bind(('kind',), (1,), active=index == last['kind'])
            limit = 2; extra_path = ('kind',)
        elif kind == 'REMOVALS':
            any_active = any(removal_last[canonical_key(v[c])] == (index, c) for c in range(2, len(v), 2))
            bind(('removals',), range(1, len(v)), 'derived', any_active)
            for col in range(2, len(v), 2):
                key = canonical_key(v[col]); prefix = ('removals', removal_indexes[key])
                active = removal_last[key] == (index, col)
                bind(prefix, (col, col + 1), 'derived', active)
                ref(prefix + ('pollutant',), col, active)
                bind(prefix + ('percent',), (col + 1,), active=active)
            continue
        else:
            prefix = (l.LAYERS[kind][0],); active = index == last[kind]
            names = (*required[kind], *optional.get(kind, ()))
            if kind == 'STORAGE': names += ('covered',)
            if kind == 'DRAIN': names += ('curve',)
            limit = 2 + len(names); extra_path = prefix
            bind(prefix, range(1, min(len(v), limit)), 'derived', active)
            for col, name in enumerate(names, 2):
                if col >= len(v): break
                if name == 'curve': ref(prefix + (name,), col, active)
                else: bind(prefix + (name,), (col,), active=active)
        if len(v) > limit: bind(extra_path, range(limit, len(v)), 'retained', False)


def usage_sources(source, row, line):
    owner = Ref(collection='swmm:lid_usage', key=row.record_id)
    cover(source, owner, row)
    v = line.values
    def bind(path, cols, role='value', active=True):
        source.add(owner, path, line, cols, role=role, contributes=active, overwrite=False)
    def ref(name, col):
        bind((name,), (col,)); bind((name, 'key'), (col,))
        bind((name, 'collection'), (col,), 'derived')
    bind(('record_id',), range(min(3, len(v))), 'derived')
    ref('subcatchment', 0); ref('control', 1)
    if type(row) is l.DisabledLidUsage:
        bind(('parameters',), range(3, len(v)), 'retained', False)
        for index in range(len(row.parameters)):
            bind(('parameters', index), (index + 3,), 'retained', False)
        return
    for col, name in enumerate(('number', 'area', 'width', 'initial_saturation', 'from_impervious', 'to_pervious'), 2):
        bind((name,), (col,))
    if len(v) > 8:
        if row.report_file is None:
            bind(('report_file',), (8,), 'marker')
        else:
            bind(('report_file',), (8,)); bind(('report_file', 'path'), (8,))
            for name in ('base_directory', 'flavor', 'direction'):
                bind(('report_file', name), (8,), 'derived')
    if len(v) > 9:
        if row.drain_to is None: bind(('drain_to',), (9,), 'marker')
        else: ref('drain_to', 9)
    if len(v) > 10: bind(('from_pervious',), (10,))
    if len(v) > 11: bind(('record_id',), range(11, len(v)), 'retained', False)
