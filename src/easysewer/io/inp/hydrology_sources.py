"""Exact rain-gage/catchment source grammar, including replaced optional fields."""

from dataclasses import fields, is_dataclass

from ...model import hydrology as h, climate as c


def hydrology_sources(source, owner, record, lines, options, profile):
    from .hydrology import parse_infiltration
    if type(record) not in (h.RainGage, h.Subcatchment, h.Snowpack, c.SubcatchmentAdjustments):
        return

    def cover(value, path=()):
        if is_dataclass(value):
            for f in fields(value):
                child = path + (f.name,)
                source.cover(owner, child)
                cover(getattr(value, f.name), child)
        elif isinstance(value, tuple):
            for i, item in enumerate(value):
                source.cover(owner, path + (i,))
                cover(item, path + (i,))
    cover(record)

    def bind(path, line, tokens, *, role='value', contributes=True, overwrite=True):
        source.add(owner, path, line, tokens, role=role, contributes=contributes, overwrite=overwrite)

    def ref(path, line, token, *, contributes=True):
        bind(path, line, (token,), contributes=contributes)
        bind(path + ('key',), line, (token,), contributes=contributes)
        bind(path + ('collection',), line, (token,), role='derived', contributes=contributes)

    if type(record) is h.Snowpack:
        names = {'PLOWABLE': 'plowable', 'IMPERVIOUS': 'impervious', 'PERVIOUS': 'pervious', 'REMOVAL': 'removal'}
        last = {line.values[1].upper(): line.number for line in lines}
        for i, line in enumerate(lines):
            tokens = line.values
            kind = tokens[1].upper()
            group = names[kind]
            current = line.number == last[kind]
            bind(('id',), line, (0,), role='value' if i == 0 else 'retained', contributes=i == 0)
            bind((group,), line, range(1, len(tokens)), contributes=current)
            names_in_row = (('threshold', 'out_of_system', 'to_impervious', 'to_pervious', 'immediate_melt', 'to_subcatchment')
                if kind == 'REMOVAL' else ('minimum_melt', 'maximum_melt', 'base_temperature', 'free_water_fraction',
                                           'initial_snow', 'initial_free_water', 'fraction' if kind == 'PLOWABLE' else 'full_cover_depth'))
            for col, name in enumerate(names_in_row, 2):
                if col < len(tokens):
                    bind((group, name), line, (col,), contributes=current)
            if kind == 'REMOVAL' and len(tokens) == 9:
                ref((group, 'destination'), line, 8, contributes=current)
        return
    if type(record) is c.SubcatchmentAdjustments:
        names = {'INFIL': 'infiltration', 'DSTORE': 'depression_storage', 'N-PERV': 'pervious_roughness'}
        last = {line.values[0].upper(): line.number for line in lines}
        for i, line in enumerate(lines):
            kind = line.values[0].upper()
            if i == 0:
                ref(('subcatchment',), line, 1)
            else:
                bind(('subcatchment',), line, (1,), role='retained', contributes=False)
                bind(('subcatchment', 'key'), line, (1,), role='retained', contributes=False)
                bind(('subcatchment', 'collection'), line, (1,), role='derived', contributes=False)
            ref((names[kind],), line, 2, contributes=line.number == last[kind])
        return

    last = {line.section: line.number for line in lines}
    primary = 'RAINGAGES' if type(record) is h.RainGage else 'SUBCATCHMENTS'
    vertex = 0
    for line in lines:
        tokens, section = line.values, line.section
        current = line.number == last[section]
        bind(('id',), line, (0,), role='value' if section == primary else 'retained', contributes=section == primary)
        if section == 'RAINGAGES':
            for name, col in (('form', 1), ('interval', 2), ('snow_factor', 3)):
                bind((name,), line, (col,))
            bind(('source',), line, range(4, len(tokens)))
            if type(record.source) is h.SeriesRainfall:
                ref(('source', 'series'), line, 5)
            else:
                bind(('source', 'file'), line, (4, 5))
                bind(('source', 'file', 'path'), line, (5,))
                for name in ('base_directory', 'flavor'):
                    bind(('source', 'file', name), line, (5,), role='derived')
                bind(('source', 'file', 'direction'), line, (4,), role='derived')
                bind(('source', 'station'), line, (6,))
                bind(('source', 'units'), line, (7,))
                if len(tokens) > 8:
                    bind(('source', 'start_date'), line, (8,), role='marker' if tokens[8] == '*' else 'value')
        elif section == 'SUBCATCHMENTS':
            ref(('rain_gage',), line, 1)
            ref(('outlet',), line, 2)
            for col, name in enumerate(('area', 'impervious_percent', 'width', 'slope', 'curb_length'), 3):
                bind((name,), line, (col,))
            if len(tokens) > 8:
                ref(('snowpack',), line, 8)
        elif section == 'SUBAREAS':
            bind(('subareas',), line, range(1, len(tokens)), contributes=current)
            for col, name in enumerate(('impervious_roughness', 'pervious_roughness', 'impervious_storage', 'pervious_storage',
                                         'zero_storage_percent', 'route_to', 'routed_percent'), 1):
                if col < len(tokens):
                    bind(('subareas', name), line, (col,), contributes=current)
        elif section == 'INFILTRATION':
            value, _ = parse_infiltration(tokens, options, profile)
            end = len(tokens) - (value.method is not None)
            numeric = (1, 3) if type(value.parameters) is h.CurveNumber else tuple(range(1, end))
            bind(('infiltration',), line, (*numeric, *((end,) if value.method is not None else ())), contributes=current)
            bind(('infiltration', 'parameters'), line, numeric, contributes=current)
            if value.method is not None:
                bind(('infiltration', 'method'), line, (end,), contributes=current)
            if type(value.parameters) is h.CurveNumber:
                pairs = (('curve_number', 1), ('drying_time', 3))
                bind(('infiltration', 'parameters'), line, (2,), role='retained', contributes=False)
            else:
                pairs = tuple((f.name, i) for i, f in enumerate(fields(value.parameters), 1) if i < end)
            for name, col in pairs:
                bind(('infiltration', 'parameters', name), line, (col,), contributes=current)
        elif section in ('SYMBOLS', 'POLYGONS'):
            path = ('position',) if section == 'SYMBOLS' else ('polygon', vertex)
            contributes = current if section == 'SYMBOLS' else True
            if section == 'POLYGONS':
                bind(('polygon',), line, (1, 2), overwrite=False)
                vertex += 1
            bind(path, line, (1, 2), contributes=contributes)
            bind(path + ('x',), line, (1,), contributes=contributes)
            bind(path + ('y',), line, (2,), contributes=contributes)
            if len(tokens) > 3:
                bind(path, line, range(3, len(tokens)), role='retained', contributes=False)
