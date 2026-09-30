"""Atomic control-program codec with declaration and rule-order preservation."""

from collections import OrderedDict
from dataclasses import replace
from datetime import date, timedelta
import re

from ...model import controls as c
from ...model.fields import validate_fields
from ...model.identity import Ref, canonical_key, references
from ...model.options import MonthDay
from ...schema import DecodedFeature, FeatureDescriptor
from ...schema.structured import EncodedRow, FeatureData, RecordEntry, SourceBinding
from ...validation import Diagnostic, Severity, SourceSpan, ValidationReport
from .expressions import ExpressionCodec
from .geometry import finite_number, number_text
from .options import _native_time, _date
from .field_sources import FieldSources
from .control_sources import control_sources
from ...schema.control_fields import CONTROL_FIELD_RULES
from ...schema.option_profile import decimal_clock_hours
from ...validation._cooperative import checkpointed


class UnsupportedControl(ValueError):
    pass


def _reference(kind, name):
    return Ref(collection="swmm:controls", key=(kind, name))


def _key(row, index=0):
    return "CONTROLS", row.kind, canonical_key(row.id), str(index)


def format_attribute(value):
    attribute = f"{value.history_hours}-HR_DEPTH" if value.history_hours else value.attribute
    return (value.object_type, attribute) if value.target is None else (value.object_type, value.target.key, attribute)


def format_constant(value):
    if isinstance(value, timedelta):
        # Native clock fields truncate fractions of seconds; numeric hours do not.
        if value.microseconds or value.total_seconds() > 2_147_483_647:
            return decimal_clock_hours(value)
        hours, seconds = divmod(int(value.total_seconds()), 3600)
        minutes, seconds = divmod(seconds, 60)
        return f"{hours:02}:{minutes:02}:{seconds:02}"
    if isinstance(value, date):
        return value.strftime("%m/%d/%Y")
    if isinstance(value, MonthDay):
        return f"{value.month:02}/{value.day:02}"
    return value if isinstance(value, str) else number_text(value)


