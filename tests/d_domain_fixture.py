"""D acceptance: shared aquifer, local overrides and dependent C/R treatment."""
from datetime import date, time, timedelta
import re

from easysewer.io.inp import InpDocument
from easysewer.io.inp.network import default_schema
from easysewer.io.inp.groundwater import GroundwaterCodec, GroundwaterExpressionCodec
from easysewer.io.inp.treatment import TreatmentCodec, TreatmentExpressionCodec
from easysewer.model import Model, Ref
from easysewer.model import geometry as g, hydrology as h, network as n, groundwater as gw
from easysewer.model.inflows import FlowInflow, ConcentrationInflow
from easysewer.model.quality import Pollutant
from easysewer.model.treatment import Treatment
from easysewer.model.report import ReportSelection
from easysewer.model.resources import Pattern, InlineTimeSeries, SeriesPoint
from easysewer.scenario import ScenarioPatch, RenameRecord, SetFields, FieldChange
from easysewer.schema.structured import ModelSchema


def ref(collection,key):
    return Ref(collection='swmm:'+collection,key=key)


def schema(enabled=True):
    original,result=default_schema(),ModelSchema()
    keys={GroundwaterCodec.descriptor.key,TreatmentCodec.descriptor.key}
    for descriptor,codec in original.bindings:
        if descriptor.key not in keys:result.register(descriptor,codec)
    if enabled:
        for cls in (TreatmentCodec,GroundwaterCodec):result.register(cls.descriptor,cls())
    result.register_json(*original.json_types.declarations)
    return result


def treatment(text, names=('A','B')):
    return TreatmentExpressionCodec().parse(text,pollutants=names)


def model():
    value=Model(schema=schema())
    value.update_options(flow_units='CFS',flow_routing='DYNWAVE',infiltration='HORTON',
        start_date=date(2020,1,1),end_date=date(2020,1,1),start_time=time(),end_time=time(0,30),
        report_step=timedelta(seconds=60),wet_step=timedelta(seconds=30),dry_step=timedelta(seconds=30),
        routing_step=timedelta(seconds=5),variable_step=0)
    value.update_report(subcatchments=ReportSelection(mode='ALL'),nodes=ReportSelection(mode='ALL'),links=ReportSelection(mode='ALL'))
    value.nodes.add(n.Storage(id='J',elevation=1,max_depth=10,initial_depth=1,
        shape=n.FunctionalStorage(coefficient=0,exponent=1,constant=500)))
    value.nodes.add(n.Outfall(id='O',elevation=0,boundary=n.FreeBoundary()))
    value.links.add(n.Conduit(id='P',inlet=ref('nodes','J'),outlet=ref('nodes','O'),length=50,roughness=.013,
        inlet_offset=0,outlet_offset=0,section=g.CrossSection(geometry=g.Circular(diameter=2))))
    value.timeseries.add(InlineTimeSeries(id='Rain',points=tuple(SeriesPoint(time=timedelta(minutes=i),value=v)
        for i,v in ((0,2),(10,3),(20,1),(30,0)))))
    value.raingages.add(h.RainGage(id='R',form='INTENSITY',interval=timedelta(minutes=10),snow_factor=1,
        source=h.SeriesRainfall(series=ref('timeseries','Rain'))))
    for key in ('S','S2'):
        value.subcatchments.add(h.Subcatchment(id=key,rain_gage=ref('raingages','R'),outlet=ref('nodes','J'),
            area=2,impervious_percent=30,width=100,slope=1,curb_length=0,
            subareas=h.Subareas(impervious_roughness=.01,pervious_roughness=.2,impervious_storage=.05,
                pervious_storage=.1,zero_storage_percent=25,route_to='OUTLET'),
            infiltration=h.Infiltration(parameters=h.Horton(maximum_rate=3,minimum_rate=.2,decay=4,drying_time=2,maximum_volume=0))))
    value.patterns.add(Pattern(id='ET',kind='MONTHLY',factors=(.5,1.5)))
    value.aquifers.add(gw.Aquifer(id='Soil',porosity=.45,wilting_point=.1,field_capacity=.25,conductivity=.2,
        conductivity_slope=10,tension_slope=15,upper_evaporation_fraction=.5,lower_evaporation_depth=5,
        deep_seepage=.001,bottom_elevation=-10,water_table_elevation=2,upper_moisture=.3,
        evaporation_pattern=ref('patterns','ET')))
    codec=GroundwaterExpressionCodec()
    for key in ('S','S2'):
        overrides={} if key=='S' else dict(threshold_elevation=0,bottom_elevation=-12,water_table_elevation=3,upper_moisture=.35)
        value.groundwater.add(gw.Groundwater(subcatchment=ref('subcatchments',key),aquifer=ref('aquifers','Soil'),
            node=ref('nodes','J'),surface_elevation=20,groundwater_coefficient=.001,groundwater_exponent=1.2,
            surface_water_coefficient=.0001,surface_water_exponent=1.1,interaction_coefficient=.00001,
            fixed_surface_depth=0,**overrides))
        lateral='0.002 * (HGW - HCB) + 0.0001 * HSW' if key=='S' else '0.003 * (HGW - HCB)'
        deep='0.001 * (HGW / HGS)' if key=='S' else '0.002 * (HGW / HGS)'
        for kind,text in (('LATERAL',lateral),('DEEP',deep)):
            value.gwf.add(gw.GroundwaterExpression(subcatchment=ref('subcatchments',key),kind=kind,expression=codec.parse(text)))
    value.inflows.add(FlowInflow(node=ref('nodes','J'),baseline=.05,scale_factor=1))
    for key,units,concentration,base in (('A','MG/L',20,10),('B','UG/L',100,200)):
        value.pollutants.add(Pollutant(id=key,units=units,rainfall_concentration=0,groundwater_concentration=concentration,
            rdii_concentration=0,decay_rate=0))
        value.inflows.add(ConcentrationInflow(node=ref('nodes','J'),constituent=ref('pollutants',key),baseline=base,scale_factor=1))
    value.treatment.add(Treatment(node=ref('nodes','J'),pollutant=ref('pollutants','A'),kind='C',expression=treatment('A * EXP(-.1 * HRT)')))
    value.treatment.add(Treatment(node=ref('nodes','J'),pollutant=ref('pollutants','B'),kind='R',expression=treatment('.2 * R_A + 0 * B')))
    return value


