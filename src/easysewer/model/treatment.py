"""Node/pollutant treatment relations and domain-specific arithmetic leaves."""

from collections import Counter, deque
from dataclasses import dataclass, replace
import re
from typing import Literal

from ..validation import Diagnostic, DiagnosticSubject, Severity, ValidationReport
from .expressions import (ExpressionNode, ExpressionNumber, UnaryExpression,
    BinaryExpression, FunctionExpression, FUNCTIONS, walk_expression)
from .fields import reference, validate_fields
from .identity import Ref, canonical_key
from .network import Storage
from .pollutant_units import PollutantUnitConversion, PollutantUnitTransform
from .store import CollectionSpec
from .units import UnitTransform

PROCESS_VARIABLES = ('HRT', 'DT', 'FLOW', 'DEPTH', 'AREA')
_NAME = re.compile(r'[A-Za-z_][A-Za-z_0-9]*\Z')


@dataclass(frozen=True, kw_only=True)
class TreatmentProcessVariable(ExpressionNode):
    name: Literal['HRT', 'DT', 'FLOW', 'DEPTH', 'AREA']


@dataclass(frozen=True, kw_only=True)
class TreatmentConcentration(ExpressionNode):
    pollutant: Ref = reference('swmm:pollutants')

    def validate_local(self):
        if type(self.pollutant.key) is not str or not _NAME.fullmatch(self.pollutant.key):
            yield Diagnostic(code='treatment.variable_identifier', message='Concentration variables require an ASCII arithmetic identifier')


@dataclass(frozen=True, kw_only=True)
class TreatmentRemoval(ExpressionNode):
    pollutant: Ref = reference('swmm:pollutants')

    def validate_local(self):
        if type(self.pollutant.key) is not str or not _NAME.fullmatch('R_'+self.pollutant.key):
            yield Diagnostic(code='treatment.variable_identifier', message='Removal variables require an ASCII arithmetic identifier after the R_ prefix')


@dataclass(frozen=True, kw_only=True)
class Treatment:
    node: Ref = reference('swmm:nodes')
    pollutant: Ref = reference('swmm:pollutants')
    kind: Literal['C', 'R']
    expression: ExpressionNode

    def validate_local(self):
        for item in walk_expression(self.expression):
            if type(item) not in (ExpressionNumber, UnaryExpression, BinaryExpression, FunctionExpression,
                    TreatmentProcessVariable, TreatmentConcentration, TreatmentRemoval):
                yield Diagnostic(code='treatment.expression_variant', message='Unsupported treatment arithmetic node')
            if type(item) is FunctionExpression and item.function not in FUNCTIONS:
                yield Diagnostic(code='treatment.expression_function', message='Unsupported native arithmetic function')


TREATMENT_COLLECTION = CollectionSpec(key='swmm:treatment', record_type=Treatment,
    key_of=lambda row:(row.node.key, row.pollutant.key), validate=validate_fields)


def resolve_variable(token, pollutants):
    """Native process-prefix, exact concentration, then R_ removal lookup."""
    name = canonical_key(token)
    process = next((p for p in PROCESS_VARIABLES if name.startswith(p)), None)
    if process:
        return TreatmentProcessVariable(name=process)
    if name in pollutants:
        return TreatmentConcentration(pollutant=Ref(collection='swmm:pollutants', key=token))
    if name.startswith('R_') and name[2:] in pollutants:
        return TreatmentRemoval(pollutant=Ref(collection='swmm:pollutants', key=token[2:]))
    raise ValueError(f'Unknown treatment variable {token!r}')


def variable_text(value):
    if type(value) is TreatmentProcessVariable:
        return value.name
    if type(value) is TreatmentConcentration:
        return value.pollutant.key
    if type(value) is TreatmentRemoval:
        return 'R_'+value.pollutant.key
    raise TypeError(f'No treatment arithmetic writer for {type(value).__name__}')


