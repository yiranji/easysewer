"""Ordered hydrograph declarations and legacy three-response source rows."""
from dataclasses import fields, is_dataclass

from ...model.rdii import UnitHydrograph, RESPONSES


def rdii_sources(source, owner, record, lines):
    def cover(value, path=()):
        if is_dataclass(value):
            for f in fields(value):
                p = path + (f.name,); source.cover(owner, p); cover(getattr(value, f.name), p)
        elif isinstance(value, tuple):
            for index, item in enumerate(value):
                p = path + (index,); source.cover(owner, p); cover(item, p)
    cover(record)
    gage_lines = [line for line in lines if len(line.values) == 2] if type(record) is UnitHydrograph else []
    gage_index, response_index = 0, 0
    for line in lines:
        v = line.values
        def bind(path, cols, role='value', active=True):
            source.add(owner, path, line, cols, role=role, contributes=active, overwrite=False)
        def ref(path, column, active=True):
            bind(path, (column,), active=active); bind(path + ('key',), (column,), active=active)
            bind(path + ('collection',), (column,), 'derived', active)
        if type(record) is not UnitHydrograph:
            active = line is lines[-1]
            ref(('node',), 0, active); ref(('hydrograph',), 1, active)
            bind(('sewer_area',), (2,), active=active)
            if len(v) > 3: bind(('node',), range(3, len(v)), 'retained', False)
            continue
        bind(('id',), (0,), active=line is lines[0])
        if len(v) == 2:
            if gage_index == len(gage_lines) - 1:
                ref(('rain_gage',), 1)
            else:
                bind(('prior_rain_gages',), (1,), 'derived')
                ref(('prior_rain_gages', gage_index), 1)
            gage_index += 1
            continue
        kind = next((k for k in RESPONSES if v[2].upper().startswith(k)), None)
        legacy = kind is None
        count = 3 if legacy else 1
        tail_start, limit = (11, 14) if legacy else (6, 9)
        bind(('responses',), range(1, min(limit, len(v))), 'derived')
        for k in range(count):
            prefix = ('responses', response_index)
            start = 2 + 3 * k if legacy else 3
            cols = (1, *range(start, start + 3), *range(tail_start, min(limit, len(v))))
            if not legacy: cols = (1, 2, *cols[1:])
            bind(prefix, cols, 'derived')
            bind(prefix + ('month',), (1,))
            bind(prefix + ('response',), range(start, start + 3) if legacy else (2,), 'derived' if legacy else 'value')
            for column, name in enumerate(('fraction', 'time_to_peak', 'recession_ratio'), start):
                bind(prefix + (name,), (column,))
            for column, name in enumerate(('maximum_abstraction', 'recovery_rate', 'initial_abstraction'), tail_start):
                if column < len(v): bind(prefix + (name,), (column,))
            response_index += 1
        if len(v) > limit: bind(('responses',), range(limit, len(v)), 'retained', False)
