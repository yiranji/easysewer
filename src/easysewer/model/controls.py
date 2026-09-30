"""Ordered control statements and immutable, reference-bearing syntax nodes."""

from dataclasses import dataclass
from datetime import date, timedelta
import math
from typing import ClassVar, Literal

from ..validation import Diagnostic
from .fields import number, reference, validate_fields
from .identity import Ref
from .network import Entity
from .options import MonthDay
from .store import CollectionSpec
from .expressions import (FUNCTIONS, ExpressionNode, ExpressionNumber,
    UnaryExpression, BinaryExpression, FunctionExpression)

ATTRIBUTES = {
    "NODE": ("DEPTH", "MAXDEPTH", "HEAD", "VOLUME", "INFLOW"),
    "LINK": ("FLOW", "FULLFLOW", "DEPTH", "FULLDEPTH", "VELOCITY", "LENGTH", "SLOPE", "STATUS", "TIMEOPEN", "TIMECLOSED"),
    "PUMP": ("FLOW", "STATUS", "SETTING", "TIMEOPEN", "TIMECLOSED"),
    "ORIFICE": ("FLOW", "SETTING", "TIMEOPEN", "TIMECLOSED"),
    "WEIR": ("FLOW", "SETTING", "TIMEOPEN", "TIMECLOSED"),
    "OUTLET": ("FLOW", "SETTING", "TIMEOPEN", "TIMECLOSED"),
    "SIMULATION": ("TIME", "DATE", "CLOCKTIME", "DAYOFYEAR", "DAY", "MONTH"),
    "GAGE": ("INTENSITY", "DEPTH"),
}
ATTRIBUTES["CONDUIT"] = ATTRIBUTES["LINK"]
TIME_ATTRIBUTES = ("TIME", "CLOCKTIME", "TIMEOPEN", "TIMECLOSED")
NATIVE_ATTRIBUTES = ("DEPTH", "MAXDEPTH", "HEAD", "VOLUME", "INFLOW", "FLOW", "FULLFLOW", "FULLDEPTH", "STATUS", "SETTING",
                     "LENGTH", "SLOPE", "VELOCITY", "TIMEOPEN", "TIMECLOSED", "TIME", "DATE", "CLOCKTIME", "DAYOFYEAR", "DAY", "MONTH")


@dataclass(frozen=True, kw_only=True)
class Operand:
    """Extension base for premise operands; codecs declare supported variants."""


@dataclass(frozen=True, kw_only=True)
class Attribute(Operand):
    object_type: str
    attribute: str
    target: Ref | None = None
    history_hours: int | None = number("hours", None, minimum=1, maximum=48, integer=True)

    def validate_local(self):
        allowed = ATTRIBUTES.get(self.object_type, ())
        if self.attribute not in allowed:
            yield Diagnostic(code="control.attribute", message="Attribute is not available for this control object", field="attribute")
        namespace = "swmm:nodes" if self.object_type == "NODE" else "swmm:raingages" if self.object_type == "GAGE" else "swmm:links"
        if self.object_type == "SIMULATION":
            if self.target is not None:
                yield Diagnostic(code="control.target", message="Simulation attributes have no object reference", field="target")
        elif self.target is None or self.target.collection != namespace:
            yield Diagnostic(code="control.target", message=f"Expected a reference to {namespace}", field="target")
        if (self.object_type == "GAGE" and self.attribute == "DEPTH") != (self.history_hours is not None):
            yield Diagnostic(code="control.rain_history", message="Only accumulated gage depth requires a 1–48 hour window", field="history_hours")


@dataclass(frozen=True, kw_only=True)
class NamedOperand(Operand):
    reference: Ref = reference("swmm:controls")

    def validate_local(self):
        if not isinstance(self.reference.key, tuple) or len(self.reference.key) != 2 or self.reference.key[0].upper() not in ("VARIABLE", "EXPRESSION"):
            yield Diagnostic(code="control.named_reference", message="Named operands reference a VARIABLE or EXPRESSION", field="reference")


@dataclass(frozen=True, kw_only=True)
class Constant(Operand):
    value: float | timedelta | date | MonthDay | str

    def validate_local(self):
        if type(self.value) in (int, float) and not math.isfinite(self.value):
            yield Diagnostic(code="control.constant", message="Control constants must be finite", field="value")
        if isinstance(self.value, str) and self.value not in ("ON", "OFF", "OPEN", "CLOSED"):
            yield Diagnostic(code="control.status", message="Unknown status constant", field="value")
        if isinstance(self.value, timedelta) and self.value < timedelta():
            yield Diagnostic(code="control.duration", message="Premise duration must be nonnegative", field="value")


