"""Build and edit a network using public 2.0 APIs.

Geometry, rainfall, simulation clocks, dry-weather flow and a control rule are
explicitly configured to make the example runnable.
"""
import argparse
from datetime import date, time, timedelta
import json
from pathlib import Path

from easysewer.io.json import JsonDocument
from easysewer import Model
from easysewer.model import FileReference, Ref
from easysewer.model.controls import Action, Attribute, Condition, Constant, ControlRule, StatusSetting
from easysewer.model.geometry import Circular, CrossSection, Custom
from easysewer.model.hydrology import Horton, Infiltration, RainGage, SeriesRainfall, Subareas, Subcatchment
from easysewer.model.inflows import DryWeatherFlow
from easysewer.model.network import Conduit, FreeBoundary, Junction, Outfall
from easysewer.model.report import ReportSelection
from easysewer.model.resources import Curve, CurvePoint, InlineTimeSeries, Pattern, SeriesPoint
from easysewer.model.values import Point
from easysewer.runtime import RunConfig, RunResult
from easysewer.validation import ValidationError


def node(key):
    return Ref(collection='swmm:nodes', key=key)


def create_model():
    model = Model()
    model.update_options(
        flow_units='CMS', infiltration='HORTON', flow_routing='KINWAVE',
        start_date=date(2023, 4, 28), start_time=time(8),
        end_date=date(2023, 4, 28), end_time=time(11),
        report_start_date=date(2023, 4, 28), report_start_time=time(8),
        routing_step=timedelta(seconds=0.5), rule_step=timedelta(seconds=30),
        wet_step=timedelta(minutes=1), dry_step=timedelta(hours=1),
        report_step=timedelta(minutes=1), variable_step=0, dry_days=0,
    )
    for name, y, elevation in (('J1', 1000, 5), ('J2', 500, 4)):
        model.nodes.add(Junction(id=name, position=Point(x=0, y=y), elevation=elevation,
            max_depth=3, initial_depth=0, surcharge_depth=0, ponded_area=0))
    model.nodes.add(Outfall(id='J3', position=Point(x=0, y=-1000), elevation=3,
                           boundary=FreeBoundary(), gated=True))
    for name, inlet, outlet, length in (('C1', 'J1', 'J2', 500), ('C2', 'J2', 'J3', 1500)):
        model.links.add(Conduit(id=name, inlet=node(inlet), outlet=node(outlet),
            length=length, roughness=0.013, inlet_offset=0, outlet_offset=0,
            initial_flow=0, maximum_flow=0,
            section=CrossSection(geometry=Circular(diameter=0.5), barrels=1)))
    model.timeseries.add(InlineTimeSeries(id='Rain', points=tuple(
        SeriesPoint(time=timedelta(minutes=minute), value=30 if 10 <= minute <= 40 else 0)
        for minute in range(181))))
    model.raingages.add(RainGage(id='RG', form='INTENSITY', interval=timedelta(minutes=1),
        snow_factor=1, source=SeriesRainfall(series=Ref(collection='swmm:timeseries', key='Rain'))))
    model.subcatchments.add(Subcatchment(id='area1', rain_gage=Ref(collection='swmm:raingages', key='RG'),
        outlet=node('J2'), area=1, impervious_percent=0.2, width=100, slope=0.05, curb_length=0,
        subareas=Subareas(impervious_roughness=0.01, pervious_roughness=0.05,
            impervious_storage=0, pervious_storage=0, zero_storage_percent=0, routed_percent=100),
        infiltration=Infiltration(parameters=Horton(maximum_rate=20, minimum_rate=5,
            decay=5, drying_time=7, maximum_volume=0)),
        polygon=(Point(x=100, y=500), Point(x=300, y=500), Point(x=200, y=800))))
    # Shared resources are separate from nodes and rainfall collections.
    model.patterns.add(Pattern(id='DailyFlow', kind='HOURLY', factors=(1.0,)*24))
    model.dwf.add(DryWeatherFlow(node=node('J1'), baseline=0.01,
                                patterns=(Ref(collection='swmm:patterns', key='DailyFlow'),)))
    model.controls.add(ControlRule(id='PausePipe', conditions=(
        Condition(left=Attribute(object_type='SIMULATION', attribute='TIME'), relation='>=',
                  right=Constant(value=timedelta(minutes=10))),
        Condition(left=Attribute(object_type='SIMULATION', attribute='TIME'), relation='<',
                  right=Constant(value=timedelta(minutes=20)), conjunction='AND')),
        then_actions=(Action(target=Ref(collection='swmm:links', key='C1'), object_type='CONDUIT',
                             setting=StatusSetting(value='CLOSED')),),
        else_actions=(Action(target=Ref(collection='swmm:links', key='C1'), object_type='CONDUIT',
                             setting=StatusSetting(value='OPEN')),), priority=1))
    all_objects = ReportSelection(mode='ALL')
    model.update_report(nodes=all_objects, links=all_objects, subcatchments=all_objects, controls=True)
    model.validate(for_run=True).raise_for_errors()
    return model


