"""E shared seasonal hydrograph with observable prior-gage activation."""
from dataclasses import replace
from datetime import date,time,timedelta
import re
from easysewer.io.inp import InpDocument
from easysewer.io.inp.network import default_schema
from easysewer.io.inp.rdii import RdiiCodec
from easysewer.model import Model,Ref
from easysewer.model import network as n,hydrology as h,controls as c
from easysewer.model.rdii import UnitHydrograph,HydrographResponse,RdiiInflow
from easysewer.model.resources import InlineTimeSeries,SeriesPoint
from easysewer.model.report import ReportSelection
from easysewer.scenario import ScenarioPatch,RenameRecord,SetFields,FieldChange
from easysewer.schema.structured import ModelSchema

def ref(collection,key):return Ref(collection='swmm:'+collection,key=key)

def schema(enabled=True):
    original,result=default_schema(),ModelSchema()
    for descriptor,codec in original.bindings:
        if descriptor.key!=RdiiCodec.descriptor.key:result.register(descriptor,codec)
    if enabled:result.register(RdiiCodec.descriptor,RdiiCodec())
    result.register_json(*original.json_types.declarations)
    return result

def responses():
    rows=(('ALL','SHORT',.12,.05,1,.02,.04,.01),
          ('ALL','MEDIUM',.08,.1,2,.02,.04,.01),
          ('ALL','LONG',.04,.2,3,.02,.04,.01),
          ('JAN','SHORT',.2,.05,1,None,None,None),
          ('FEB','MEDIUM',.16,.1,2,0,0,0),
          ('ALL','SHORT',.1,.05,1,None,None,None),
          ('FEB','SHORT',.3,.025,1,0,0,0))
    return tuple(HydrographResponse(month=m,response=k,fraction=r,time_to_peak=t,recession_ratio=b,
        maximum_abstraction=a,recovery_rate=d,initial_abstraction=i) for m,k,r,t,b,a,d,i in rows)

def model():
    value=Model(schema=schema())
    value.update_options(flow_units='CFS',flow_routing='DYNWAVE',start_date=date(2020,1,31),
        start_time=time(23,30),end_date=date(2020,2,1),end_time=time(1,30),
        report_step=timedelta(minutes=1),wet_step=timedelta(minutes=1),dry_step=timedelta(minutes=1),
        routing_step=timedelta(seconds=30),variable_step=0)
    value.update_report(nodes=ReportSelection(mode='ALL'),links=ReportSelection(mode='ALL'))
    for key in ('J','K'):
        value.nodes.add(n.Storage(id=key,elevation=0,max_depth=20,initial_depth=0,
            shape=n.FunctionalStorage(coefficient=0,exponent=1,constant=500)))
    for key in ('O','O2'):value.nodes.add(n.Outfall(id=key,elevation=-1,boundary=n.FreeBoundary()))
    for key,node,outfall in (('P','J','O'),('Q','K','O2')):
        value.links.add(n.Outlet(id=key,inlet=ref('nodes',node),outlet=ref('nodes',outfall),offset=0,
            rating=n.FunctionalRating(basis='HEAD',coefficient=1,exponent=1)))
    value.timeseries.add(InlineTimeSeries(id='Rain',points=tuple(SeriesPoint(time=timedelta(minutes=i),value=v)
        for i,v in ((0,1),(10,2),(20,0),(30,3),(40,1),(50,0),(120,0)))))
    for key in ('Earlier','R'):
        value.raingages.add(h.RainGage(id=key,form='INTENSITY',interval=timedelta(minutes=10),snow_factor=1,
            source=h.SeriesRainfall(series=ref('timeseries','Rain'))))
    value.hydrographs.add(UnitHydrograph(id='Shared',rain_gage=ref('raingages','R'),
        prior_rain_gages=(ref('raingages','Earlier'),),responses=responses()))
    for node,area in (('J',2),('K',3)):
        value.rdii.add(RdiiInflow(node=ref('nodes',node),hydrograph=ref('hydrographs','Shared'),sewer_area=area))
    value.controls.add(c.ControlRule(id='RainControl',conditions=(c.Condition(
        left=c.Attribute(object_type='GAGE',attribute='INTENSITY',target=ref('raingages','Earlier')),
        relation='>',right=c.Constant(value=.1)),),
        then_actions=(c.Action(target=ref('links','P'),object_type='OUTLET',setting=c.NumericSetting(value=.2)),),
        else_actions=(c.Action(target=ref('links','P'),object_type='OUTLET',setting=c.NumericSetting(value=1)),)))
    return value

