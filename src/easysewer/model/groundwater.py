"""Unconfined aquifers, local groundwater bindings and typed flow expressions."""

from dataclasses import dataclass, replace
from typing import Literal

from ..validation import Diagnostic, DiagnosticSubject, Severity, ValidationReport
from .expressions import (ExpressionNode, ExpressionNumber, UnaryExpression,
    BinaryExpression, FunctionExpression, FUNCTIONS, walk_expression)
from .fields import number, reference, validate_fields
from .identity import Ref
from .network import Entity
from .store import CollectionSpec
from .units import UnitTransform
from .usage import ResourceUse

VARIABLE_DIMENSIONS = {
    'HGW':'length', 'HSW':'length', 'HCB':'length', 'HGS':'length',
    'KS':'conductivity', 'K':'conductivity', 'THETA':'ratio', 'PHI':'ratio',
    'FI':'rain_intensity', 'FU':'rain_intensity', 'A':'catchment_area',
}


@dataclass(frozen=True, kw_only=True)
class Aquifer(Entity):
    porosity: float = number('ratio', positive=True, maximum=1)
    wilting_point: float = number('ratio', minimum=0, maximum=1)
    field_capacity: float = number('ratio', minimum=0, maximum=1)
    conductivity: float = number('conductivity', positive=True)
    conductivity_slope: float = number('ratio', minimum=0)
    # The fixed solver divides Tslp by UCF(LENGTH), despite the manual's
    # in/mm label. Model stores the numeric input with native length meaning.
    tension_slope: float = number('length', minimum=0)
    upper_evaporation_fraction: float = number('ratio', minimum=0, maximum=1)
    lower_evaporation_depth: float = number('depth', minimum=0)
    deep_seepage: float = number('rain_intensity', minimum=0)
    bottom_elevation: float = number('elevation')
    water_table_elevation: float = number('elevation')
    upper_moisture: float = number('ratio', minimum=0, maximum=1)
    evaporation_pattern: Ref | None = reference('swmm:patterns', None)

    def validate_local(self):
        if not self.wilting_point < self.field_capacity < self.porosity:
            yield Diagnostic(code='groundwater.soil_order', message='Wilting point < field capacity < porosity is required')
        if not self.wilting_point <= self.upper_moisture <= self.porosity:
            yield Diagnostic(code='groundwater.moisture', message='Initial moisture must be between wilting point and porosity')
        if self.water_table_elevation < self.bottom_elevation:
            yield Diagnostic(code='groundwater.water_table', message='Water table cannot be below the aquifer bottom')


@dataclass(frozen=True, kw_only=True)
class Groundwater:
    subcatchment: Ref = reference('swmm:subcatchments')
    aquifer: Ref = reference('swmm:aquifers')
    node: Ref = reference('swmm:nodes')
    surface_elevation: float = number('elevation')
    groundwater_coefficient: float = number('groundwater_coefficient')
    groundwater_exponent: float = number('ratio')
    surface_water_coefficient: float = number('groundwater_coefficient')
    surface_water_exponent: float = number('ratio')
    interaction_coefficient: float = number('groundwater_interaction_coefficient')
    fixed_surface_depth: float = number('depth', minimum=0)
    threshold_elevation: float | None = number('elevation', None)
    bottom_elevation: float | None = number('elevation', None)
    water_table_elevation: float | None = number('elevation', None)
    upper_moisture: float | None = number('ratio', None, minimum=0, maximum=1)


@dataclass(frozen=True, kw_only=True)
class GroundwaterVariable(ExpressionNode):
    name: Literal['HGW','HSW','HCB','HGS','KS','K','THETA','PHI','FI','FU','A']


@dataclass(frozen=True, kw_only=True)
class GroundwaterExpression:
    subcatchment: Ref = reference('swmm:subcatchments')
    kind: Literal['LATERAL','DEEP']
    expression: ExpressionNode

    def validate_local(self):
        supported = (ExpressionNumber, UnaryExpression, BinaryExpression, FunctionExpression, GroundwaterVariable)
        for node in walk_expression(self.expression):
            if type(node) not in supported:
                yield Diagnostic(code='groundwater.expression_variant', message='Unsupported groundwater arithmetic node')
            if type(node) is FunctionExpression and node.function not in FUNCTIONS:
                yield Diagnostic(code='groundwater.expression_function', message='Unsupported native arithmetic function')


GROUNDWATER_COLLECTIONS = (
    CollectionSpec(key='swmm:aquifers', record_type=Aquifer, key_of=lambda row:row.id, identity_field='id', validate=validate_fields),
    CollectionSpec(key='swmm:groundwater', record_type=Groundwater, key_of=lambda row:row.subcatchment.key, validate=validate_fields),
    CollectionSpec(key='swmm:gwf', record_type=GroundwaterExpression, key_of=lambda row:(row.subcatchment.key,row.kind), validate=validate_fields),
)


def groundwater_resource_uses(store):
    for key,row in store.collection('swmm:aquifers').items():
        if ValidationReport(diagnostics=tuple(validate_fields(row))).is_valid and row.evaporation_pattern is not None:
            yield ResourceUse(owner=Ref(collection='swmm:aquifers',key=key),target=row.evaporation_pattern,
                path=('evaporation_pattern',),role='upper-zone evaporation adjustment',accepted_kinds=('MONTHLY',))