class ControlsCodec:
    descriptor = FeatureDescriptor(key="swmm:controls", sections=frozenset({"CONTROLS"}), atomic_write=True,
                                   ordered_sections=frozenset({"CONTROLS"}), requires=("swmm:network",), rewrite_after=("swmm:network",))
    collections = (c.CONTROL_COLLECTION,)
    field_rules = CONTROL_FIELD_RULES

    def __init__(self, *, expressions=None):
        self.expressions = expressions or ExpressionCodec()

    @property
    def unit_transforms(self):
        from ...model.control_rules import CONTROL_UNIT_TRANSFORMS
        return CONTROL_UNIT_TRANSFORMS

    def symbol_conflicts(self, rows):
        """Check the names that the canonical declaration order actually binds."""
        symbols = {kind: tuple(row for row in rows if row.kind == kind)
                   for kind in ("VARIABLE", "EXPRESSION")}

        def resolve(token, kinds):
            return next((_reference(row.kind, row.id).canonical
                         for kind in kinds for row in symbols[kind]
                         if canonical_key(token).startswith(canonical_key(row.id))), None)

        for row in rows:
            if not ValidationReport(diagnostics=tuple(validate_fields(row))).is_valid:
                continue
            if isinstance(row, c.ControlExpression):
                for _, ref in references(row.expression):
                    if ref.collection == "swmm:controls" and resolve(ref.key[1], ("VARIABLE",)) != ref.canonical:
                        yield row, f"Native prefix lookup shadows arithmetic variable {ref.key[1]!r}"
            if isinstance(row, c.ControlRule):
                for condition in row.conditions:
                    for operand, kinds in ((condition.left, ("EXPRESSION", "VARIABLE")), (condition.right, ("VARIABLE",))):
                        expected = None
                        if isinstance(operand, c.NamedOperand):
                            token, expected = operand.reference.key[1], operand.reference.canonical
                        elif isinstance(operand, c.Attribute):
                            token = operand.object_type
                        elif isinstance(operand, c.Constant):
                            token = format_constant(operand.value)
                        else:
                            continue
                        if resolve(token, kinds) != expected:
                            yield row, f"Native prefix lookup changes the meaning of premise token {token!r} after declarations are ordered"

    def decode(self, document, profile):
        lines = document.records("CONTROLS")
        if not lines:
            return DecodedFeature(value=FeatureData())
        records, groups, issues = OrderedDict(), {}, []
        expression_spans = {}
        current, state = None, None
        line = lines[0]

        def issue(code, message, severity=Severity.WARNING):
            issues.append(Diagnostic(code=code, message=message, severity=severity, section="CONTROLS",
                span=SourceSpan(source=document.source, line=line.number, column=1, end_column=len(line.content)+1)))

        def resolve(token, kind):
            key = canonical_key(token)
            return next((_reference(row.kind, row.id) for row in checkpointed(records.values())
                         if row.kind == kind and key.startswith(canonical_key(row.id))), None)

        def variable(token):
            result = resolve(token, "VARIABLE")
            if result is None:
                raise ValueError(f"Named variable must be declared before use: {token}")
            return result

        def attribute(tokens):
            if not tokens or tokens[0].upper() not in c.ATTRIBUTES:
                raise UnsupportedControl("Unsupported control object")
            obj = tokens[0].upper()
            count = 2 if obj == "SIMULATION" else 3
            if len(tokens) < count:
                raise ValueError("Incomplete control attribute")
            attr = tokens[count-1].upper()
            history = None
            if obj == "GAGE" and attr != "INTENSITY":
                match = re.fullmatch(r"([1-9][0-9]*)-HR_DEPTH", attr)
                if not match:
                    raise UnsupportedControl("Unsupported accumulated-rainfall attribute")
                attr, history = "DEPTH", int(match[1])
            if obj in ("LINK", "CONDUIT") and attr == "MAXDEPTH":
                attr = "FULLDEPTH"
                issue("control.native_fulldepth", "Native requires FULLDEPTH where the manual lists link MAXDEPTH; explicitly normalize before running")
            target = None if obj == "SIMULATION" else Ref(collection="swmm:nodes" if obj == "NODE" else "swmm:raingages" if obj == "GAGE" else "swmm:links", key=tokens[1])
            return c.Attribute(object_type=obj, attribute=attr, target=target, history_hours=history), count

        def operand(tokens, *, lhs=False):
            named = resolve(tokens[0], "EXPRESSION") if lhs else None
            named = named or resolve(tokens[0], "VARIABLE")
            return (c.NamedOperand(reference=named), 1) if named else attribute(tokens)

        def source_attribute(value):
            if isinstance(value, c.Attribute):
                return value
            if value.reference.key[0] == "VARIABLE":
                return records[value.reference.canonical.key].value
            return None

        def constant(token, left):
            attr = source_attribute(left)
            kind = c.native_value_attribute(attr) if attr else "NUMERIC"
            if kind == "STATUS":
                value = token.upper()
                if value not in ("ON", "OFF", "OPEN", "CLOSED"):
                    raise ValueError("Expected a status constant")
            elif kind in c.TIME_ATTRIBUTES:
                value, changed = _native_time(token, integer_seconds=False)
                if changed:
                    issue("control.time_precision", "Native truncates fractional clock components; numeric hours retain fractional seconds")
            elif kind == "DATE":
                value = _date(token)
            elif kind == "DAYOFYEAR" and "/" in token:
                parts = token.split("/")
                if len(parts) != 2:
                    raise ValueError("DAYOFYEAR requires month/day or a day number")
                value = MonthDay(month=int(parts[0]), day=int(parts[1]))
            else:
                value = finite_number(token)
                limits = {"DAY":(1,7), "MONTH":(1,12), "DAYOFYEAR":(1,365)}
                if kind in limits and not limits[kind][0] <= value <= limits[kind][1]:
                    raise ValueError(f"Out-of-range {kind} constant")
            return c.Constant(value=value)

        def condition(values):
            left, index = operand(values[1:], lhs=True)
            index += 1
            if len(values) <= index+1:
                raise ValueError("Condition requires a relation and right operand")
            relation = values[index]
            right_tokens = values[index+1:]
            if resolve(right_tokens[0], "VARIABLE") or right_tokens[0].upper() in c.ATTRIBUTES:
                right, count = operand(right_tokens)
                if len(right_tokens) != count:
                    raise ValueError("Unexpected right-operand suffix")
            else:
                if len(right_tokens) != 1:
                    raise ValueError("Constant condition requires one right-side value")
                right = constant(right_tokens[0], left)
            return c.Condition(left=left, relation=relation, right=right, conjunction=values[0].upper())

        def action(values):
            if len(values) < 6 or values[4] != "=":
                raise ValueError("Action requires object, ID, attribute, = and setting")
            obj, attr, value = values[1].upper(), values[3].upper(), values[5].upper()
            if attr == "STATUS" and len(values) == 6:
                setting = c.StatusSetting(value=value)
            elif attr == "SETTING":
                if value == "CURVE" and len(values) == 7:
                    setting = c.CurveSetting(curve=Ref(collection="swmm:curves", key=values[6]))
                elif value == "TIMESERIES" and len(values) == 7:
                    setting = c.SeriesSetting(series=Ref(collection="swmm:timeseries", key=values[6]))
                elif value == "PID" and len(values) == 9:
                    setting = c.PIDSetting(gain=finite_number(values[6]), integral_time=timedelta(minutes=finite_number(values[7])),
                                           derivative_time=timedelta(minutes=finite_number(values[8])))
                elif len(values) == 6:
                    setting = c.NumericSetting(value=finite_number(values[5]))
                else:
                    raise UnsupportedControl("Unsupported modulated action")
            else:
                raise UnsupportedControl("Unsupported control action attribute")
            return c.Action(target=Ref(collection="swmm:links", key=values[2]), object_type=obj, setting=setting)

        try:
            lexical = {item.span.line for item in checkpointed(document.report.errors) if item.span}
            for line in checkpointed(lines):
                if line.number in lexical:
                    raise ValueError("Control program contains lexical errors")
                values, keyword = line.values, line.values[0].upper()
                if keyword in ("VARIABLE", "EXPRESSION"):
                    if len(values) < 4 or values[2] != "=":
                        raise ValueError("Named control definition requires name = value")
                    if len(values[1].encode("utf-8")) > 32:
                        raise UnsupportedControl("Native named control symbols are limited to 32 bytes")
                    if keyword == "VARIABLE":
                        if values[1].upper() in c.NATIVE_ATTRIBUTES:
                            raise ValueError("Variable name cannot equal a control attribute")
                        value, count = attribute(values[3:])
                        if count != len(values)-3:
                            raise ValueError("Unexpected variable-definition suffix")
                        row = c.ControlVariable(id=values[1], value=value)
                    else:
                        text = " ".join(values[3:])
                        if type(self.expressions).parse is ExpressionCodec.parse:
                            expression, spans = self.expressions._parse_with_spans(text, variable)
                        else:
                            expression, spans = self.expressions.parse(text, variable), None
                        row = c.ControlExpression(id=values[1], expression=expression)
                        expression_spans[canonical_key(c.statement_key(row))] = spans
                    key = canonical_key(c.statement_key(row))
                    if key in records:
                        raise UnsupportedControl("Repeated named definitions require preserving the whole source program")
                    records[key], groups[key] = row, [line.number]
                    continue
                if keyword == "RULE":
                    if len(values) != 2:
                        raise ValueError("RULE requires one ID")
                    current = ("RULE", canonical_key(values[1]))
                    if current in records:
                        raise ValueError("Duplicate control rule")
                    records[current] = c.ControlRule(id=values[1], conditions=(), then_actions=())
                    groups[current], state = [line.number], "RULE"
                    continue
                if current is None:
                    raise UnsupportedControl("A control clause requires a preceding RULE")
                row = records[current]
                if (keyword == "IF" and state == "RULE") or (keyword in ("AND", "OR") and state == "IF"):
                    row = replace(row, conditions=(*row.conditions, condition(values)))
                    state = "IF"
                elif (keyword == "THEN" and state == "IF") or (keyword == "AND" and state == "THEN"):
                    row = replace(row, then_actions=(*row.then_actions, action(values)))
                    state = "THEN"
                elif (keyword == "ELSE" and state == "THEN") or (keyword == "AND" and state == "ELSE"):
                    row = replace(row, else_actions=(*row.else_actions, action(values)))
                    state = "ELSE"
                elif keyword == "PRIORITY" and state in ("THEN", "ELSE") and len(values) == 2:
                    row, state = replace(row, priority=finite_number(values[1])), "PRIORITY"
                else:
                    raise UnsupportedControl(f"Unsupported control clause or order: {keyword} after {state}")
                records[current] = row
                groups[current].append(line.number)
            for row in checkpointed(records.values()):
                ValidationReport(diagnostics=tuple(validate_fields(row))).raise_for_errors()
            conflicts = tuple(self.symbol_conflicts(tuple(records.values())))
            if conflicts:
                raise UnsupportedControl(conflicts[0][1])
            bindings = tuple(SourceBinding(line=number, key=_key(records[key], index)) for key, numbers in checkpointed(groups.items()) for index, number in checkpointed(enumerate(numbers)))
            source_fields = FieldSources()
            for key, row in checkpointed(records.items()):
                control_sources(source_fields, row, tuple(document.lines[n - 1] for n in checkpointed(groups[key])), expression_spans.get(key))
            originals = {_reference(row.kind, row.id).canonical: row for row in checkpointed(records.values())}
            return DecodedFeature(value=FeatureData(records=tuple(RecordEntry(collection="swmm:controls", value=row) for row in checkpointed(records.values())), bindings=bindings,
                **source_fields.finish(originals)),
                claimed_lines=frozenset(item.line for item in checkpointed(bindings)), report=ValidationReport(diagnostics=tuple(issues)))
        except (ValueError, TypeError, OverflowError, RecursionError) as error:
            issue("control.unsupported_program" if isinstance(error, UnsupportedControl) else "control.invalid_program", str(error),
                  Severity.WARNING if isinstance(error, UnsupportedControl) else Severity.ERROR)
            return DecodedFeature(value=FeatureData(), report=ValidationReport(diagnostics=tuple(issues)))

    def encode(self, store, profile):
        rows = tuple(store.collection("swmm:controls").values())

        def operand(value):
            if type(value) is c.Attribute:
                return format_attribute(value)
            if type(value) is c.NamedOperand:
                return (value.reference.key[1],)
            if type(value) is c.Constant:
                return (format_constant(value.value),)
            raise TypeError(f"No condition writer for {type(value).__name__}")

        def action(value):
            setting = value.setting
            if type(setting) is c.StatusSetting:
                return value.object_type, value.target.key, "STATUS", "=", setting.value
            if type(setting) is c.NumericSetting:
                tail = (number_text(setting.value),)
            elif type(setting) is c.CurveSetting:
                tail = "CURVE", setting.curve.key
            elif type(setting) is c.SeriesSetting:
                tail = "TIMESERIES", setting.series.key
            elif type(setting) is c.PIDSetting:
                tail = "PID", number_text(setting.gain), number_text(setting.integral_time.total_seconds()/60), number_text(setting.derivative_time.total_seconds()/60)
            else:
                raise TypeError(f"No action writer for {type(setting).__name__}")
            return value.object_type, value.target.key, "SETTING", "=", *tail

        # Hoist definitions without changing order within each namespace or the
        # relative order of rules. This also supports definitions between clauses.
        for kind in checkpointed(("VARIABLE", "EXPRESSION", "RULE")):
            for row in checkpointed(rows):
                if row.kind != kind:
                    continue
                if type(row) is c.ControlVariable:
                    output = [(kind, row.id, "=", *format_attribute(row.value))]
                elif type(row) is c.ControlExpression:
                    output = [(kind, row.id, "=", self.expressions.format(row.expression))]
                elif type(row) is c.ControlRule:
                    output = [("RULE", row.id)]
                    output.extend((item.conjunction, *operand(item.left), item.relation, *operand(item.right)) for item in checkpointed(row.conditions))
                    for actions, keyword in checkpointed(((row.then_actions, "THEN"), (row.else_actions, "ELSE"))):
                        output.extend((keyword if index == 0 else "AND", *action(item)) for index, item in checkpointed(enumerate(actions)))
                    if row.priority is not None:
                        output.append(("PRIORITY", number_text(row.priority)))
                else:
                    raise TypeError(f"No control statement writer for {type(row).__name__}")
                owner = _reference(kind, row.id)
                for index, values in checkpointed(enumerate(output)):
                    yield EncodedRow(key=_key(row, index), section="CONTROLS", values=tuple(values), owners=(owner,))

    def validate(self, store, profile):
        from ...model.control_rules import validate_controls
        yield from validate_controls(store, profile, functions=self.expressions.functions)
        for row, message in self.symbol_conflicts(tuple(store.collection("swmm:controls").values())):
            yield Diagnostic(code="control.symbol_shadow", message=message, object_id=row.id, feature="swmm:controls")

    def validate_run(self, store, profile):
        from ...model.control_rules import validate_controls
        yield from validate_controls(store, profile, for_run=True, functions=self.expressions.functions)

    def resource_uses(self, store, profile):
        from ...model.control_rules import control_resource_uses
        yield from control_resource_uses(store)

    def validate_document(self, document, profile):
        declarations = {(kind[:-1], canonical_key(line.values[0])): line.number
                        for kind in ("CONDUITS", "PUMPS", "ORIFICES", "WEIRS", "OUTLETS")
                        for line in document.records(kind)}
        for line in document.records("CONTROLS"):
            values = line.values
            if len(values) >= 6 and values[0].upper() in ("THEN", "ELSE", "AND") and values[4] == "=":
                declared = declarations.get((values[1].upper(), canonical_key(values[2])))
                if declared is not None and declared > line.number:
                    yield Diagnostic(code="control.action_declaration_order", message="Native control actions require their link type to be declared earlier; normalize the document",
                        section="CONTROLS", span=SourceSpan(source=document.source, line=line.number, column=1, end_column=len(line.content)+1))
            if any(values[i].upper() in ("LINK", "CONDUIT") and values[i+2].upper() == "MAXDEPTH" for i in range(len(values)-2)):
                yield Diagnostic(code="control.native_fulldepth", message="Native link premises require FULLDEPTH; explicitly normalize the manual alias",
                    section="CONTROLS", span=SourceSpan(source=document.source, line=line.number, column=1, end_column=len(line.content)+1))