ORACLE='''[OPTIONS]
FLOW_UNITS CFS
FLOW_ROUTING DYNWAVE
INFILTRATION HORTON
START_DATE 01/01/2020
START_TIME 00:00
END_DATE 01/01/2020
END_TIME 00:30
REPORT_STEP 00:01:00
WET_STEP 00:00:30
DRY_STEP 00:00:30
ROUTING_STEP 5
VARIABLE_STEP 0
[STORAGE]
J 1 10 1 FUNCTIONAL 0 1 500
[OUTFALLS]
O 0 FREE
[CONDUITS]
P J O 50 .013 0 0
[XSECTIONS]
P CIRCULAR 2 0 0 0
[TIMESERIES]
Rain 0 2
Rain 00:10 3
Rain 00:20 1
Rain 00:30 0
[RAINGAGES]
R INTENSITY 00:10 1 TIMESERIES Rain
[SUBCATCHMENTS]
S R J 2 30 100 1 0
S2 R J 2 30 100 1 0
[SUBAREAS]
S .01 .2 .05 .1 25 OUTLET
S2 .01 .2 .05 .1 25 OUTLET
[INFILTRATION]
S 3 .2 4 2 0
S2 3 .2 4 2 0
[PATTERNS]
ET MONTHLY .5 1.5
[AQUIFERS]
Soil .45 .1 .25 .2 10 15 .5 5 .001 -10 2 .3 ET
[GROUNDWATER]
S Soil J 20 .001 1.2 .0001 1.1 .00001 0 *
S2 Soil J 20 .001 1.2 .0001 1.1 .00001 0 0 -12 3 .35
[GWF]
S LATERAL 0.002 * (HGW - HCB) + 0.0001 * HSW
S DEEP 0.001 * (HGW / HGS)
S2 LATERAL 0.003 * (HGW - HCB)
S2 DEEP 0.002 * (HGW / HGS)
[POLLUTANTS]
A MG/L 0 20 0 0
B UG/L 0 100 0 0
[INFLOWS]
J FLOW "" FLOW 1 1 .05
J A "" CONCEN 1 1 10
J B "" CONCEN 1 1 200
[TREATMENT]
J A C=A * EXP(-.1 * HRT)
J B R=.2 * R_A + 0 * B
[REPORT]
SUBCATCHMENTS ALL
NODES ALL
LINKS ALL
'''


def imported():
    return Model.from_document(InpDocument.from_text(ORACLE,source='d-original.inp'),schema=schema(),strict=True)


def state(value, *, relation_order=True):
    collections=('nodes','links','subcatchments','raingages','timeseries',
        'patterns','aquifers','groundwater','gwf','pollutants','inflows','treatment')
    # Source-preserving edits may relocate renamed keyed relation rows. These
    # three sections assign by object ID, not by row index. Model/JSON order is
    # checked separately; engine identity collections always retain their order.
    return tuple(tuple(sorted(getattr(value,c).items())) if not relation_order and c in ('groundwater','gwf','treatment')
        else tuple(getattr(value,c).items()) for c in collections)


def edit_patch():
    return ScenarioPatch(operations=(
        *(RenameRecord(target=ref(c,old),new_id=new) for c,old,new in
          (('aquifers','Soil','SharedSoil'),('subcatchments','S','Basin'),('nodes','J','Tank'),
           ('patterns','ET','Monthly'),('pollutants','A','Solids'),('pollutants','B','Tracer'))),
        SetFields(target=ref('aquifers','SharedSoil'),changes=(FieldChange(name='conductivity',value=.4),)),
        SetFields(target=ref('groundwater','S2'),changes=(FieldChange(name='water_table_elevation',value=4),)),
        SetFields(target=ref('gwf',('S2','LATERAL')),changes=(FieldChange(name='expression',
            value=GroundwaterExpressionCodec().parse('0.006 * (HGW - HCB)')),)),
        SetFields(target=ref('treatment',('Tank','Solids')),changes=(FieldChange(name='expression',
            value=treatment('Solids * EXP(-.2 * HRT)',('Solids','Tracer'))),)),
    ))


def edited(value=None):
    return ScenarioPatch.from_json_document(edit_patch().to_json_document()).apply(value or imported()).model


def edited_oracle():
    raw=ORACLE
    for old,new in (('Soil','SharedSoil'),('S','Basin'),('J','Tank'),('ET','Monthly'),('A','Solids'),('B','Tracer')):
        raw=re.sub(r'\b'+old+r'\b',new,raw)
    return raw.replace('R_A','R_Solids').replace('.25 .2 10','.25 .4 10').replace('0 0 -12 3 .35','0 0 -12 4 .35').replace(
        '0.003 * (HGW - HCB)','0.006 * (HGW - HCB)').replace('EXP(-.1 * HRT)','EXP(-.2 * HRT)')
