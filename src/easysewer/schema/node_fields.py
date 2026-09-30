"""Configured storage/divider/outfall facts, distinct from live node state."""

from dataclasses import fields
import math

from ..model import network as n
from ..model.fields import validate_fields
from ..model.identity import Ref
from ..model.inspection import FieldFact, FieldSemantics
from ..model.options import get_options
from ..model.resources import InlineTimeSeries, FileTimeSeries
from ..model.units import UnitContext
from ..model.values import Point
from ..validation import Diagnostic, ValidationReport
from .display_fields import coordinate_unit
from .field_contracts import FieldRule
from .geometry_profile import known, invalid, valid
from .network_fields import node_depth, initial_depth_fact
from .regulator_fields import _curve

SHAPES = (n.FunctionalStorage,n.TabularStorage,n.CylindricalStorage,n.ConicalStorage,
          n.ParaboloidStorage,n.PyramidalStorage)
BOUNDARIES = (n.FreeBoundary,n.NormalBoundary,n.FixedBoundary,n.TidalBoundary,n.SeriesBoundary)
LAWS = (n.OverflowDivider,n.CutoffDivider,n.TabularDivider,n.WeirDivider)
SEEPAGE = (n.ConstantSeepage,n.Seepage)


def na(reason):
    return FieldFact(status='not_applicable',reason=reason)


def reference(store, ref):
    if not isinstance(ref,Ref) or not store.contains(ref):
        return invalid('Missing referenced object')
    if not valid(store.collection(ref.collection)[ref.key]):
        return invalid('Invalid referenced object')
    return known(ref,'Configured reference, without external I/O or time-dependent lookup')


def shape_fact(context):
    shape=context.record.shape
    if type(shape) not in SHAPES:
        return FieldFact(reason='Extension storage shape requires its own contract')
    if type(shape) is n.TabularStorage:
        fact=_curve(context.store,shape.curve,('STORAGE',))
        if fact.status!='known':return fact
        curve=context.store.collection(shape.curve.collection)[shape.curve.key]
        if any(p.x<0 or p.y<0 for p in curve.points):
            return invalid('Storage curve depths and areas must be nonnegative')
    if type(shape) is n.FunctionalStorage and shape.exponent<0:
        return invalid('Negative storage exponent is singular at zero depth')
    return known(shape,'Configured storage relation, not instantaneous area or volume')


def boundary_fact(context):
    boundary=context.record.boundary
    if type(boundary) not in BOUNDARIES:
        return FieldFact(reason='Extension boundary requires its own contract')
    if type(boundary) is n.TidalBoundary:
        fact=_curve(context.store,boundary.curve,('TIDAL',))
        if fact.status!='known':return fact
    elif type(boundary) is n.SeriesBoundary:
        fact=reference(context.store,boundary.series)
        if fact.status!='known':return fact
        series=context.store.collection(boundary.series.collection)[boundary.series.key]
        if type(series) not in (InlineTimeSeries,FileTimeSeries):
            return FieldFact(reason='Extension time series requires its own boundary contract')
    return known(boundary,'Configured boundary, not instantaneous outlet depth or stage')


def diversion_link(context, routing):
    ref=context.record.diverted_link
    fact=reference(context.store,ref)
    if fact.status!='known':return fact
    link=context.store.collection(ref.collection)[ref.key]
    if context.owner.canonical not in (link.inlet.canonical,link.outlet.canonical):
        return invalid('Diverted link is not connected to its divider')
    if routing!='DYNWAVE' and context.owner.canonical!=link.inlet.canonical:
        return invalid('Active diversion requires a link leaving the divider')
    return fact


def law_fact(context, routing):
    law=context.record.law
    if type(law) not in LAWS:
        return FieldFact(reason='Extension divider law requires its own contract')
    if type(law) is n.TabularDivider:
        fact=_curve(context.store,law.curve,('DIVERSION',))
        if fact.status!='known':return fact
    elif type(law) is n.WeirDivider:
        try:maximum=law.coefficient*law.height**1.5
        except OverflowError:maximum=math.inf
        if not math.isfinite(maximum) or law.minimum_flow>maximum:
            return invalid('Divider minimum flow exceeds finite full-weir flow C * H**1.5')
    if routing=='DYNWAVE':
        return na('Dynamic wave treats a divider as a junction; diversion law is inactive')
    return known(law,'Configured diversion law, not instantaneous diverted flow')


