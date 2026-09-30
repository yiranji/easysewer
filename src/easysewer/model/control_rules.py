"""Control graph checks, consumer dimensions and explicit transformations."""

from dataclasses import fields, is_dataclass, replace
from datetime import date, timedelta
import re

from ..validation import Diagnostic, DiagnosticSubject, Severity, ValidationReport
from . import controls as c
from .fields import validate_fields
from .identity import Ref, references
from .options import MonthDay
from .units import UnitTransform
from .usage import ResourceUse


def resolve_attribute(operand, record):
    if isinstance(operand, c.Attribute):
        return operand
    if isinstance(operand, c.NamedOperand):
        row = record(operand.reference)
        if isinstance(row, c.ControlVariable):
            return row.value
    return None


def expression_dimension(node, record):
    if isinstance(node, c.ExpressionNumber):
        return "ratio"
    if isinstance(node, c.ExpressionVariable):
        return c.attribute_dimension(record(node.reference).value)
    if isinstance(node, c.UnaryExpression):
        return expression_dimension(node.operand, record)
    if isinstance(node, c.FunctionExpression):
        dimension = expression_dimension(node.argument, record)
        if node.function == "ABS":
            return dimension
        if node.function in ("SGN", "STEP"):
            return "ratio" if dimension is not None else None
        return "ratio" if dimension == "ratio" else None
    if isinstance(node, c.BinaryExpression):
        left, right = expression_dimension(node.left, record), expression_dimension(node.right, record)
        if node.operator in ("+", "-"):
            if isinstance(node.left, c.ExpressionNumber) and node.left.value == 0:
                return right
            if isinstance(node.right, c.ExpressionNumber) and node.right.value == 0:
                return left
            return left if left == right else None
        if node.operator == "*":
            return right if left == "ratio" else left if right == "ratio" else None
        if node.operator == "/":
            return left if right == "ratio" else "ratio" if left is not None and left == right else None
        if node.operator == "^":
            if isinstance(node.right, c.ExpressionNumber):
                if node.right.value == 0:
                    return "ratio" if left is not None else None
                if node.right.value == 1:
                    return left
            return "ratio" if left == right == "ratio" else None
    return None


def operand_dimension(operand, record, *, controller=False):
    attribute = resolve_attribute(operand, record)
    if attribute is not None:
        return c.attribute_dimension(attribute, controller=controller)
    if isinstance(operand, c.NamedOperand):
        expression = record(operand.reference)
        if isinstance(expression, c.ControlExpression):
            return expression_dimension(expression.expression, record)
    return None


def operand_attributes(operand, record):
    attribute = resolve_attribute(operand, record)
    if attribute is not None:
        yield attribute
    elif isinstance(operand, c.NamedOperand):
        expression = record(operand.reference)
        if isinstance(expression, c.ControlExpression):
            for value in _nodes(expression.expression):
                if isinstance(value, c.ExpressionVariable):
                    yield record(value.reference).value


def controller_dimension(rule, record):
    # Short-circuiting can leave any evaluated clause as the active controller.
    # Time EQ/NE return without assigning the global controller/setpoint.
    dimensions = set()
    for condition in rule.conditions:
        # Link-age operands can be MISSING when the link has the other status.
        # This returns before assigning either global value, including when the
        # operand is on the RHS or is a named expression returning MISSING.
        if any(attribute.object_type != "GAGE" and attribute.attribute in ("TIMEOPEN", "TIMECLOSED")
               for operand in (condition.left, condition.right)
               for attribute in operand_attributes(operand, record)):
            return None
        attribute = resolve_attribute(condition.left, record)
        if attribute and c.native_value_attribute(attribute) in c.TIME_ATTRIBUTES and condition.relation in ("=", "<>"):
            return None
        dimensions.add(operand_dimension(condition.left, record, controller=True))
    return next(iter(dimensions)) if len(dimensions) == 1 else None


def _nodes(value):
    for _, child in _located_nodes(value):
        yield child


def _located_nodes(value, path=()):
    yield path, value
    if isinstance(value, tuple):
        for index, child in enumerate(value):
            yield from _located_nodes(child, path + (index,))
    elif is_dataclass(value) and not isinstance(value, Ref):
        for item in fields(value):
            yield from _located_nodes(getattr(value, item.name), path + (item.name,))


