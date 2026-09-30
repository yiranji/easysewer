"""Explicit pollutant/inflow tokens and overwritten optional tails."""
from dataclasses import fields, is_dataclass

from ...model import inflows as i, quality as q


def inflow_sources(source, owner, record, lines):
    def cover(value, path=()):
        if is_dataclass(value):
            for f in fields(value):
                p = path + (f.name,); source.cover(owner, p); cover(getattr(value, f.name), p)
        elif isinstance(value, tuple):
            for index, item in enumerate(value):
                source.cover(owner, path + (index,)); cover(item, path + (index,))
    cover(record)
    for line in lines:
        values = line.values; current = line is lines[-1]
        def bind(path, cols, role='value', contributes=None):
            source.add(owner, path, line, cols, role=role, contributes=current if contributes is None else contributes, overwrite=False)
        def ref(path, col):
            bind(path, (col,), 'value' if values[col] else 'marker')
            if values[col]:
                bind(path + ('key',), (col,)); bind(path + ('collection',), (col,), 'derived')
        if type(record) is q.Pollutant:
            for col, name in enumerate(('id', 'units', 'rainfall_concentration', 'groundwater_concentration', 'rdii_concentration', 'decay_rate', 'snow_only')):
                if col < len(values): bind((name,), (col,))
            if len(values) >= 9 and values[7] != '*':
                ref(('co_pollutant',), 7); bind(('co_fraction',), (8,))
            elif len(values) > 7:
                marker = len(values) >= 9 and values[7] == '*'
                bind(('co_pollutant',), (7,), 'marker' if marker else 'retained', marker)
                if len(values) > 8: bind(('co_fraction',), (8,), 'retained', False)
            for col, name in ((9, 'dwf_concentration'), (10, 'initial_concentration')):
                if len(values) > col: bind((name,), (col,))
            if len(values) > 11: bind(('id',), range(11, len(values)), 'retained', False)
        else:
            ref(('node',), 0)
            if record.constituent is i.FLOW: bind(('constituent',), (1,))
            else: ref(('constituent',), 1)
            if line.section == 'INFLOWS':
                ref(('series',), 2)
                # Type/factor slots refer to the variant of this historical row,
                # not to a later row that may have switched MASS/CONCEN.
                mass = record.constituent is not i.FLOW and len(values) > 3 and values[3].upper().startswith('MASS')
                if len(values) > 3: bind(('constituent',), (3,), 'derived' if record.constituent is not i.FLOW else 'retained', current and record.constituent is not i.FLOW)
                if len(values) > 4: bind(('mass_factor',) if mass else ('constituent',), (4,), 'value' if mass else 'retained', current if mass else False)
                for col, name in ((5, 'scale_factor'), (6, 'baseline')):
                    if len(values) > col: bind((name,), (col,))
                if len(values) > 7: ref(('pattern',), 7)
                if len(values) > 8: bind(('node',), range(8, len(values)), 'retained', False)
            else:
                bind(('baseline',), (2,))
                if len(values) > 3: bind(('patterns',), range(3, min(7, len(values))))
                for index in range(3, min(7, len(values))): ref(('patterns', index-3), index)
                if len(values) > 7: bind(('node',), range(7, len(values)), 'retained', False)
