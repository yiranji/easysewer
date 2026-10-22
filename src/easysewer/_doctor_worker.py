"""Native-only, one-shot diagnostic child. Never imported by ordinary imports."""
import json
import math
import os
from pathlib import Path
import sys


def _solver(name):
    from ._native_registry import select_library
    artifact = select_library(name, verify=True)
    if name == 'swmm5':
        from .runtime._native_solver import NativeSolver as Solver
        from .runtime.native import StandardBackend as Backend
    elif name == 'flexible_ponding':
        from .runtime._native_flexible import NativeFlexibleSolver as Solver
        from .runtime.flexible import FlexiblePondingBackend as Backend
    else:
        raise ValueError('Unknown solver diagnostic')
    solver = Solver(str(artifact.path), expected_sha256=artifact.record['sha256'])
    info = Backend()._loaded(solver.metadata)
    if not info.available:
        raise ValueError(info.reason)
    return solver, info


def _probe(name):
    if name == 'swmm-output':
        from .runtime._output_api import SWMMOutputAPI
        with SWMMOutputAPI():
            return dict(output_io_revision=1, initialize_close=True)
    solver, info = _solver(name)
    try:
        return dict(engine_version=info.engine_version, abi=info.abi,
                    architecture=info.architecture, numerical_policy=info.numerical_policy,
                    capabilities=info.capabilities)
    finally:
        failures = solver.cleanup()
        if failures:
            raise RuntimeError(str(failures))


_INPUT = '''[TITLE]
EasySewer isolated diagnostic
[OPTIONS]
FLOW_UNITS CMS
FLOW_ROUTING DYNWAVE
START_DATE 01/01/2020
START_TIME 00:00:00
END_DATE 01/01/2020
END_TIME 00:02:00
REPORT_START_DATE 01/01/2020
REPORT_START_TIME 00:00:00
REPORT_STEP 00:00:30
ROUTING_STEP 1
THREADS 1
[JUNCTIONS]
J1 1 2 0 0 0
[OUTFALLS]
O1 0 FREE NO
[CONDUITS]
C1 J1 O1 50 0.013 0 0 0 0
[XSECTIONS]
C1 CIRCULAR 0.6 0 0 0 1
[REPORT]
INPUT NO
CONTINUITY YES
FLOWSTATS NO
NODES ALL
LINKS ALL
'''


def _smoke(directory):
    from .io.output import OutputReader
    from .runtime._output_api import SWMMOutputAPI
    root = Path(directory).resolve(strict=True)
    os.chdir(root)
    paths = tuple(root / ('diagnostic.' + suffix) for suffix in ('inp', 'rpt', 'out'))
    paths[0].write_text(_INPUT, encoding='ascii')
    solver, info = _solver('swmm5')
    try:
        # Native filenames are ASCII-relative even when the user's temporary
        # directory contains Unicode; Python owns the directory change.
        solver.open(tuple(Path(path.name) for path in paths))
        solver.start(True)
        for _ in range(1000):
            if solver.step(1000)['finished']:
                break
        else:
            raise RuntimeError('Minimal solve exceeded its step bound')
        mass_balance = solver.end()
        solver.report()
    finally:
        failures = solver.cleanup()
        if failures:
            raise RuntimeError(str(failures))
    with OutputReader(paths[2]) as reader:
        periods = reader.metadata.periods
        if periods <= 0 or reader.metadata.engine_version != 52004:
            raise ValueError('Minimal solve did not produce valid SWMM 5.2.4 output')
        pure_values = reader.series(None, 'swmm:flooding').values
    with SWMMOutputAPI() as reader:
        reader.open(paths[2].name)
        native_periods = reader.get_times(1)
        native_values = reader.get_system_series(10, 0, native_periods)
        if native_periods != periods or len(native_values) != periods:
            raise ValueError('Native and pure output period counts differ')
        if any(not math.isfinite(value) for value in native_values):
            raise ValueError('Native output contains nonfinite observations')
        if tuple(native_values) != tuple(pure_values):
            raise ValueError('Native and pure output observations differ')
    return dict(engine_version=info.engine_version, periods=periods,
                native_output_read=True, pure_output_read=True,
                output_values_match=True, mass_balance=mass_balance)


def main():
    from .runtime._solver_worker import _configure_error_mode
    _configure_error_mode()
    # Native stdout must not corrupt the single JSON diagnostic response.
    output = os.fdopen(os.dup(sys.stdout.fileno()), 'w', encoding='utf-8')
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    try:
        request = json.loads(sys.stdin.buffer.read(65537))
        operation = request.get('operation')
        if operation == 'probe':
            evidence = _probe(request['name'])
        elif operation == 'smoke':
            evidence = _smoke(request['directory'])
        else:
            raise ValueError('Unknown doctor operation')
        result = dict(ok=True, evidence=evidence)
    except Exception as error:
        result = dict(ok=False, message=f'{type(error).__name__}: {error}')
    json.dump(result, output, allow_nan=False)
    output.write('\n')
    output.flush()
