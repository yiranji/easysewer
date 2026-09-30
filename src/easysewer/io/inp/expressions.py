"""Non-executing parser for SWMM arithmetic and its signed-number rules."""

import re
from dataclasses import fields, is_dataclass

from ...model import expressions as c
from .geometry import finite_number, number_text

_NUMBER = re.compile(r"(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")
_NAME = re.compile(r"[A-Za-z_][A-Za-z_0-9]*")


class ExpressionCodec:
    def __init__(self, *, functions=c.FUNCTIONS):
        self.functions = tuple(functions)

    def variable(self, resolved):
        from ...model.controls import ExpressionVariable
        return ExpressionVariable(reference=resolved)

    def format_variable(self, node):
        from ...model.controls import ExpressionVariable
        if type(node) is ExpressionVariable:
            return node.reference.key[1]
        raise TypeError(f"No arithmetic writer for {type(node).__name__}")

    def parse(self, text, resolve):
        return self._parse(text, resolve, trace=False)

    def _parse_with_spans(self, text, resolve):
        """Return the same AST plus original half-open character spans by path."""
        return self._parse(text, resolve, trace=True)

    def _parse(self, text, resolve, *, trace):
        tokens, position, previous = [], 0, "START"
        while position < len(text):
            char = text[position]
            if char.isspace():
                position += 1
                continue
            negative = char == "-" and position+1 < len(text) and text[position+1].isdigit() and previous in ("START", "(", ")", "+", "-", "*", "/", "^")
            match = _NUMBER.match(text, position+1 if negative else position)
            if match:
                tokens.append(("NUMBER", finite_number(("-" if negative else "")+match[0]), position, match.end()))
                position, previous = match.end(), "NUMBER"
                continue
            match = _NAME.match(text, position)
            if match:
                tokens.append(("NAME", match[0], position, match.end()))
                position, previous = match.end(), "NAME"
            elif char in "()+-*/^":
                tokens.append((char, char, position, position+1))
                position, previous = position+1, char
            else:
                raise ValueError(f"Unsupported expression character at {position+1}")
        cursor = 0
        locations = {}

        def located(node, start, **attributes):
            if trace:
                locations[id(node)] = ((tokens[start][2], tokens[cursor-1][3]), attributes)
            return node

        def token_span(token):
            return token[2], token[3]

        def take(kind=None):
            nonlocal cursor
            if cursor >= len(tokens) or (kind and tokens[cursor][0] != kind):
                raise ValueError(f"Expected expression token {kind or 'operand'}")
            token = tokens[cursor]
            cursor += 1
            return token

        def peek():
            return tokens[cursor][0] if cursor < len(tokens) else None

        def single():
            start = cursor
            token = take()
            kind, value = token[:2]
            if kind == "NUMBER":
                node = located(c.ExpressionNumber(value=value), start, value=token_span(token))
            elif kind == "NAME":
                if value.upper() in self.functions:
                    take("(")
                    node = c.FunctionExpression(function=value.upper(), argument=expression())
                    take(")")
                    located(node, start, function=token_span(token))
                else:
                    node = self.variable(resolve(value))
                    located(node, start, **{f.name: token_span(token) for f in fields(node)})
            elif kind == "(":
                node = expression()
                take(")")
                if trace:
                    located(node, start, **locations[id(node)][1])
            else:
                raise ValueError("Expected a number, named variable, function or parenthesized expression")
            if peek() == "^":
                operator = take()
                node = located(c.BinaryExpression(operator="^", left=node, right=single()), start,
                               operator=token_span(operator))
            return node

        def term(*, first=False):
            start = cursor
            sign = take() if first and peek() in ("+", "-") else None
            operand_start = cursor
            node = single()
            while peek() in ("*", "/"):
                operator = take()
                node = located(c.BinaryExpression(operator=operator[0], left=node, right=single()), operand_start,
                               operator=token_span(operator))
            return located(c.UnaryExpression(operator=sign[0], operand=node), start,
                           operator=token_span(sign)) if sign else node

        def expression():
            start = cursor
            node = term(first=True)
            while peek() in ("+", "-"):
                operator = take()
                node = located(c.BinaryExpression(operator=operator[0], left=node, right=term()), start,
                               operator=token_span(operator))
            return node

        result = expression()
        if cursor != len(tokens):
            raise ValueError("Unexpected expression suffix")
        if not trace:
            return result
        spans = {}
        def collect(node, path=(), inherited=None):
            span, attributes = locations.get(id(node), (inherited, {}))
            spans[path] = span
            for attr in fields(node):
                child = getattr(node, attr.name)
                child_path = path + (attr.name,)
                if is_dataclass(child):
                    collect(child, child_path, span)
                else:
                    spans[child_path] = attributes.get(attr.name, span)
        collect(result)
        return result, spans

    def format(self, node):
        if type(node) is c.ExpressionNumber:
            return number_text(node.value)
        if type(node) is c.UnaryExpression:
            return f"({node.operator} ({self.format(node.operand)}))"
        if type(node) is c.BinaryExpression:
            return f"({self.format(node.left)} {node.operator} {self.format(node.right)})"
        if type(node) is c.FunctionExpression and node.function in self.functions:
            return f"{node.function}({self.format(node.argument)})"
        return self.format_variable(node)
