"""C acceptance: two layer combinations and four separately owned placements."""
from dataclasses import replace
from datetime import date, time, timedelta

from easysewer.io.inp import InpDocument
from easysewer.io.inp.lid import LidCodec
from easysewer.io.inp.network import default_schema
from easysewer.model import Model, Ref, FileReference
from easysewer.model import geometry as g, hydrology as h, network as n, lid as l
from easysewer.model.report import ReportSelection
from easysewer.model.resources import Curve, CurvePoint, InlineTimeSeries, SeriesPoint
from easysewer.scenario import ScenarioPatch, RenameRecord, SetFields, FieldChange, MoveRecord
from easysewer.schema.structured import ModelSchema

REPORTS = ('bio one.txt', 'bio two.txt', 'green.txt', 'bio other.txt')


def ref(collection, key):
    return Ref(collection='swmm:'+collection, key=key)


def schema(with_lid=True):
    original, result = default_schema(), ModelSchema()
    for descriptor, codec in original.bindings:
        if descriptor.key != LidCodec.descriptor.key:
            result.register(descriptor, codec)
    if with_lid:
        result.register(LidCodec.descriptor, LidCodec())
    result.register_json(*original.json_types.declarations)
    return result


def model():
    value = Model(schema=schema())
    value.update_options(flow_units='CFS', flow_routing='DYNWAVE', infiltration='HORTON',
        start_date=date(2020, 1, 1), end_date=date(2020, 1, 1), start_time=time(), end_time=time(0, 15),
        report_step=timedelta(seconds=30), wet_step=timedelta(seconds=30), dry_step=timedelta(seconds=30),
        routing_step=timedelta(seconds=5), variable_step=0)
    value.update_report(subcatchments=ReportSelection(mode='ALL'), nodes=ReportSelection(mode='ALL'), links=ReportSelection(mode='ALL'))
    value.nodes.add(n.Junction(id='J', elevation=1, max_depth=5, initial_depth=0))
    value.nodes.add(n.Outfall(id='O', elevation=0, boundary=n.FreeBoundary()))
    value.links.add(n.Conduit(id='P', inlet=ref('nodes', 'J'), outlet=ref('nodes', 'O'), length=50, roughness=.013,
        inlet_offset=0, outlet_offset=0, section=g.CrossSection(geometry=g.Circular(diameter=2))))
    value.timeseries.add(InlineTimeSeries(id='Rain', points=tuple(SeriesPoint(time=timedelta(minutes=i), value=v)
        for i,v in ((0,2), (5,3), (10,1), (15,0)))))
    value.raingages.add(h.RainGage(id='R', form='INTENSITY', interval=timedelta(minutes=5), snow_factor=1,
        source=h.SeriesRainfall(series=ref('timeseries','Rain'))))
    for key in ('S', 'S2'):
        value.subcatchments.add(h.Subcatchment(id=key, rain_gage=ref('raingages','R'), outlet=ref('nodes','J'),
            area=2, impervious_percent=30, width=100, slope=1, curb_length=0,
            subareas=h.Subareas(impervious_roughness=.01, pervious_roughness=.2, impervious_storage=.05,
                pervious_storage=.1, zero_storage_percent=25, route_to='OUTLET'),
            infiltration=h.Infiltration(parameters=h.Horton(maximum_rate=3, minimum_rate=.2, decay=4,
                drying_time=2, maximum_volume=0))))
    surface = l.LidSurface(storage_depth=3, vegetation_fraction=.1, roughness=.1, slope=2, side_slope=2)
    soil = l.LidSoil(thickness=12, porosity=.5, field_capacity=.2, wilting_point=.1, conductivity=1, conductivity_slope=5, suction=3)
    value.curves.add(Curve(id='Head', kind='CONTROL', points=(CurvePoint(x=0,y=.5), CurvePoint(x=36,y=1))))
    value.lid_controls.add(l.LidControl(id='Bio', kind='BC', surface=surface, soil=soil,
        storage=l.LidStorage(thickness=12, void_ratio=.5, seepage_rate=.1, clogging_factor=10, covered=False),
        drain=l.LidDrain(coefficient=.2, exponent=.5, offset=1, delay=0, open_head=2, close_head=.5,
            curve=ref('curves','Head'))))
    value.lid_controls.add(l.LidControl(id='Green', kind='GR', surface=surface, soil=soil,
        drain_mat=l.LidDrainMat(thickness=2, void_fraction=.6, roughness=.1)))
    for i,(catchment,control,area,saturation) in enumerate((('S','Bio',100,50), ('S','Bio',200,75),
        ('S2','Green',150,25), ('S2','Bio',80,50)),1):
        value.lid_usage.add(l.LidUsage(record_id=f'lid-usage-{i}', subcatchment=ref('subcatchments',catchment),
            control=ref('lid_controls',control), number=2, area=area, width=10, initial_saturation=saturation,
            from_impervious=20, to_pervious=False, from_pervious=10,
            drain_to=ref('nodes','J') if control=='Bio' else None,
            report_file=FileReference(path=REPORTS[i-1], direction='output')))
    return value


