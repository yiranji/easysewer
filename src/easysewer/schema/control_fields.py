"""Configured control syntax and units; never evaluate a live controller."""
from dataclasses import fields, is_dataclass, replace
from datetime import date, timedelta

from ..model import controls as c, network as n, hydrology as h
from ..model.control_rules import (_nodes, expression_dimension, operand_dimension,
                                   resolve_attribute, controller_dimension, validate_controls)
from ..model.fields import validate_fields
from ..model.identity import Ref
from ..model.inspection import FieldFact, FieldSemantics
from ..model.options import MonthDay, get_options
from ..model.resources import Curve, InlineTimeSeries, FileTimeSeries
from ..model.units import UnitContext
from ..validation import Diagnostic, ValidationReport
from .field_contracts import FieldRule
from .hydrology_fields import known, invalid, na, reference
from .resource_fields import dimensions

ROOTS = (c.ControlVariable, c.ControlExpression, c.ControlRule)
ARITHMETIC = (c.ExpressionNumber, c.UnaryExpression, c.BinaryExpression, c.FunctionExpression, c.ExpressionVariable)
SETTINGS = (c.StatusSetting, c.NumericSetting, c.CurveSetting, c.SeriesSetting, c.PIDSetting)
VALUES = (*ROOTS, *ARITHMETIC, *SETTINGS, c.Attribute, c.NamedOperand, c.Constant, c.Condition, c.Action, Ref, MonthDay)
TARGETS = {'swmm:nodes': (n.Junction, n.Outfall, n.Storage, n.Divider),
           'swmm:links': (n.Conduit, n.Pump, n.Orifice, n.Weir, n.Outlet),
           'swmm:raingages': (h.RainGage,), 'swmm:controls': ROOTS,
           'swmm:curves': (Curve,), 'swmm:timeseries': (InlineTimeSeries, FileTimeSeries)}


def control_context(context):
    from ..io.inp.controls import ControlsCodec
    pending, seen = [context.record], set()
    conflicts = tuple(ControlsCodec().symbol_conflicts(tuple(context.store.collection('swmm:controls').values())))
    while pending:
        row = pending.pop()
        key = c.statement_key(row)
        if key in seen: continue
        seen.add(key)
        for node in _nodes(row):
            if is_dataclass(node) and type(node) not in VALUES:
                return FieldFact(reason='Extension control syntax requires its own contract')
            if type(node) is c.FunctionExpression and node.function not in c.FUNCTIONS:
                return FieldFact(reason='Extension arithmetic function requires its own contract')
            if type(node) is Ref:
                fact = reference(context, node)
                if fact.status != 'known': return fact
                target = context.store.collection(node.collection)[node.key]
                if type(target) not in TARGETS.get(node.collection, ()):
                    return FieldFact(reason='Extension control target requires its own contract')
                if node.collection == 'swmm:controls': pending.append(target)
                elif node.collection in ('swmm:curves', 'swmm:timeseries'):
                    if type(target) is Curve and (target.kind != 'CONTROL' or not target.points):
                        return invalid('A control action requires a nonempty CONTROL curve')
                    fact = dimensions(replace(context, owner=node, record=target))
                    if fact.status in ('invalid', 'ambiguous'): return fact
                    # Undetermined controller units do not make the configured
                    # curve/PID syntax unknown. Unit queries expose that limit.
        if not ValidationReport(diagnostics=tuple(validate_fields(row))).is_valid:
            return invalid('Invalid referenced control statement')
        diagnostics = (issue for for_run in (False, True)
                       for issue in validate_controls(context.store, context.profile, for_run=for_run, statements=(row,)))
        if any(issue.severity.value == 'error' for issue in diagnostics):
            return invalid('Control context is rejected by native compatibility validation')
        if any(c.statement_key(other) == key for other, _ in conflicts):
            return invalid('Native prefix lookup shadows a configured control symbol')
    return known(context.record)