def validate_treatment(store):
    names = {canonical_key(key) for key in store.collection('swmm:pollutants')}
    def subject(key, path=('expression',)):
        return DiagnosticSubject(collection='swmm:treatment', key=key, path=path)
    for key,row in store.collection('swmm:treatment').items():
        if type(row) is not Treatment:
            yield Diagnostic(code='treatment.record_variant', object_id=str(key),
                subject=subject(key,()),
                message='An extension treatment record requires an explicit feature adapter')
    rows = {canonical_key(key):row for key,row in store.collection('swmm:treatment').items()
        if type(row) is Treatment and ValidationReport(diagnostics=tuple(validate_fields(row))).is_valid}
    dependencies = {}
    for key,row in rows.items():
        dependencies[key] = set()
        for item in walk_expression(row.expression):
            if type(item) in (TreatmentConcentration, TreatmentRemoval):
                token = variable_text(item)
                if store.contains(item.pollutant):
                    actual = resolve_variable(token, names)
                    if token.upper() in FUNCTIONS or type(actual) is not type(item) or actual.pollutant.canonical != item.pollutant.canonical:
                        yield Diagnostic(code='treatment.variable_shadowed', object_id=str(key),
                            subject=subject(key), related=(DiagnosticSubject(collection=item.pollutant.collection,
                                key=item.pollutant.key),) + ((DiagnosticSubject(collection=actual.pollutant.collection,
                                key=actual.pollutant.key),) if type(actual) in (TreatmentConcentration,TreatmentRemoval) else ()),
                            message=f'Native resolves {token!r} as another variable/function; rename the conflicting pollutant explicitly')
                if type(item) is TreatmentRemoval:
                    target = (key[0], canonical_key(item.pollutant.key))
                    if target in rows:
                        dependencies[key].add(target)
                    elif store.contains(item.pollutant):
                        yield Diagnostic(code='treatment.missing_removal', severity=Severity.WARNING, object_id=str(key),
                            subject=subject(key), related=(subject(target),
                                DiagnosticSubject(collection=item.pollutant.collection,key=item.pollutant.key)),
                            message=f'No treatment equation for {item.pollutant.key!r} at this node; its removal evaluates to zero')
            elif type(item) is TreatmentProcessVariable and item.name == 'HRT' and store.contains(row.node):
                if not isinstance(store.collection('swmm:nodes')[row.node.key], Storage):
                    yield Diagnostic(code='treatment.hrt_nonstorage', severity=Severity.WARNING, object_id=str(key),
                        subject=subject(key), related=(DiagnosticSubject(collection=row.node.collection,key=row.node.key),),
                        message='HRT is zero at non-storage nodes')
    # Iteratively eliminate leaves; remaining vertices contain or reach a
    # removal cycle. Concentration references do not recurse and are not edges.
    pending = Counter({key:len(value) for key,value in dependencies.items()})
    parents = {key:[] for key in rows}
    for key,values in dependencies.items():
        for child in values:
            parents[child].append(key)
    ready = deque(key for key,count in pending.items() if count == 0)
    while ready:
        key = ready.popleft()
        del pending[key]
        for parent in parents[key]:
            pending[parent] -= 1
            if pending[parent] == 0:
                ready.append(parent)
    for key in pending:
        yield Diagnostic(code='treatment.removal_cycle', object_id=str(key),
            subject=subject(key), related=tuple(subject(child) for child in sorted(dependencies[key]) if child in pending),
            message='Removal dependencies contain or reach a cycle; native execution cannot be relied upon to diagnose it')


def _map_expression(node, leaf):
    if type(node) is ExpressionNumber:
        return node
    if type(node) is UnaryExpression:
        return replace(node, operand=_map_expression(node.operand, leaf))
    if type(node) is BinaryExpression:
        return replace(node, left=_map_expression(node.left, leaf), right=_map_expression(node.right, leaf))
    if type(node) is FunctionExpression and node.function in FUNCTIONS:
        return replace(node, argument=_map_expression(node.argument, leaf))
    if type(node) in (TreatmentProcessVariable, TreatmentConcentration, TreatmentRemoval):
        return leaf(node)
    raise ValueError('Unknown treatment arithmetic requires an explicit unit transform')


def _scaled_variable(node, factor):
    return node if factor == 1 else BinaryExpression(operator='/', left=node, right=ExpressionNumber(value=factor))


def _convert_hydraulic(row, context):
    def leaf(node):
        if type(node) is not TreatmentProcessVariable:
            return node
        factor = (context.number(1., 'flow') if node.name == 'FLOW' else
            context.number(1., 'length') if node.name == 'DEPTH' else
            context.number(1., 'length')**2 if node.name == 'AREA' else 1.)
        return _scaled_variable(node, factor)
    return replace(row, expression=_map_expression(row.expression, leaf))


def _convert_concentration(row, context):
    def leaf(node):
        if type(node) is TreatmentConcentration and node.pollutant.canonical == context.target.canonical:
            return _scaled_variable(node, context.factor)
        return node
    expression = _map_expression(row.expression, leaf)
    if row.kind == 'C' and row.pollutant.canonical == context.target.canonical:
        expression = BinaryExpression(operator='*', left=ExpressionNumber(value=context.factor), right=expression)
    return PollutantUnitConversion(value=replace(row, expression=expression))


TREATMENT_TRANSFORMS = (UnitTransform(value_type=Treatment, convert=_convert_hydraulic),)
TREATMENT_POLLUTANT_TRANSFORMS = (PollutantUnitTransform(value_type=Treatment, convert=_convert_concentration),)