ORACLE='''[OPTIONS]
FLOW_UNITS CFS
FLOW_ROUTING DYNWAVE
START_DATE 01/31/2020
START_TIME 23:30
END_DATE 02/01/2020
END_TIME 01:30
REPORT_STEP 00:01:00
WET_STEP 00:01:00
DRY_STEP 00:01:00
ROUTING_STEP 30
VARIABLE_STEP 0
[STORAGE]
J 0 20 0 FUNCTIONAL 0 1 500
K 0 20 0 FUNCTIONAL 0 1 500
[OUTFALLS]
O -1 FREE
O2 -1 FREE
[OUTLETS]
P J O 0 FUNCTIONAL/HEAD 1 1
Q K O2 0 FUNCTIONAL/HEAD 1 1
[TIMESERIES]
Rain 0 1
Rain 00:10 2
Rain 00:20 0
Rain 00:30 3
Rain 00:40 1
Rain 00:50 0
Rain 02:00 0
[RAINGAGES]
Earlier INTENSITY 00:10 1 TIMESERIES Rain
R INTENSITY 00:10 1 TIMESERIES Rain
[HYDROGRAPHS]
Shared Earlier
Shared R
Shared ALL .12 .05 1 .08 .1 2 .04 .2 3 .02 .04 .01
Shared JAN SHORT .2 .05 1
Shared FEB MEDIUM .16 .1 2 0 0 0
Shared ALL SHORT .1 .05 1
Shared FEB SHORT .3 .025 1 0 0 0
[RDII]
J Shared 2
K Shared 3
[CONTROLS]
RULE RainControl
IF GAGE Earlier INTENSITY > .1
THEN OUTLET P SETTING = .2
ELSE OUTLET P SETTING = 1
[REPORT]
NODES ALL
LINKS ALL
'''

def imported():return Model.from_document(InpDocument.from_text(ORACLE,source='e-original.inp'),schema=schema(),strict=True)

def state(value,*,relation_order=True):
    return tuple(tuple(sorted(getattr(value,c).items())) if c=='rdii' and not relation_order else tuple(getattr(value,c).items())
        for c in ('nodes','links','raingages','timeseries','hydrographs','rdii','controls'))

def edit_patch():
    return ScenarioPatch(operations=(
        *(RenameRecord(target=ref(c,o),new_id=n) for c,o,n in (
            ('hydrographs','Shared','Seasonal'),('raingages','Earlier','Activated'),('raingages','R','Gauge'),
            ('timeseries','Rain','Storm'),('nodes','J','Receiving'))),
        SetFields(target=ref('rdii','K'),changes=(FieldChange(name='sewer_area',value=4.),)),
        SetFields(target=ref('hydrographs','Seasonal'),changes=(FieldChange(name='responses',
            value=tuple(replace(r,fraction=.4) if i==6 else r for i,r in enumerate(responses()))),))))

def edited(value=None):return ScenarioPatch.from_json_document(edit_patch().to_json_document()).apply(value or imported()).model

def edited_oracle():
    raw=ORACLE
    for old,new in (('Shared','Seasonal'),('Earlier','Activated'),('R','Gauge'),('Rain','Storm'),('J','Receiving')):
        raw=re.sub(r'\b'+old+r'\b',new,raw)
    return raw.replace('K Seasonal 3','K Seasonal 4').replace('FEB SHORT .3','FEB SHORT .4')
