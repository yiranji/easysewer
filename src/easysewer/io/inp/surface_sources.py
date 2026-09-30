"""Original street tokens and last-assignment inlet component provenance."""
from dataclasses import fields, is_dataclass

from ...model import surface as s
from ...model.identity import Ref


def cover(source, owner, value, path=()):
    if is_dataclass(value):
        for f in fields(value):
            p = path + (f.name,); source.cover(owner, p)
            cover(source, owner, getattr(value, f.name), p)
    elif isinstance(value, tuple):
        for index, child in enumerate(value):
            p = path + (index,); source.cover(owner, p)
            cover(source, owner, child, p)


def surface_sources(source, row, lines, street_fields, usage_fields):
    namespace = 'streets' if type(row) is s.StreetSection else 'inlets' if type(row) is s.InletDesign else 'inlet_usage'
    owner = Ref(collection='swmm:' + namespace, key=row.link.key if type(row) is s.InletUsage else row.id)
    cover(source, owner, row)
    last = {line.values[1].upper(): i for i, line in enumerate(lines)} if type(row) is s.InletDesign else {}
    for i, line in enumerate(lines):
        v = line.values
        def bind(path, cols, role='value', active=True):
            source.add(owner, path, line, cols, role=role, contributes=active, overwrite=False)
        def ref(path, col, active=True):
            bind(path, (col,), active=active); bind(path + ('key',), (col,), active=active)
            bind(path + ('collection',), (col,), 'derived', active)
        if type(row) is s.StreetSection:
            bind(('id',), (0,))
            for col, name in enumerate(street_fields, 1):
                if col >= len(v): break
                ignored = col >= 9 and not row.backing_width
                bind((name,), (col,), 'retained' if ignored else 'value', not ignored)
        elif type(row) is s.InletUsage:
            active = i == len(lines) - 1
            for col, name in enumerate(('link', 'inlet', 'node')): ref((name,), col, active)
            for col, name in enumerate(usage_fields, 3):
                if col < len(v): bind((name,), (col,), active=active)
        else:
            bind(('id',), (0,), active=i == 0)
            kind = v[1].upper(); active = last[kind] == i
            prefix = ('design',)
            bind(prefix, range(1, len(v)), 'derived', active)
            if type(row.design) is s.CombinationInlet:
                prefix += ('grate' if kind == 'GRATE' else 'curb',)
                bind(prefix, range(1, len(v)), 'derived', active)
            if kind in ('GRATE', 'DROP_GRATE', 'CURB', 'DROP_CURB'):
                bind(prefix + ('kind',), (1,), active=active)
            if kind == 'CUSTOM':
                ref(prefix + ('curve',), 2, active)
                continue
            bind(prefix + ('length',), (2,), active=active)
            bind(prefix + ('height' if kind in ('CURB', 'DROP_CURB') else 'width',), (3,), active=active)
            if kind in ('GRATE', 'DROP_GRATE'):
                grate = prefix + ('grate',)
                bind(grate, range(4, len(v)), 'derived', active)
                if v[4].upper() == 'GENERIC':
                    bind(grate + ('open_fraction',), (5,), active=active)
                    if len(v) > 6: bind(grate + ('splash_velocity',), (6,), active=active)
                else:
                    bind(grate + ('kind',), (4,), active=active)
                    if len(v) > 5: bind(grate, range(5, len(v)), 'retained', False)
            elif kind in ('CURB', 'DROP_CURB') and len(v) > 4:
                bind(prefix + ('throat',), (4,), 'value' if kind == 'CURB' else 'retained', active and kind == 'CURB')
