"""Create, edit, save, solve and archive a small model using public 2.0 APIs."""
import argparse
from datetime import date, time, timedelta
import json
from pathlib import Path

from easysewer.io.json import JsonDocument
from easysewer import Model
from easysewer.model import FileReference, Ref
from easysewer.model.geometry import Circular, CrossSection
from easysewer.model.inflows import FlowInflow
from easysewer.model.network import Conduit, FreeBoundary, Junction, Outfall
from easysewer.model.report import ReportSelection
from easysewer.model.resources import InlineTimeSeries, SeriesPoint
from easysewer.runtime import ReportReadOptions, RunConfig, RunResult


def create_model():
    """SI geometry and CMS flow; all times use the model's local calendar."""
    model = Model()
    model.update_options(
        flow_units='CMS', flow_routing='DYNWAVE',
        start_date=date(2020, 1, 1), end_date=date(2020, 1, 1),
        end_time=time(0, 20), routing_step=timedelta(seconds=1),
        report_step=timedelta(minutes=1), variable_step=0,
    )
    upstream = Ref(collection='swmm:nodes', key='J1')
    outlet = Ref(collection='swmm:nodes', key='O1')
    model.nodes.add(Junction(id='J1', elevation=1, max_depth=2))
    model.nodes.add(Outfall(id='O1', elevation=0, boundary=FreeBoundary()))
    model.links.add(Conduit(
        id='P1', inlet=upstream, outlet=outlet, length=50, roughness=0.013,
        inlet_offset=0, outlet_offset=0,
        section=CrossSection(geometry=Circular(diameter=0.6)),
    ))
    model.timeseries.add(InlineTimeSeries(
        id='Q1', points=tuple(
            SeriesPoint(time=timedelta(minutes=minute), value=flow)
            for minute, flow in ((0, 0), (3, 0.020), (7, 0.010), (10, 0), (20, 0))
        ),
    ))
    model.inflows.add(FlowInflow(
        node=upstream, series=Ref(collection='swmm:timeseries', key='Q1'),
    ))
    # Records are immutable; edit them through their collection.
    model.links.update('P1', length=60)
    model.update_report(
        nodes=ReportSelection(mode='ALL'), links=ReportSelection(mode='ALL'),
    )
    model.validate(for_run=True).raise_for_errors()
    return model


def read_archive(directory):
    """Inspect a moved archive; this needs no native solver or output library."""
    restored = RunResult.load(directory)
    restored.raise_for_status()
    with restored.open_output() as output:
        depth = output.series(Ref(collection='swmm:nodes', key='J1'), 'swmm:depth')
    peak = depth.maximum()
    return dict(
        status=restored.status, samples=len(depth.values),
        maximum_saved_depth=peak.value, depth_unit=peak.unit,
        maximum_saved_depth_time=peak.time.isoformat() if peak.time else None,
        statistic=peak.statistic,
        flow_continuity_percent=restored.mass_balance.flow_percent,
        output_sha256=restored.output.sha256,
        node_depth_rows=len(restored.report_table('swmm:node_depth').rows),
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path, help='New output directory, or existing archive with --read-archive')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--build-only', action='store_true', help='Create INP/JSON without loading a native solver')
    mode.add_argument('--read-archive', action='store_true', help='Read an existing archive without running a simulation')
    args = parser.parse_args()
    destination = args.directory.resolve()
    if args.read_archive:
        print(json.dumps(read_archive(destination), indent=2))
        return
    if destination.exists():
        parser.error('Choose a new output directory; existing files are preserved.')
    destination.mkdir(parents=True)
    model = create_model()
    model.to_inp(destination/'network.inp')
    model.to_json_document().write(destination/'network.json')
    from_inp = Model.from_inp(destination/'network.inp', strict=True)
    from_json = Model.from_json_document(JsonDocument.read(destination/'network.json'), strict=True)
    # Check the values used by this example after both persistence paths.
    for restored in (from_inp, from_json):
        restored.validate(for_run=True).raise_for_errors()
        for collection in ('nodes', 'links', 'timeseries', 'inflows'):
            assert tuple(getattr(restored, collection).values()) == tuple(getattr(model, collection).values())
    if args.build_only:
        print(json.dumps(dict(status='built', directory=str(destination), inp_json_roundtrip=True)))
        return
    result = from_json.run(RunConfig(
        output_directory=FileReference(path=str(destination/'run'), direction='output'),
        wall_time_limit=timedelta(minutes=2), native_call_timeout=timedelta(seconds=30),
        report_read=ReportReadOptions(tables=('swmm:node_depth',)),
    ))
    result.raise_for_status()
    archive = destination/'archive'
    result.save(archive)
    moved = destination/'archive-moved'
    archive.rename(moved)
    summary = read_archive(moved)
    assert summary['output_sha256'] == result.output.sha256
    summary.update(directory=str(destination), archive=str(moved), inp_json_roundtrip=True)
    (destination/'summary.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