def edit_model(original):
    """Return an independently edited network with references updated together."""
    edited = original.copy()
    with edited.transaction():
        edited.nodes.rename('J2', 'Middle')
        edited.nodes.rename('J1', 'Upstream')  # Also changes the DWF composite key.
        edited.links.rename('C1', 'Feeder')    # Also changes both control actions.
        edited.timeseries.rename('Rain', 'Storm')
        edited.raingages.rename('RG', 'RainStation')
        edited.patterns.rename('DailyFlow', 'HourlyFlow')
        edited.curves.add(Curve(id='BoxShape', kind='SHAPE',
            points=(CurvePoint(x=0, y=1), CurvePoint(x=1, y=1))))
        edited.links.update('C2', section=CrossSection(geometry=Custom(
            full_depth=0.75, curve=Ref(collection='swmm:curves', key='BoxShape')), barrels=1))
        edited.curves.rename('BoxShape', 'WiderShape')
        edited.subcatchments.update('area1', width=120)
        # This run starts with the added dry-weather flow already present.
        # The original construction model retains its zero initial flows.
        for pipe in tuple(edited.links.values()):
            edited.links.update(pipe.id, initial_flow=0.01)
    edited.validate(for_run=True).raise_for_errors()
    return edited


def demonstrate_protection(model):
    """Show a rejected delete and an invalid batch; both leave the model intact."""
    before = model.to_json_document().data
    diagnostics = {}
    try:
        model.nodes.remove('Middle')
    except ValidationError as error:
        diagnostics['delete'] = [issue.code for issue in error.report.errors]
    else:
        raise AssertionError('A referenced node was deleted without acknowledgement')
    assert model.to_json_document().data == before
    try:
        with model.transaction():
            model.nodes.rename('Middle', 'Temporary')
            model.links.update('C2', length=-1)
    except ValidationError as error:
        diagnostics['transaction'] = [issue.code for issue in error.report.errors]
    else:
        raise AssertionError('An invalid pipe length was accepted')
    assert model.to_json_document().data == before
    # Preview only: cascade=True would also remove every dependent in this plan.
    diagnostics['delete_plan'] = [dict(collection=ref.collection, key=ref.key)
                                 for ref in model.deletion_plan(node('Middle'))]
    return diagnostics


def save_models(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=False)
    original = create_model()
    edited = edit_model(original)
    diagnostics = demonstrate_protection(edited)
    for label, model in (('original', original), ('edited', edited)):
        model.to_inp(directory/(label+'.inp'))
        model.to_json_document().write(directory/(label+'.json'))
        inp = Model.from_inp(directory/(label+'.inp'), strict=True)
        restored = Model.from_json_document(JsonDocument.read(directory/(label+'.json')), strict=True)
        for collection in ('nodes', 'links', 'subcatchments', 'raingages', 'timeseries', 'patterns', 'curves', 'dwf', 'controls'):
            assert tuple(getattr(inp, collection).values()) == tuple(getattr(model, collection).values()), collection
            assert tuple(getattr(restored, collection).values()) == tuple(getattr(model, collection).values()), collection
    (directory/'edit-diagnostics.json').write_text(json.dumps(diagnostics, indent=2)+'\n', encoding='utf-8')
    return original, edited


def read_archive(directory):
    result = RunResult.load(directory)
    result.raise_for_status()
    with result.open_output() as reader:
        flow = reader.series(Ref(collection='swmm:links', key='C2'), 'swmm:flow')
        depth = reader.series(node('Middle'), 'swmm:depth')
    return dict(status=result.status, samples=len(flow.values), flow_unit=flow.unit, depth_unit=depth.unit,
                saved_flow_maximum=flow.maximum().value, saved_depth_maximum=depth.maximum().value,
                flow_continuity_percent=result.mass_balance.flow_percent,
                runoff_continuity_percent=result.mass_balance.runoff_percent,
                continuity_within_1_percent=all(value is not None and abs(value) <= 1 for value in
                    (result.mass_balance.flow_percent, result.mass_balance.runoff_percent)),
                output_sha256=result.output.sha256)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--build-only', action='store_true')
    mode.add_argument('--read-archive', action='store_true')
    args = parser.parse_args()
    directory = args.directory.resolve()
    if args.read_archive:
        print(json.dumps(read_archive(directory), indent=2))
        return
    if directory.exists(): parser.error('Choose a new directory; existing output is preserved.')
    _, edited = save_models(directory)
    if args.build_only:
        print(json.dumps(dict(status='built', directory=str(directory))))
        return
    result = edited.run(RunConfig(output_directory=FileReference(path=str(directory/'run'), direction='output'),
        wall_time_limit=timedelta(minutes=2), native_call_timeout=timedelta(seconds=30)))
    result.raise_for_status()
    result.save(directory/'archive')
    summary = read_archive(directory/'archive')
    print(json.dumps(summary, indent=2))
    if not summary['continuity_within_1_percent']:
        raise RuntimeError('Example continuity exceeds 1%; inspect the preserved report and archive.')


if __name__ == '__main__':
    main()