ORACLE = '''[OPTIONS]
FLOW_UNITS CFS
FLOW_ROUTING DYNWAVE
INFILTRATION HORTON
START_DATE 01/01/2020
START_TIME 00:00
END_DATE 01/01/2020
END_TIME 00:15
REPORT_STEP 00:00:30
WET_STEP 00:00:30
DRY_STEP 00:00:30
ROUTING_STEP 5
VARIABLE_STEP 0
[JUNCTIONS]
J 1 5 0
[OUTFALLS]
O 0 FREE
[CONDUITS]
P J O 50 .013 0 0
[XSECTIONS]
P CIRCULAR 2 0 0 0
[TIMESERIES]
Rain 0 2
Rain 00:05 3
Rain 00:10 1
Rain 00:15 0
[RAINGAGES]
R INTENSITY 00:05 1 TIMESERIES Rain
[SUBCATCHMENTS]
S R J 2 30 100 1 0
S2 R J 2 30 100 1 0
[SUBAREAS]
S .01 .2 .05 .1 25 OUTLET
S2 .01 .2 .05 .1 25 OUTLET
[INFILTRATION]
S 3 .2 4 2 0
S2 3 .2 4 2 0
[CURVES]
Head CONTROL 0 .5
Head 36 1
[LID_CONTROLS]
Bio BC
Bio SURFACE 3 .1 .1 2 2
Bio SOIL 12 .5 .2 .1 1 5 3
Bio STORAGE 12 .5 .1 10 NO
Bio DRAIN .2 .5 1 0 2 .5 Head
Green GR
Green SURFACE 3 .1 .1 2 2
Green SOIL 12 .5 .2 .1 1 5 3
Green DRAINMAT 2 .6 .1
[LID_USAGE]
S Bio 2 100 10 50 20 0 "bio one.txt" J 10
S Bio 2 200 10 75 20 0 "bio two.txt" J 10
S2 Green 2 150 10 25 20 0 green.txt * 10
S2 Bio 2 80 10 50 20 0 "bio other.txt" J 10
[REPORT]
SUBCATCHMENTS ALL
NODES ALL
LINKS ALL
'''


def imported():
    return Model.from_document(InpDocument.from_text(ORACLE, source='c-original.inp'), schema=schema(), strict=True)


def state(value, *, identities=True):
    result = tuple(tuple(getattr(value, c).items()) for c in ('nodes','links','subcatchments','raingages','timeseries','curves','lid_controls'))
    usage = tuple(value.lid_usage.values())
    if not identities:
        usage = tuple(replace(row, record_id=f'row-{i}') for i,row in enumerate(usage))
    return result+(usage,)


def edit_patch(value):
    return ScenarioPatch(operations=(
        RenameRecord(target=ref('lid_controls','Bio'),new_id='Garden'),
        RenameRecord(target=ref('subcatchments','S'),new_id='Basin'),
        RenameRecord(target=ref('nodes','J'),new_id='Tank'),
        RenameRecord(target=ref('curves','Head'),new_id='DrainCurve'),
        SetFields(target=ref('lid_controls','Garden'),changes=(FieldChange(name='soil',
            value=replace(value.lid_controls['Bio'].soil,conductivity=2)),)),
        SetFields(target=ref('lid_usage','lid-usage-1'),changes=(FieldChange(name='area',value=180),
            FieldChange(name='initial_saturation',value=90))),
        MoveRecord(target=ref('lid_usage','lid-usage-2'),before='lid-usage-1'),
    ))


def edited(value=None):
    value = value or imported()
    return ScenarioPatch.from_json_document(edit_patch(value).to_json_document()).apply(value).model


def edited_oracle():
    import re
    raw = ORACLE
    for old,new in (('Bio','Garden'),('S','Basin'),('J','Tank'),('Head','DrainCurve')):
        raw = re.sub(r'\b'+old+r'\b',new,raw)
    raw = raw.replace('Garden SOIL 12 .5 .2 .1 1 5 3','Garden SOIL 12 .5 .2 .1 2 5 3')
    first = 'Basin Garden 2 100 10 50 20 0 "bio one.txt" Tank 10\n'
    second = 'Basin Garden 2 200 10 75 20 0 "bio two.txt" Tank 10\n'
    return raw.replace(first+second,second+first.replace('100 10 50','180 10 90'))
