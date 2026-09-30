"""Shared, non-executing arithmetic syntax with domain-specific variable leaves."""

from dataclasses import dataclass
from typing import Literal

from .fields import number

FUNCTIONS = ("ABS", "SGN", "STEP", "SQRT", "LOG", "LOG10", "EXP", "SIN", "COS", "TAN", "COT",
             "ASIN", "ACOS", "ATAN", "ACOT", "SINH", "COSH", "TANH", "COTH")


@dataclass(frozen=True, kw_only=True)
class ExpressionNode:
    """Safe syntax only: expression text is never passed to Python eval."""


@dataclass(frozen=True, kw_only=True)
class ExpressionNumber(ExpressionNode):
    value: float = number("ratio")


@dataclass(frozen=True, kw_only=True)
class UnaryExpression(ExpressionNode):
    operator: Literal["+", "-"]
    operand: ExpressionNode


@dataclass(frozen=True, kw_only=True)
class BinaryExpression(ExpressionNode):
    operator: Literal["+", "-", "*", "/", "^"]
    left: ExpressionNode
    right: ExpressionNode


@dataclass(frozen=True, kw_only=True)
class FunctionExpression(ExpressionNode):
    function: str
    argument: ExpressionNode


def walk_expression(node):
    """Visit syntax leaves without resolving references or executing formulas."""
    yield node
    if isinstance(node, UnaryExpression):
        yield from walk_expression(node.operand)
    elif isinstance(node, BinaryExpression):
        yield from walk_expression(node.left)
        yield from walk_expression(node.right)
    elif isinstance(node, FunctionExpression):
        yield from walk_expression(node.argument)