def validate_groundwater(store,profile):
    from .options import get_options
    from .units import UnitContext
    options=get_options(store)
    if not ValidationReport(diagnostics=tuple(validate_fields(options))).is_valid:
        return
    units=options.flow_units or profile.option_default('flow_units')
    native_length=UnitContext().convert(1.,dimension='length',to=UnitContext(flow_units=units),rules=profile.unit_rules)
    for key,row in store.collection('swmm:groundwater').items():
        if not ValidationReport(diagnostics=tuple(validate_fields(row))).is_valid or not store.contains(row.aquifer):
            continue
        def local(name):
            return DiagnosticSubject(collection='swmm:groundwater', key=key, path=(name,))

        def inherited(*names):
            return tuple(DiagnosticSubject(collection=row.aquifer.collection, key=row.aquifer.key, path=(name,))
                         for name in names if getattr(row, name) is None)
        for name in ('threshold_elevation','bottom_elevation','water_table_elevation','upper_moisture'):
            value=getattr(row,name)
            if value is not None and value/(1. if name=='upper_moisture' else native_length)==-1.e10:
                yield Diagnostic(code='groundwater.native_missing_value',object_id=key,field=name,
                    subject=local(name), related=(DiagnosticSubject(collection='swmm:options', key='settings', path=('flow_units',)),),
                    message='This numeric value is a native missing sentinel; use None explicitly')
        aquifer=store.collection('swmm:aquifers')[row.aquifer.key]
        if not ValidationReport(diagnostics=tuple(validate_fields(aquifer))).is_valid:
            continue
        effective=lambda name:getattr(row,name) if getattr(row,name) is not None else getattr(aquifer,name)
        bottom,table,moisture=map(effective,('bottom_elevation','water_table_elevation','upper_moisture'))
        if not bottom <= table <= row.surface_elevation or bottom >= row.surface_elevation:
            yield Diagnostic(code='groundwater.local_elevations',object_id=key,
                subject=local('surface_elevation'),
                related=(local('bottom_elevation'), local('water_table_elevation')) + inherited('bottom_elevation','water_table_elevation'),
                message='Local water table must be between bottom and surface, with positive total aquifer depth')
        if not aquifer.wilting_point <= moisture <= aquifer.porosity:
            yield Diagnostic(code='groundwater.local_moisture',object_id=key,message='Local initial moisture must be between aquifer wilting point and porosity',
                subject=local('upper_moisture'), related=inherited('upper_moisture') + tuple(
                    DiagnosticSubject(collection=row.aquifer.collection, key=row.aquifer.key, path=(name,))
                    for name in ('wilting_point','porosity')))
        if table==row.surface_elevation or moisture==aquifer.porosity:
            yield Diagnostic(code='groundwater.initial_clamp',object_id=key,severity=Severity.WARNING,
                subject=DiagnosticSubject(collection='swmm:groundwater', key=key),
                related=(local('surface_elevation'), local('water_table_elevation'), local('upper_moisture'),
                    DiagnosticSubject(collection=row.aquifer.collection, key=row.aquifer.key, path=('porosity',))) +
                    inherited('water_table_elevation','upper_moisture'),
                message='Native initialization moves a fully saturated zone slightly below its boundary')
    for row in store.collection('swmm:gwf').values():
        if ValidationReport(diagnostics=tuple(validate_fields(row))).is_valid and row.subcatchment.key not in store.collection('swmm:groundwater'):
            yield Diagnostic(code='groundwater.inactive_expression',object_id=row.subcatchment.key,severity=Severity.WARNING,
                subject=DiagnosticSubject(collection='swmm:gwf', key=(row.subcatchment.key,row.kind), path=('expression',)),
                related=(DiagnosticSubject(collection='swmm:groundwater', key=row.subcatchment.key),),
                message='Groundwater flow expression has no groundwater binding and is inactive')


def _convert_binding(row,context):
    length=context.number(1.,'length'); flux=context.number(1.,'groundwater_flux')
    fields={name:context.number(getattr(row,name),'length') for name in
        ('surface_elevation','fixed_surface_depth','threshold_elevation','bottom_elevation','water_table_elevation') if getattr(row,name) is not None}
    fields.update(groundwater_coefficient=row.groundwater_coefficient*flux/length**row.groundwater_exponent,
        surface_water_coefficient=row.surface_water_coefficient*flux/length**row.surface_water_exponent,
        interaction_coefficient=row.interaction_coefficient*flux/length**2)
    return replace(row,**fields)


def _convert_expression(row,context):
    if context.source.system==context.target.system:
        return row
    # Preserve arbitrary empirical arithmetic by evaluating each new variable
    # in its old units, then converting the result. No coefficient inference.
    def convert(node):
        if type(node) is GroundwaterVariable:
            factor=context.number(1.,VARIABLE_DIMENSIONS[node.name])
            return node if factor==1 else BinaryExpression(operator='/',left=node,right=ExpressionNumber(value=factor))
        if type(node) is ExpressionNumber:
            return node
        if type(node) is UnaryExpression:
            return replace(node,operand=convert(node.operand))
        if type(node) is BinaryExpression:
            return replace(node,left=convert(node.left),right=convert(node.right))
        if type(node) is FunctionExpression:
            return replace(node,argument=convert(node.argument))
        raise ValueError('Unknown groundwater expression variant requires an explicit unit transform')
    result=convert(row.expression)
    factor=context.number(1.,'groundwater_flux' if row.kind=='LATERAL' else 'rain_intensity')
    if factor!=1:
        result=BinaryExpression(operator='*',left=ExpressionNumber(value=factor),right=result)
    return replace(row,expression=result)


GROUNDWATER_TRANSFORMS = (
    UnitTransform(value_type=Groundwater,convert=_convert_binding),
    UnitTransform(value_type=GroundwaterExpression,convert=_convert_expression),
)
