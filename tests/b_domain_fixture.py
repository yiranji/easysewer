"""B acceptance fixture: six independent relations on one node."""
from datetime import date, time, timedelta

from easysewer.io.inp import InpDocument
from easysewer.io.inp.inflows import InflowsCodec
from easysewer.io.inp.network import default_schema
from easysewer.model import Model, Ref
from easysewer.model.geometry import Circular, CrossSection
from easysewer.model.network import Junction, Outfall, FreeBoundary, Conduit
from easysewer.model.inflows import (FlowInflow, ConcentrationInflow, MassInflow,
                                    DryWeatherFlow, DryWeatherConcentration)
from easysewer.model.quality import Pollutant
from easysewer.model.resources import InlineTimeSeries, SeriesPoint, Pattern
from easysewer.model.report import ReportSelection
from easysewer.scenario import ScenarioPatch, RenameRecord, SetFields, FieldChange
from easysewer.schema.structured import ModelSchema


def ref(collection, key):
    return Ref(collection='swmm:' + collection, key=key)


def schema(with_inflows=True):
    original, result = default_schema(), ModelSchema()
    for descriptor, codec in original.bindings:
        if descriptor.key != InflowsCodec.descriptor.key:
            result.register(descriptor, codec)
    if with_inflows:
        result.register(InflowsCodec.descriptor, InflowsCodec())
    result.register_json(*original.json_types.declarations)
    return result


def model():
    value = Model(schema=schema())
    value.update_options(flow_units='CFS', flow_routing='DYNWAVE', start_date=date(2020, 1, 1),
        end_date=date(2020, 1, 1), start_time=time(0), end_time=time(0, 10),
        report_step=timedelta(seconds=15), routing_step=timedelta(seconds=1), variable_step=0)
    value.update_report(nodes=ReportSelection(mode='ALL'), links=ReportSelection(mode='ALL'))
    value.nodes.add(Junction(id='J', elevation=1, max_depth=5, initial_depth=0))
    value.nodes.add(Outfall(id='O', elevation=0, boundary=FreeBoundary()))
    value.links.add(Conduit(id='P', inlet=ref('nodes', 'J'), outlet=ref('nodes', 'O'),
        length=50, roughness=.013, inlet_offset=0, outlet_offset=0,
        section=CrossSection(geometry=Circular(diameter=2))))
    for key, units in (('A', 'MG/L'), ('B', 'UG/L')):
        value.pollutants.add(Pollutant(id=key, units=units, rainfall_concentration=0,
            groundwater_concentration=0, rdii_concentration=0, decay_rate=0))
    for key, initial, final in (('Q', .1, .2), ('C', .5, 1), ('M', .01, .02)):
        value.timeseries.add(InlineTimeSeries(id=key, points=(SeriesPoint(time=timedelta(), value=initial),
            SeriesPoint(time=timedelta(minutes=10), value=final))))
    value.patterns.add(Pattern(id='Daily', kind='DAILY', factors=(1, 2, 3, 4, 5, 6, 7)))
    value.patterns.add(Pattern(id='Monthly', kind='MONTHLY', factors=(2,)*12))
    node, daily, monthly = ref('nodes', 'J'), ref('patterns', 'Daily'), ref('patterns', 'Monthly')
    value.inflows.add(FlowInflow(node=node, series=ref('timeseries', 'Q'), scale_factor=2, baseline=.05, pattern=daily))
    value.inflows.add(ConcentrationInflow(node=node, constituent=ref('pollutants', 'A'),
        series=ref('timeseries', 'C'), scale_factor=1, baseline=2, pattern=daily))
    value.inflows.add(MassInflow(node=node, constituent=ref('pollutants', 'B'),
        series=ref('timeseries', 'M'), scale_factor=.5, baseline=.01, mass_factor=126, pattern=daily))
    value.dwf.add(DryWeatherFlow(node=node, baseline=.1, patterns=(monthly, daily)))
    value.dwf.add(DryWeatherConcentration(node=node, constituent=ref('pollutants', 'A'),
        baseline=4, patterns=(None, daily, monthly)))
    value.dwf.add(DryWeatherConcentration(node=node, constituent=ref('pollutants', 'B'),
        baseline=30, patterns=(daily, monthly)))
    return value


ORACLE = '''[OPTIONS]
FLOW_UNITS CFS
FLOW_ROUTING DYNWAVE
START_DATE 01/01/2020
START_TIME 00:00
END_DATE 01/01/2020
END_TIME 00:10
REPORT_STEP 00:00:15
ROUTING_STEP 1
VARIABLE_STEP 0
[JUNCTIONS]
J 1 5 0
[OUTFALLS]
O 0 FREE
[CONDUITS]
P J O 50 .013 0 0
[XSECTIONS]
P CIRCULAR 2 0 0 0
[POLLUTANTS]
A MG/L 0 0 0 0
B UG/L 0 0 0 0
[TIMESERIES]
Q 0 .1
Q 00:10 .2
C 0 .5
C 00:10 1
M 0 .01
M 00:10 .02
[PATTERNS]
Daily DAILY 1 2 3 4 5 6 7
Monthly MONTHLY 2 2 2 2 2 2 2 2 2 2 2 2
[INFLOWS]
J FLOW Q FLOW 1 2 .05 Daily
J A C CONCEN 1 1 2 Daily
J B M MASS 126 .5 .01 Daily
[DWF]
J FLOW .1 Monthly Daily
J A 4 "" Daily Monthly
J B 30 Daily Monthly
[REPORT]
NODES ALL
LINKS ALL
'''


def imported():
    return Model.from_document(InpDocument.from_text(ORACLE, source='b-original.inp'), schema=schema(), strict=True)


def state(value):
    return tuple(tuple(getattr(value, name).items())
                 for name in ('nodes', 'links', 'pollutants', 'timeseries', 'patterns', 'inflows', 'dwf'))


def edit_patch():
    operations = tuple(RenameRecord(target=ref(c, a), new_id=b) for c, a, b in (
        ('nodes', 'J', 'Tank'), ('pollutants', 'A', 'Solids'), ('pollutants', 'B', 'Tracer'),
        ('timeseries', 'Q', 'Hydrograph'), ('timeseries', 'C', 'Chem'), ('timeseries', 'M', 'Dose'),
        ('patterns', 'Daily', 'WorkDays')))
    operations += (SetFields(target=ref('inflows', ('Tank', 'FLOW')), changes=(FieldChange(name='baseline', value=.075),)),
        SetFields(target=ref('inflows', ('Tank', 'POLLUTANT:Tracer')), changes=(FieldChange(name='mass_factor', value=252),)),
        SetFields(target=ref('dwf', ('Tank', 'POLLUTANT:Solids')), changes=(FieldChange(name='baseline', value=6),)))
    return ScenarioPatch(operations=operations)


def edited(value=None):
    return ScenarioPatch.from_json_document(edit_patch().to_json_document()).apply(value or imported()).model


def edited_oracle():
    # Token mapping is independent of INP/model serialization. All chosen IDs
    # are unique and do not collide with section or formula keywords.
    names = dict(J='Tank', A='Solids', B='Tracer', Q='Hydrograph', C='Chem', M='Dose', Daily='WorkDays')
    raw = '\n'.join(' '.join(names.get(token, token) for token in row.split()) for row in ORACLE.splitlines())+'\n'
    return raw.replace('FLOW 1 2 .05', 'FLOW 1 2 .075').replace('MASS 126', 'MASS 252').replace('Tank Solids 4 ', 'Tank Solids 6 ')
