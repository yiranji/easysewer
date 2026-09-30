"""Exact clause positions and traced arithmetic spans in control programs."""
from dataclasses import fields, is_dataclass

from ...model import controls as c
from ...model.identity import Ref
from ...model.inspection import field_at
from ...model.options import MonthDay


def control_sources(source, row, lines, spans):
    owner = Ref(collection='swmm:controls', key=c.statement_key(row))
    def cover(value, path=()):
        if is_dataclass(value):
            for f in fields(value):
                p = path + (f.name,); source.cover(owner, p); cover(getattr(value, f.name), p)
        elif isinstance(value, tuple):
            for i, child in enumerate(value):
                p = path + (i,); source.cover(owner, p); cover(child, p)
    cover(row)
    counts = dict(conditions=0, then_actions=0, else_actions=0)
    branch = None
    for line in lines:
        v = line.values; keyword = v[0].upper()
        def bind(path, cols, role='value'):
            source.add(owner, path, line, cols, role=role, overwrite=False)
        def ref(path, column, *, named=False):
            role = 'derived' if named else 'value'
            bind(path, (column,), role); bind(path + ('key',), (column,), role)
            bind(path + ('collection',), (column,), 'derived')
            if named:
                bind(path + ('key', 0), (column,), 'derived')
                bind(path + ('key', 1), (column,), 'derived')
        def attribute(value, path, column):
            count = 2 if value.object_type == 'SIMULATION' else 3
            bind(path, range(column, column + count), 'derived')
            bind(path + ('object_type',), (column,))
            bind(path + ('attribute',), (column + count - 1,))
            if value.target is not None: ref(path + ('target',), column + 1)
            if value.history_hours is not None: bind(path + ('history_hours',), (column + count - 1,), 'derived')
            return count
        def operand(value, path, column):
            if type(value) is c.Attribute: return attribute(value, path, column)
            bind(path, (column,), 'derived')
            if type(value) is c.NamedOperand: ref(path + ('reference',), column, named=True)
            else:
                bind(path + ('value',), (column,))
                if type(value.value) is MonthDay:
                    for name in ('month', 'day'): bind(path + ('value', name), (column,), 'derived')
            return 1
        if keyword in ('VARIABLE', 'EXPRESSION', 'RULE'):
            bind(('id',), (1,))
            if keyword == 'VARIABLE': attribute(row.value, ('value',), 3)
            elif keyword == 'EXPRESSION':
                if spans is None:
                    source.block(owner, ('expression',))
                    continue
                intervals, start = [], 0
                for col, token in enumerate(v[3:], 3):
                    intervals.append((col, start, start + len(token))); start += len(token) + 1
                for path, (start, end) in spans.items():
                    cols = tuple(col for col, left, right in intervals if left < end and right > start)
                    bind(('expression',) + path, cols, 'derived')
                    value = row.expression if not path else field_at(row.expression, path)[1]
                    if isinstance(value, tuple):
                        for i in range(len(value)): bind(('expression',) + path + (i,), cols, 'derived')
        elif keyword == 'PRIORITY': bind(('priority',), (1,))
        else:
            if keyword == 'IF': branch = 'conditions'
            elif keyword == 'THEN': branch = 'then_actions'
            elif keyword == 'ELSE': branch = 'else_actions'
            index = counts[branch]; counts[branch] += 1
            prefix = (branch, index); value = getattr(row, branch)[index]
            bind((branch,), range(len(v)), 'derived'); bind(prefix, range(len(v)), 'derived')
            if branch == 'conditions':
                bind(prefix + ('conjunction',), (0,))
                count = operand(value.left, prefix + ('left',), 1)
                bind(prefix + ('relation',), (1 + count,))
                operand(value.right, prefix + ('right',), 2 + count)
            else:
                bind(prefix + ('object_type',), (1,)); ref(prefix + ('target',), 2)
                path = prefix + ('setting',); setting = value.setting
                bind(path, range(3, len(v)), 'derived')
                if type(setting) in (c.NumericSetting, c.StatusSetting): bind(path + ('value',), (5,))
                elif type(setting) is c.CurveSetting: ref(path + ('curve',), 6)
                elif type(setting) is c.SeriesSetting: ref(path + ('series',), 6)
                else:
                    for col, name in enumerate(('gain', 'integral_time', 'derivative_time'), 6): bind(path + (name,), (col,))