def validate_controls(store, profile, *, for_run=False, functions=c.FUNCTIONS, statements=None):
    # Field inspection validates a statement and its dependencies, while keeping
    # the complete store available for references. IDs alone are not unique
    # across VARIABLE, EXPRESSION and RULE namespaces.
    rows = tuple(store.collection("swmm:controls").values()) if statements is None else tuple(statements)
    record = lambda ref: store.collection(ref.collection)[ref.key]
    for row in rows:
        if not ValidationReport(diagnostics=tuple(validate_fields(row))).is_valid:
            continue
        def issue(code, message, *, warning=False, path=(), context=None):
            primary = DiagnosticSubject(collection='swmm:controls', key=c.statement_key(row), path=path)
            related = [DiagnosticSubject(collection='swmm:controls', key=c.statement_key(row))] if path else []
            queue = [ref for _,ref in references(row if context is None else context)]
            seen = set()
            for ref in queue:
                if ref.canonical in seen:
                    continue
                seen.add(ref.canonical)
                related.append(DiagnosticSubject(collection=ref.collection, key=ref.key))
                if ref.collection == 'swmm:controls' and store.contains(ref):
                    queue.extend(ref for _,ref in references(record(ref)))
            return Diagnostic(code=code, message=message, object_id=row.id, feature="swmm:controls",
                              severity=Severity.WARNING if warning else Severity.ERROR,
                              subject=primary, related=tuple(v for v in dict.fromkeys(related) if v != primary))
        if not for_run:
            if isinstance(row, (c.ControlVariable, c.ControlExpression)) and len(row.id.encode("utf-8")) > 32:
                yield issue("control.symbol_length", "Named variable/expression exceeds the native 32-byte name capacity", path=('id',))
            if isinstance(row, c.ControlVariable) and row.id.upper() in c.NATIVE_ATTRIBUTES:
                yield issue("control.reserved_variable", "A variable name cannot equal an attribute", path=('id',))
            for path, value in _located_nodes(row):
                if isinstance(value,c.ExpressionNode) and type(value) not in (c.ExpressionNumber,c.ExpressionVariable,c.UnaryExpression,c.BinaryExpression,c.FunctionExpression):
                    yield issue('control.expression_variant','Arithmetic variable/node is not supported in control expressions', path=path, context=value)
                if isinstance(value, c.FunctionExpression) and value.function not in functions:
                    yield issue("control.function", f"Unsupported arithmetic function {value.function}", path=path+('function',), context=value)
                if isinstance(value, c.ExpressionVariable) and (not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", value.reference.key[1]) or value.reference.key[1].upper() in functions):
                    yield issue("control.expression_name", "Arithmetic variable names must be identifiers distinct from math functions", path=path+('reference',), context=value)
            continue
        for path, value in _located_nodes(row):
            if isinstance(value, c.Attribute) and value.target is not None and store.contains(value.target):
                target = record(value.target)
                if value.object_type == "GAGE":
                    if value.history_hours in range(8, 21):
                        yield issue("control.native_rain_attribute", "Native rainfall-window codes collide with status/time/date attributes; use a named arithmetic expression for a numeric comparison", warning=True, path=path+('history_hours',), context=value)
                    used = any(getattr(s, "rain_gage", None) == value.target for s in store.collection("swmm:subcatchments").values())
                    if not used and not store.opaque_constraints:
                        yield issue("control.unused_gage", "A gage used only by controls returns zero unless runoff/RDII marks it used", warning=True, path=path+('target',), context=value)
                elif value.target.collection == "swmm:links":
                    kind = target.kind.rstrip("S") if target.kind != "CONDUITS" else "CONDUIT"
                    if value.attribute == "SETTING" and kind == "OUTLET":
                        yield issue("control.native_outlet_setting", "Fixed SWMM 5.2.4 returns MISSING for an OUTLET SETTING premise", path=path+('attribute',), context=value)
                    if (value.attribute in ("FULLFLOW", "FULLDEPTH", "LENGTH", "SLOPE", "VELOCITY") and kind != "CONDUIT") or (value.attribute == "STATUS" and kind not in ("CONDUIT", "PUMP")):
                        yield issue("control.native_missing_attribute", "This link type returns MISSING for the requested attribute", path=path+('attribute',), context=value)
            if isinstance(value, c.Action) and store.contains(value.target):
                kind = record(value.target).kind
                if kind != value.object_type+"S":
                    yield issue("control.action_target_kind", f"{value.object_type} action targets a {kind} object", path=path+('target',), context=value)
        if isinstance(row, c.ControlRule):
            try:
                for index, condition in enumerate(row.conditions):
                    path = ('conditions',index)
                    if isinstance(condition.left, c.Constant) or isinstance(condition.right, c.NamedOperand) and condition.right.reference.key[0].upper() == "EXPRESSION":
                        yield issue("control.operand_position", "The left operand must be a variable/expression and the right cannot be an expression", path=path, context=condition)
                        continue
                    attr = resolve_attribute(condition.left, record)
                    if isinstance(condition.right, c.Constant):
                        value = condition.right.value
                        kind = c.native_value_attribute(attr) if attr else "NUMERIC"
                        valid = (isinstance(value, str) if kind == "STATUS" else isinstance(value, timedelta) if kind in c.TIME_ATTRIBUTES else
                                 type(value) is date if kind == "DATE" else isinstance(value, MonthDay) or type(value) in (int,float) if kind == "DAYOFYEAR" else type(value) in (int,float))
                        if not valid:
                            yield issue("control.native_constant_type", f"Fixed engine expects a {kind} constant for this premise", path=path+('right','value'), context=condition)
                        limits = {"DAY":(1,7), "MONTH":(1,12), "DAYOFYEAR":(1,365)}
                        if kind in limits and type(value) in (int,float) and not limits[kind][0] <= value <= limits[kind][1]:
                            yield issue("control.calendar_range", f"Out-of-range {kind} constant", path=path+('right','value'), context=condition)
                    if any(isinstance(a.setting, (c.CurveSetting,c.PIDSetting)) for a in (*row.then_actions,*row.else_actions)):
                        if len(row.conditions) > 1:
                            yield issue("control.short_circuit_controller", "CURVE/PID uses the last evaluated numeric clause, which need not be the final written clause", warning=True, path=('conditions',))
                        if attr and c.native_value_attribute(attr) in c.TIME_ATTRIBUTES:
                            yield issue("control.native_time_controller", "Time equality may retain controller/setpoint state; link-age control uses hours for value but days for setpoint", warning=True, path=path)
            except (KeyError, AttributeError):
                pass  # Missing graph references have their own diagnostics.


def control_resource_uses(store):
    record = lambda ref: store.collection(ref.collection)[ref.key]
    for key, row in store.collection("swmm:controls").items():
        if not isinstance(row, c.ControlRule) or not ValidationReport(diagnostics=tuple(validate_fields(row))).is_valid:
            continue
        owner = Ref(collection="swmm:controls", key=key)
        try:
            dimension = controller_dimension(row, record)
        except (KeyError, AttributeError):
            dimension = None
        for branch in ("then_actions", "else_actions"):
            for index, action in enumerate(getattr(row, branch)):
                setting = action.setting
                if isinstance(setting, c.CurveSetting):
                    yield ResourceUse(owner=owner, target=setting.curve, path=(branch,index,"setting","curve"), role="modulated link control",
                        dimensions=(dimension,"ratio") if dimension else (), accepted_kinds=("CONTROL",))
                elif isinstance(setting, c.SeriesSetting):
                    yield ResourceUse(owner=owner, target=setting.series, path=(branch,index,"setting","series"), role="link setting", dimensions=("ratio",))


def _reject(message, row, path=(), *, value=None, context=None):
    subject = DiagnosticSubject(collection='swmm:controls', key=c.statement_key(row), path=path)
    related = [DiagnosticSubject(collection='swmm:options', key='settings', path=('flow_units',))]
    pending = [target for _, target in references(row if value is None else value)]
    seen = set()
    for target in pending:
        if target.canonical in seen:
            continue
        seen.add(target.canonical)
        related.append(DiagnosticSubject(collection=target.collection, key=target.key))
        if context is not None and target.collection == 'swmm:controls':
            pending.extend(ref for _, ref in references(context.record(target)))
    ValidationReport(diagnostics=(Diagnostic(code="units.control_dimensions", message=message,
        subject=subject, related=tuple(v for v in dict.fromkeys(related) if v != subject)),)).raise_for_errors()


def _convert_expression(row, context):
    if expression_dimension(row.expression, context.record) is None:
        _reject("Expression units cannot be inferred for this combination of dimensions and constants",
                row, ('expression',), context=context)
    return row


def _convert_rule(row, context):
    conditions = []
    for index, condition in enumerate(row.conditions):
        dimension = operand_dimension(condition.left, context.record)
        if dimension is None:
            _reject("Control premise has unknown dimensions", row, ('conditions', index, 'left'), value=condition, context=context)
        factor = context.number(1, dimension)
        attr = resolve_attribute(condition.left, context.record)
        if attr and attr.object_type == "GAGE" and c.native_value_attribute(attr) not in ("NUMERIC", "DEPTH", "MAXDEPTH", "HEAD", "VOLUME", "INFLOW", "FLOW", "FULLFLOW", "FULLDEPTH", "SETTING", "LENGTH", "SLOPE", "VELOCITY") and factor != 1:
            _reject("Native gage attribute-code collision prevents reliable threshold conversion; use a numeric expression",
                    row, ('conditions', index, 'left'), value=condition, context=context)
        right = condition.right
        if isinstance(right, c.Constant):
            if type(right.value) in (int,float):
                right = replace(right, value=context.number(right.value, dimension))
        else:
            right_dimension = operand_dimension(right, context.record)
            if right_dimension is None or context.number(1,right_dimension) != factor:
                _reject("Compared control operands do not transform by the same unit factor", row,
                        ('conditions', index, 'right'), value=condition, context=context)
        conditions.append(replace(condition, right=right))
    modulated = [a for a in (*row.then_actions,*row.else_actions) if isinstance(a.setting,c.PIDSetting)]
    if modulated and controller_dimension(row, context.record) is None:
        _reject("PID control has ambiguous controller dimensions due to short-circuit/time semantics", row,
                ('conditions',), context=context)
    return replace(row, conditions=tuple(conditions))


CONTROL_UNIT_TRANSFORMS = (UnitTransform(value_type=c.ControlRule, convert=_convert_rule),
                           UnitTransform(value_type=c.ControlExpression, convert=_convert_expression))
