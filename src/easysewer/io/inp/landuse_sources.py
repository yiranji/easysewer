"""Quality relation sources, including repeated pairs on a single line."""
from dataclasses import fields, is_dataclass

from ...model import quality as q
from ...model.identity import Ref, canonical_key


def landuse_sources(source, section, records, lines):
    namespace = {'LANDUSES': 'landuses', 'COVERAGES': 'coverages', 'LOADINGS': 'loadings',
                 'BUILDUP': 'buildup', 'WASHOFF': 'washoff'}[section]
    owners = {}
    def cover(owner, value, path=()):
        if is_dataclass(value):
            for f in fields(value):
                p = path + (f.name,); source.cover(owner, p); cover(owner, getattr(value, f.name), p)
    for row in records:
        key = row.id if type(row) is q.LandUse else q.relation_key(row)
        owner = Ref(collection='swmm:' + namespace, key=key)
        owners[canonical_key(key)] = owner; cover(owner, row)
    # Enumerate each assignment once. The last pair, not just the last row,
    # contributes to a repeated COVERAGES/LOADINGS composite identity.
    assignments = []
    for line in lines:
        v = line.values
        if section in ('COVERAGES', 'LOADINGS'):
            assignments.extend((canonical_key((v[0], v[col])), line, col) for col in range(1, len(v), 2))
        else:
            assignments.append((canonical_key(v[0] if section == 'LANDUSES' else tuple(v[:2])), line, None))
    last = {key: index for index, (key, line, col) in enumerate(assignments)}
    last_on_line = {(key, line.number): index for index, (key, line, col) in enumerate(assignments)}
    for index, (key, line, col) in enumerate(assignments):
        owner = owners[key]; v = line.values; current = index == last[key]
        shared = section in ('COVERAGES', 'LOADINGS') and key != canonical_key(tuple(v[:2]))
        def bind(path, cols, role='value', active=True):
            # The physical row is owned by its first pair. Other pairs are
            # derived records under the existing cross-owner source contract.
            if shared: role = 'derived'
            source.add(owner, path, line, cols, role=role, contributes=current and active, overwrite=False)
        def ref(name, column):
            bind((name,), (column,)); bind((name, 'key'), (column,))
            bind((name, 'collection'), (column,), 'derived')
        if section == 'LANDUSES':
            for column, name in enumerate(('id', 'sweep_interval', 'sweep_availability', 'days_since_sweeping')):
                if column < len(v): bind((name,), (column,))
            if len(v) > 4: bind(('id',), range(4, len(v)), 'retained', False)
        elif section in ('COVERAGES', 'LOADINGS'):
            if index == last_on_line[key, line.number]: ref('subcatchment', 0)
            ref('landuse' if section == 'COVERAGES' else 'pollutant', col)
            bind(('percent' if section == 'COVERAGES' else 'mass_per_area',), (col + 1,))
        else:
            ref('landuse', 0); ref('pollutant', 1)
            choices = ('NONE', 'POW', 'EXP', 'SAT', 'EXT') if section == 'BUILDUP' else ('NONE', 'EXP', 'RC', 'EMC')
            kind = next(k for k in choices if v[2].upper().startswith(k))
            end = 3 if kind == 'NONE' else 6 if section == 'BUILDUP' else 5
            bind(('function',), range(2, end), 'derived')
            if kind == 'NONE':
                if len(v) > 3: bind(('function',), range(3, len(v)), 'retained', False)
                continue
            if section == 'BUILDUP':
                names = {'POW': ('maximum', 'coefficient', 'exponent'),
                         'EXP': ('maximum', 'rate', 'unused_parameter'),
                         'SAT': ('maximum', 'unused_parameter', 'half_saturation_days'),
                         'EXT': ('maximum', 'scale_factor', 'series')}[kind]
                bind(('normalizer',), (6,))
            else:
                names = ('concentration', 'unused_exponent') if kind == 'EMC' else ('coefficient', 'exponent')
                for column, name in ((5, 'sweeping_removal'), (6, 'bmp_removal')):
                    if column < len(v): bind((name,), (column,))
            for column, name in enumerate(names, 3):
                bind(('function', name), (column,))
                if name == 'series':
                    bind(('function', name, 'key'), (column,))
                    bind(('function', name, 'collection'), (column,), 'derived')
            if len(v) > 7: bind(('function',), range(7, len(v)), 'retained', False)