def node_field(context):
    root,container,name,value=context.record,context.container,context.field,context.value
    options=get_options(context.store)
    issues=tuple(validate_fields(root))+tuple(validate_fields(options))
    if not ValidationReport(diagnostics=issues).is_valid:
        return FieldSemantics(effective=invalid('Invalid node or options'),diagnostics=issues)
    units=UnitContext(flow_units=options.flow_units or context.profile.option_default('flow_units'))
    routing=options.flow_routing or context.profile.option_default('flow_routing')
    mode=options.link_offsets or context.profile.option_default('link_offsets')
    default=FieldFact(status='required')
    unit=na('Non-numeric declaration')
    effective=known(value,'Configured value, not a live simulation observation')
    dimension=next(f for f in fields(container) if f.name==name).metadata.get('dimension')
    if dimension=='storage_coefficient':
        exponent=container.exponent
        unit=known('1' if exponent==2 else units.unit('length') if exponent==1 else units.unit('area') if exponent==0 else f'{units.unit("area")}/{units.unit("length")}^{exponent}',
                   'Area divided by depth raised to the storage exponent')
    elif dimension=='divider_weir_coefficient':
        unit=known(f'{units.flow_units}/{units.unit("length")}^1.5')
    elif dimension and dimension!='map_coordinate':
        unit=known(units.unit(dimension))
    if type(container) is Point or container is root and name in ('position','polygon'):
        unit,map_issues=coordinate_unit(context);issues+=map_issues
        if name=='position':default=known(None)
        elif name=='polygon':default=known(())
        if unit.status=='invalid':effective=invalid('Invalid explicit map context')
    elif type(container) is Ref:
        if name=='collection':default=na('Reference namespace is fixed by the grammar')
        fact=reference(context.store,container)
        if fact.status!='known':effective=fact
    elif type(root) is n.Storage:
        if container is root:
            if name in ('surcharge_depth','evaporation_fraction','seepage'):
                default=known(None if name=='seepage' else 0.)
                effective=known(default.value if value is None else value)
            if name=='max_depth':effective=node_depth(context,mode)
            elif name=='initial_depth':
                effective=initial_depth_fact(context,mode)
            elif name=='shape':effective=shape_fact(context)
            elif name=='seepage' and value is not None:
                if type(value) not in SEEPAGE:effective=FieldFact(reason='Extension seepage requires its own contract')
                elif value.conductivity==0:effective=known(None,'Zero conductivity disables native exfiltration')
        elif type(container) in SHAPES:
            fact=shape_fact(context)
            if fact.status!='known':effective=fact
            elif type(container) is n.TabularStorage:effective=_curve(context.store,value,('STORAGE',))
        elif type(container) in SEEPAGE and name!='conductivity' and container.conductivity==0:
            effective=na('Zero conductivity disables native exfiltration')
    elif type(root) is n.Divider:
        if container is root:
            if name in ('max_depth','initial_depth','surcharge_depth','ponded_area'):
                default=known(0.);effective=known(0. if value is None else value)
            if name=='max_depth':effective=node_depth(context,mode)
            elif name=='initial_depth':
                effective=initial_depth_fact(context,mode)
            elif name=='ponded_area' and not (options.allow_ponding or context.profile.option_default('allow_ponding')):
                effective=na('Surface ponding is disabled')
            elif name=='diverted_link':
                default=known(None,'Omitted diversion link marker; native validation still requires an attached link')
                effective=diversion_link(context,routing)
                if effective.status=='known' and routing=='DYNWAVE':effective=na('Diversion is inactive during dynamic-wave routing')
            elif name=='law':effective=law_fact(context,routing)
        elif type(container) in LAWS:
            fact=law_fact(context,routing)
            if fact.status!='known':effective=fact
            elif type(container) is n.TabularDivider:effective=_curve(context.store,value,('DIVERSION',))
    elif type(root) is n.Outfall:
        if container is root:
            if name in ('gated','route_to'):
                default=known(False if name=='gated' else None)
                effective=known(default.value if value is None else value)
            if name=='boundary':effective=boundary_fact(context)
            elif name=='route_to' and value is not None:effective=reference(context.store,value)
        elif type(container) in BOUNDARIES:
            fact=boundary_fact(context)
            if fact.status!='known':effective=fact
            elif type(container) is n.TidalBoundary:effective=_curve(context.store,value,('TIDAL',))
            elif type(container) is n.SeriesBoundary:effective=reference(context.store,value)
    if effective.status=='invalid':
        issues+=(Diagnostic(code='node.field_context',object_id=root.id,field='.'.join(map(str,context.path)),message=effective.reason),)
    return FieldSemantics(unit=unit,default=default,effective=effective,diagnostics=issues)


NODE_FIELD_RULES=tuple(FieldRule(value_type=kind,field=f.name,root_type=root,resolve=node_field)
    for root,kinds in ((n.Storage,SHAPES+SEEPAGE),(n.Divider,LAWS),(n.Outfall,BOUNDARIES))
    for kind in (root,Ref,Point,*kinds) for f in fields(kind))