def control_field(context):
    root, container, name, value = context.record, context.container, context.field, context.value
    options = get_options(context.store)
    issues = tuple(validate_fields(root)) + tuple(validate_fields(options))
    if not ValidationReport(diagnostics=issues).is_valid:
        return FieldSemantics(effective=invalid('Invalid control statement or options'), diagnostics=issues)
    units = UnitContext(flow_units=options.flow_units or context.profile.option_default('flow_units'))
    record = lambda ref: context.store.collection(ref.collection)[ref.key]
    default = FieldFact(status='required')
    unit = na('Non-numeric control syntax')
    effective = known(value, 'Configured control syntax; no live premise, setting or PID state is evaluated')
    fact = control_context(context)
    if fact.status != 'known':
        return FieldSemantics(unit=unit, default=default, effective=fact,
            diagnostics=(Diagnostic(code='control.field_context', message=fact.reason),) if fact.status in ('invalid', 'ambiguous') else ())

    def dimensional(dimension, reason='Dimension of the represented native value, not a live observation'):
        return known(units.unit(dimension), reason) if dimension else FieldFact(reason='Expression/controller dimensions cannot be determined from these declarations')

    if type(container) is Ref:
        if name == 'collection': default = na('Namespace selected by control grammar')
        if name == 'key' and len(context.path) > 1 and type(context.path[-1]) is int:
            default = na('Composite control symbol identity component')
    elif type(root) is c.ControlRule and container is root:
        if name == 'priority':
            unit = known('1'); default = known(0., 'Omitted priority initializes to zero; equal priority retains rule order')
            effective = known(0. if value is None else value)
        elif name == 'else_actions' and len(context.path) == 1:
            default = known((), 'No ELSE actions when omitted')
        elif name in ('conditions', 'then_actions', 'else_actions') and len(context.path) == 2:
            default = na('Ordered clause has no independent omitted-row default')
    if type(container) is c.Attribute:
        if name == 'attribute': unit = dimensional(c.attribute_dimension(container))
        elif name == 'history_hours':
            unit = known('hour') if container.object_type == 'GAGE' and container.attribute == 'DEPTH' else na('Only accumulated rainfall has a history window')
            if container.history_hours is None: default = effective = na('No history window for this attribute')
        elif name == 'target' and container.object_type == 'SIMULATION':
            default = effective = na('Simulation attributes have no object target')
    elif type(value) is c.Attribute:
        unit = dimensional(c.attribute_dimension(value))
    elif type(container) is c.Constant or type(value) is c.Constant:
        constant = container if type(container) is c.Constant else value
        if isinstance(constant.value, timedelta):
            unit = known('s', 'Model timedelta; INP numeric hours or clock text is converted to native days')
        elif type(constant.value) is date: unit = known('date', 'Model-local calendar date, not a native date serial')
        elif type(constant.value) is MonthDay: unit = known('month/day', 'Native DAYOFYEAR uses a non-leap reference year')
        elif type(constant.value) in (int, float):
            condition = root.conditions[context.path[1]]
            attr = resolve_attribute(condition.left, record)
            unit = dimensional(operand_dimension(condition.left, record))
            if attr and attr.object_type == 'GAGE' and 8 <= (attr.history_hours or 0) <= 20:
                unit = FieldFact(reason='Direct rainfall window collides with native status/time/calendar codes; use a named numeric expression for an unambiguous threshold')
        if type(container) is c.Constant and isinstance(constant.value, timedelta):
            effective = known(value, 'Configured duration; fractional clock components were truncated on import, numeric hours retained precision')
    elif type(container) is MonthDay:
        unit = known('month' if name == 'month' else 'day')
    elif type(container) is c.NamedOperand or type(value) is c.NamedOperand:
        unit = dimensional(operand_dimension(container if type(container) is c.NamedOperand else value, record))
    elif type(container) is c.ExpressionVariable:
        unit = dimensional(expression_dimension(container, record))
    elif type(container) is c.ExpressionNumber:
        unit = known('1', 'Control arithmetic treats numeric literals as dimensionless; incompatible parent expressions remain unknown')
    elif type(value) in ARITHMETIC:
        unit = dimensional(expression_dimension(value, record))
    elif type(container) is c.NumericSetting:
        unit = known('1')
    elif type(container) is c.PIDSetting:
        unit = known('1') if name == 'gain' else known('s', 'Model timedelta; PID input/output tokens express minutes')
        effective = known(value, 'Configured PID coefficient; recursive error/history, clipping and current target setting are runtime state')
    elif type(value) in (c.NumericSetting, c.PIDSetting):
        unit = known('1') if type(value) is c.NumericSetting else na('PID combines a dimensionless gain and two durations')
    elif type(container) in (c.CurveSetting, c.SeriesSetting):
        resource = record(value)
        fact = dimensions(replace(context, owner=value, record=resource))
        unit = known(tuple(units.unit(d) for d in fact.value), 'Declared resource axes') if fact.status == 'known' else fact
    if type(root) is c.ControlRule and (type(container) is c.PIDSetting or type(value) is c.CurveSetting):
        dimension = controller_dimension(root, record)
        if dimension is None:
            effective = known(value, 'Configured controller; live controller/setpoint dimensions can depend on short circuit, time equality or MISSING link-age state')
    return FieldSemantics(unit=unit, default=default, effective=effective, diagnostics=issues)


CONTROL_FIELD_RULES = tuple(FieldRule(value_type=t, field=f.name, root_type=root, resolve=control_field,
    resolve_item=control_field if f.name in ('conditions', 'then_actions', 'else_actions', 'key') else None)
    for root, types in ((c.ControlVariable, (c.ControlVariable, c.Attribute, Ref)),
                        (c.ControlExpression, (c.ControlExpression, *ARITHMETIC, Ref)),
                        (c.ControlRule, (c.ControlRule, c.Condition, c.Attribute, c.NamedOperand, c.Constant, c.Action, *SETTINGS, Ref, MonthDay)))
    for t in types for f in fields(t))
