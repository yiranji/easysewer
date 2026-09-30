"""Climate assignment history, including the independent file and source modes."""

from dataclasses import fields, is_dataclass

from ...model import climate as c


def climate_sources(source, owner, record, lines):
    from .climate import _adjust_keyword, _ADJUST_FIELDS

    def cover(value, path=()):
        if is_dataclass(value):
            for f in fields(value):
                p = path + (f.name,)
                source.cover(owner, p)
                cover(getattr(value, f.name), p)
        elif isinstance(value, tuple):
            for i, item in enumerate(value):
                source.cover(owner, path + (i,))
                cover(item, path + (i,))
    cover(record)

    def groups(line):
        k = line.values[0].upper()
        if line.section == 'TEMPERATURE':
            if k == 'FILE':
                return (('file',), ('temperature',))
            return (({'TIMESERIES': 'temperature', 'WINDSPEED': 'wind', 'SNOWMELT': 'snowmelt'}.get(k)
                     or ('impervious_depletion' if line.values[1].upper().startswith('IMPERV') else 'pervious_depletion'),),)
        if line.section == 'EVAPORATION':
            return (('evaporation', {'RECOVERY': 'recovery_pattern', 'DRY_ONLY': 'dry_only'}.get(k, 'source')),)
        return (('adjustments', _ADJUST_FIELDS[_adjust_keyword(k)][0]),)

    last = {p: line.number for line in lines for p in groups(line)}
    for line in lines:
        k, tokens = line.values[0].upper(), line.values
        for path in groups(line):
            current = line.number == last[path]

            def bind(p, cols, role='value'):
                source.add(owner, p, line, cols, role=role, contributes=current, overwrite=False)

            def ref(p, col):
                bind(p, (col,)); bind(p + ('key',), (col,)); bind(p + ('collection',), (col,), 'derived')

            def monthly(p, first, field='values'):
                bind(p + (field,), range(first, len(tokens)))
                for i in range(first, len(tokens)):
                    bind(p + (field, i-first), (i,))

            bind(path, range(len(tokens)))
            if len(path) == 2:
                bind(path[:1], range(len(tokens)))
            if path == ('temperature',):
                if k == 'TIMESERIES':
                    ref(path + ('series',), 1)
            elif path == ('file',):
                bind(path + ('file',), (0, 1)); bind(path + ('file', 'path'), (1,))
                for name in ('base_directory', 'flavor'):
                    bind(path + ('file', name), (1,), 'derived')
                bind(path + ('file', 'direction'), (0,), 'derived')
                if len(tokens) > 2:
                    bind(path + ('start_date',), (2,), 'marker' if tokens[2] == '*' else 'value')
                if len(tokens) > 3:
                    bind(path + ('units',), (3,))
            elif path == ('wind',):
                if tokens[1].upper() == 'MONTHLY':
                    monthly(path, 2)
            elif path == ('snowmelt',):
                for col, f in enumerate(fields(c.Snowmelt), 1):
                    bind(path + (f.name,), (col,))
            elif path[0] in ('impervious_depletion', 'pervious_depletion'):
                monthly(path, 2, 'fractions')
            elif path[0] == 'adjustments':
                monthly(path, 1)
            elif path[1] == 'recovery_pattern':
                # The aggregate is already bound; add precise reference fields.
                bind(path + ('key',), (1,)); bind(path + ('collection',), (1,), 'derived')
            elif path[1] == 'source':
                if k == 'CONSTANT':
                    bind(path + ('rate',), (1,))
                elif k == 'TIMESERIES':
                    ref(path + ('series',), 1)
                elif k == 'MONTHLY':
                    monthly(path, 1)
                elif k == 'FILE' and len(tokens) > 1:
                    bind(path + ('pan_coefficients',), range(1, len(tokens)))
                    monthly(path + ('pan_coefficients',), 1)
