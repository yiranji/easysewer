"""Execute the shipped Quick Start cells against an explicitly installed package.

Requires Plotly on PYTHONPATH. Each invocation needs a new --output directory.
This executes Python cells headlessly; it does not claim a Jupyter UI test.
"""
import argparse
from contextlib import redirect_stdout
from datetime import datetime
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import sys
import traceback
from unittest.mock import patch


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--package-dir', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--pure', action='store_true')
    parser.add_argument('--archive-from', type=Path)
    parser.add_argument('--dependency-dir', type=Path, help='Optional directory containing Plotly dependencies')
    args = parser.parse_args()
    package = args.package_dir.resolve()
    destination = args.output.resolve()
    archive_from = args.archive_from.resolve() if args.archive_from else None
    if args.dependency_dir: sys.path.insert(0, str(args.dependency_dir.resolve()))
    destination.mkdir(parents=True, exist_ok=False)
    root = Path(__file__).resolve().parents[1]
    files = ('cases/Case_1 Quick Start/main.ipynb', 'cases/Case_1 Quick Start/cubic.inp', 'cases/utils.py')
    sources = {name: sha(root/name) for name in files}
    sources['tools/qualify_quick_start.py'] = sha(__file__)
    sandbox = destination/'sandbox'
    for name in files:
        target = sandbox/name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root/name, target)
    sys.path.insert(0, str(package))
    import easysewer
    assert Path(easysewer.__file__).resolve().parent == package/'easysewer', easysewer.__file__
    import plotly.graph_objects as go
    from easysewer.model import Model, Ref
    from easysewer.runtime import RunResult
    notebook = json.loads((sandbox/files[0]).read_text(encoding='utf-8'))
    assert notebook['nbformat'] == 4 and notebook['metadata']['language_info']['name'] == 'python'
    assert len({cell['id'] for cell in notebook['cells']}) == len(notebook['cells'])
    scope = {'__name__': '__main__'}
    executed, skipped = [], []
    previous = Path.cwd()
    os.chdir(sandbox/'cases/Case_1 Quick Start')
    log = io.StringIO()
    try:
        with redirect_stdout(log), patch.object(go.Figure, 'show') as show:
            for index, cell in enumerate(notebook['cells']):
                if cell['cell_type'] != 'code': continue
                assert cell['outputs'] == [] and cell['execution_count'] is None
                if args.pure and 'requires-native' in cell['metadata'].get('tags', ()):
                    skipped.append(index)
                    continue
                exec(compile(''.join(cell['source']), f'quick-start-cell-{index}', 'exec'), scope)
                executed.append(index)
            assert show.call_count == 1
        model = scope['cubic']
        assert (len(model.nodes), len(model.links), len(model.subcatchments)) == (14, 13, 8)
        assert model.raingages['28_RG1'].source.series.key == 'RainSeries-2'
        assert len(scope['fig'].data) == 43
        assert all(sha(sandbox/name) == sources[name] for name in files)
        assert all(sha(root/name) == digest for name, digest in sources.items())
        # Validate map behavior beyond the all-positioned, straight SI sample.
        from easysewer.model.values import Point
        alternate = model.copy()
        # The four unused rain events have no consumer declaring their units.
        # Remove them only from this geometry fixture before physical conversion.
        for series in tuple(alternate.timeseries.values()):
            if series.id != 'RainSeries-2': alternate.timeseries.remove(series.id)
        alternate.convert_units('CFS')
        alternate.nodes.update('N1', position=None)
        alternate.links.update('L2', vertices=(Point(x=100, y=200),))
        changed = scope['plot_model'](alternate)
        assert '1 nodes' in changed.layout.annotations[0].text
        link_trace = next(t for t in changed.data if isinstance(t.text, str) and t.text.startswith('Link: L2<'))
        assert tuple(link_trace.x)[1] == 100 and tuple(link_trace.y)[1] == 200
        assert link_trace.text.endswith(' ft')
        records = dict(mode='pure' if args.pure else 'native', python=sys.version,
                       package=str(package), source_hashes=sources,
                       executed_cells=executed, skipped_cells=skipped,
                       output_directory=str(scope['output_dir']), counts=[14, 13, 8], plot_traces=43,
                       input_unchanged=True, inp_json_roundtrip=True, map_missing_geometry_vertices_us_units=True)
        if args.pure:
            assert not easysewer.get_native_capabilities()['swmm_solver']
            assert args.archive_from is not None, '--pure requires a native archive to verify portable reading'
            archive = destination/'portable-archive'
            shutil.copytree(archive_from, archive)
            # Native loading must not be used for result reading.
            import ctypes
            with patch.object(ctypes, 'CDLL', side_effect=AssertionError('Pure reader loaded a native library')):
                restored = RunResult.load(archive)
                restored.raise_for_status()
                with restored.open_output() as output:
                    series = output.series(Ref(collection='swmm:nodes', key='N1'), 'swmm:depth')
                    assert len(series.values) == 180 and series.unit == 'm'
                records.update(portable_archive_read=True, output_sha256=restored.output.sha256,
                               archive_samples=len(series.values))
        else:
            result = scope['result']
            assert result.succeeded
            # Independent original input: change exactly the rain-gage binding.
            original = (sandbox/files[1]).read_text(encoding='utf-8')
            rain2, replacements = re.subn(r'(?m)^(28_RG1\s+INTENSITY\s+00:01:00\s+1\.0\s+TIMESERIES\s+)RainSeries-1',
                                         r'\g<1>RainSeries-2', original)
            assert replacements == 1
            from easysewer.runtime._solver_api import SWMMSolverAPI
            solver = SWMMSolverAPI()
            # Direct C execution bypasses the typed writer and Runner.
            for label, text in (('rain1', original), ('rain2', rain2)):
                base = destination/label
                base.with_suffix('.inp').write_text(text, encoding='utf-8')
                started = False
                try:
                    assert solver.open(*(str(base.with_suffix(ext)) for ext in ('.inp', '.rpt', '.out'))) == 0
                    assert solver.start(1) == 0
                    started = True
                    for _ in range(200000):
                        code, elapsed = solver.step()
                        assert code == 0, (label, code)
                        if not elapsed: break
                    else: raise AssertionError('Direct native simulation did not finish')
                    assert solver.end() == 0
                    started = False
                    assert solver.report() == 0
                finally:
                    if started: solver.end()
                    solver.close()
            assert sha(destination/'rain2.out') == sha(result.output.path)
            assert sha(destination/'rain1.out') != sha(result.output.path), 'Switching rainfall had no output effect'
            def stable_report(path):
                return '\n'.join(line for line in Path(path).read_text(encoding='utf-8').splitlines()
                                 if not line.strip().startswith(('Analysis begun on:', 'Analysis ended on:', 'Total elapsed time:')))
            assert stable_report(destination/'rain2.rpt') == stable_report(result.report.path)
            from easysewer.runtime._output_api import SWMMOutputAPI
            from ctypes import byref
            native = SWMMOutputAPI()
            try:
                native.open(result.output.path)
                assert native.get_times(1) == 180
                with result.open_output() as output:
                    for label, series in scope['results'].items():
                        names = output.metadata.names(series.target.collection)
                        index = names.index(series.target.key)
                        code = {'node_inflow': 4, 'node_depth': 0, 'node_overflow': 5,
                                'link_flow': 0, 'link_depth': 1, 'link_velocity': 2}[label]
                        method = native.get_node_series if label.startswith('node_') else native.get_link_series
                        assert series.values == tuple(method(index, code, 0, 180)), label
                        # This engine stores report timestamps with a 1 ms offset.
                        assert series.times[0] == datetime(1998, 1, 1, 0, 5, 0, 1000)
                        assert series.times[-1] == datetime(1998, 1, 1, 15, 0, 0, 1000)
                        assert series.unit == {'node_inflow':'LPS', 'node_depth':'m', 'node_overflow':'LPS',
                                               'link_flow':'LPS', 'link_depth':'m', 'link_velocity':'m/s'}[label]
            finally:
                assert native.lib.SMO_close(byref(native.handle)) == 0
            moved = destination/'archive-moved'
            scope['archive_dir'].rename(moved)
            # Retire the original run before reading the moved archive.
            run_dir = scope['output_dir']/'run'
            run_dir.rename(scope['output_dir']/'run-retired')
            restored = RunResult.load(moved)
            restored.raise_for_status()
            with restored.open_output() as output:
                for label, series in scope['results'].items():
                    actual = output.series(series.target, series.variable)
                    assert (actual.values, actual.times, actual.unit, actual.source.sha256) == (
                        series.values, series.times, series.unit, series.source.sha256)
            records.update(output_sha256=result.output.sha256, direct_full_out_equal=True,
                           direct_report_equal=True, rain_event_changes_output=True,
                           six_native_series_samples=1080, archive=str(moved), moved_archive_read=True)
        (destination/'result.json').write_text(json.dumps(records, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
        print(json.dumps(records, ensure_ascii=False))
    except BaseException:
        (destination/'failure.txt').write_text(traceback.format_exc(), encoding='utf-8')
        raise
    finally:
        os.chdir(previous)
        (destination/'cells.log').write_text(log.getvalue(), encoding='utf-8')


if __name__ == '__main__':
    main()