@dataclass(frozen=True, kw_only=True)
class ExpressionVariable(ExpressionNode):
    reference: Ref = reference("swmm:controls")

    def validate_local(self):
        if not isinstance(self.reference.key, tuple) or len(self.reference.key) != 2 or self.reference.key[0].upper() != "VARIABLE":
            yield Diagnostic(code="control.expression_reference", message="Arithmetic expressions refer only to named VARIABLE records", field="reference")


@dataclass(frozen=True, kw_only=True)
class Condition:
    left: Operand
    relation: Literal["=", "<>", "<", "<=", ">", ">="]
    right: Operand
    conjunction: Literal["IF", "AND", "OR"] = "IF"


@dataclass(frozen=True, kw_only=True)
class Setting:
    pass


@dataclass(frozen=True, kw_only=True)
class StatusSetting(Setting):
    value: Literal["ON", "OFF", "OPEN", "CLOSED"]


@dataclass(frozen=True, kw_only=True)
class NumericSetting(Setting):
    value: float = number("ratio")


@dataclass(frozen=True, kw_only=True)
class CurveSetting(Setting):
    curve: Ref = reference("swmm:curves")


@dataclass(frozen=True, kw_only=True)
class SeriesSetting(Setting):
    series: Ref = reference("swmm:timeseries")


@dataclass(frozen=True, kw_only=True)
class PIDSetting(Setting):
    gain: float = number("ratio")
    integral_time: timedelta
    derivative_time: timedelta


@dataclass(frozen=True, kw_only=True)
class Action:
    target: Ref = reference("swmm:links")
    object_type: Literal["CONDUIT", "PUMP", "ORIFICE", "WEIR", "OUTLET"]
    setting: Setting

    def validate_local(self):
        if self.object_type == "CONDUIT" and not isinstance(self.setting, StatusSetting):
            yield Diagnostic(code="control.conduit_action", message="Conduit actions require OPEN/CLOSED status", field="setting")
        if isinstance(self.setting, StatusSetting):
            allowed = ("ON", "OFF") if self.object_type == "PUMP" else ("OPEN", "CLOSED") if self.object_type == "CONDUIT" else ()
            if self.setting.value not in allowed:
                yield Diagnostic(code="control.action_status", message="Status does not apply to this object type", field="setting")
        if isinstance(self.setting, NumericSetting) and self.object_type != "PUMP" and not 0 <= self.setting.value <= 1:
            yield Diagnostic(code="control.setting_range", message="Native direct regulator settings must be in [0, 1]", field="setting")


@dataclass(frozen=True, kw_only=True)
class ControlStatement(Entity):
    kind: ClassVar[str] = "STATEMENT"


@dataclass(frozen=True, kw_only=True)
class ControlVariable(ControlStatement):
    kind: ClassVar[str] = "VARIABLE"
    value: Attribute


@dataclass(frozen=True, kw_only=True)
class ControlExpression(ControlStatement):
    kind: ClassVar[str] = "EXPRESSION"
    expression: ExpressionNode


@dataclass(frozen=True, kw_only=True)
class ControlRule(ControlStatement):
    kind: ClassVar[str] = "RULE"
    conditions: tuple[Condition, ...]
    then_actions: tuple[Action, ...]
    else_actions: tuple[Action, ...] = ()
    priority: float | None = number("ratio", None)

    def validate_local(self):
        if not self.conditions or self.conditions[0].conjunction != "IF" or any(row.conjunction == "IF" for row in self.conditions[1:]):
            yield Diagnostic(code="control.conditions", message="A rule requires IF followed by ordered AND/OR clauses", field="conditions")
        if not self.then_actions:
            yield Diagnostic(code="control.actions", message="A rule requires at least one THEN action", field="then_actions")


def statement_key(row):
    return row.kind, row.id


CONTROL_COLLECTION = CollectionSpec(key="swmm:controls", record_type=ControlStatement, key_of=statement_key,
                                    identity_field="id", validate=validate_fields)


def native_value_attribute(attribute):
    """The fixed engine unfortunately shares gage-window and attribute codes."""
    if attribute.object_type != "GAGE":
        return attribute.attribute
    code = attribute.history_hours or 0
    return NATIVE_ATTRIBUTES[code] if code < len(NATIVE_ATTRIBUTES) else "NUMERIC"


def attribute_dimension(value, *, controller=False):
    if value.object_type == "GAGE":
        return "rain_depth" if value.history_hours else "rain_intensity"
    if value.attribute in TIME_ATTRIBUTES:
        return "hours" if controller and value.attribute in ("TIMEOPEN", "TIMECLOSED") else "days"
    return {"DEPTH":"depth", "MAXDEPTH":"depth", "FULLDEPTH":"depth", "HEAD":"elevation", "VOLUME":"volume",
            "INFLOW":"flow", "FLOW":"flow", "FULLFLOW":"flow", "VELOCITY":"velocity", "LENGTH":"length", "SLOPE":"slope"}.get(value.attribute, "ratio")
