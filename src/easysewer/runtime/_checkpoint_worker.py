"""Private FlexiblePonding checkpoint owner, coordinated with native owners.

This is not a public resume API. The coordinator must bind these immutable bytes
to the complete native/model/resource snapshot, serialize all calls, validate
every owner and prepare every resource before committing any owner. A worker
prepare rejection leaves the running solver usable. Paths returned by the
trusted resource provider are private; that provider owns their cleanup.
"""

import hashlib
import io
import json
import math
import os
from pathlib import Path
import stat

from ..io.json import JsonDocument
from .backend import NativeFailure

MAX_BYTES = 16 * 1024 * 1024
MAX_INTEGER = 9007199254740991
FORMAT = 'easysewer:checkpoint:flexible-worker'


def _shape(value, keys):
    if type(value) is not dict or set(value) != set(keys):
        raise ValueError('Invalid worker checkpoint fields')


def _number(value, *, minimum=0):
    if type(value) not in (int, float) or not math.isfinite(value) or value < minimum:
        raise ValueError('Invalid worker checkpoint numeric value')
    return value


def _counter(value):
    if type(value) is not int or not 0 <= value <= MAX_INTEGER:
        raise ValueError('Invalid worker checkpoint counter')
    return value


def _boundary(solver):
    if (not getattr(solver, '_checkpoint_ready', False) or solver.closed or
            solver.ended or not solver.start_attempted or solver.statistics):
        raise ValueError('Worker checkpoint requires a successful unfinished boundary')


def _binding(solver):
    # Detach from mutable solver data. Physical trace destinations are resource
    # locations, not policy identity; every other execution parameter binds.
    parameters = dict(solver.parameters)
    parameters['trace'] = parameters['trace'] is not None
    return JsonDocument.from_data(dict(
        engine={k: solver.metadata[k] for k in
                ('sha256', 'engine_version', 'platform', 'architecture', 'abi')},
        parameters=parameters, duration=solver.duration, names=solver.names,
        nodes=[{k: n[k] for k in ('id', 'index', 'area', 'volume_per_depth')}
               for n in solver.records],
        pollutants=[{k: p[k] for k in
                     ('id', 'index', 'concentration_unit', 'quantity_unit')}
                    for p in solver.pollutants],
    )).data


def _identity(stream):
    info = os.fstat(stream.fileno())
    if not stat.S_ISREG(info.st_mode):
        raise ValueError('Checkpoint trace must be a regular file')
    return info.st_dev, info.st_ino


def _describe(raw):
    before = os.fstat(raw.fileno())
    identity = _identity(raw)
    raw.seek(0)
    digest = hashlib.sha256()
    size = 0
    tail = b''
    while True:
        chunk = raw.read(1024 * 1024)
        if not chunk:
            break
        digest.update(chunk)
        size += len(chunk)
        tail = chunk[-1:]
    after = os.fstat(raw.fileno())
    if (identity != _identity(raw) or before.st_size != size or after.st_size != size
            or before.st_mtime_ns != after.st_mtime_ns or tail != b'\n'):
        raise ValueError('Checkpoint trace changed or lacks a complete final line')
    return dict(size=size, sha256=digest.hexdigest())


def capture(solver):
    """Return immutable worker JSON; the coordinator separately copies trace bytes.

    Native successful-boundary validation must also run before publication.
    A trace flush error does not produce a snapshot. No trace path is serialized.
    """
    _boundary(solver)
    if solver.time != solver.lib.swmm_getCurrentTime() * 86400:
        raise ValueError('Worker and native clocks disagree')
    if solver.duration != solver.lib.swmm_getRoutingDuration() / 1000:
        raise ValueError('Worker and native durations disagree')
    trace = None
    if solver.parameters['trace'] is not None:
        if solver.trace is None:
            raise ValueError('Worker trace owner is missing')
        solver.trace.flush()
        identity = _identity(solver.trace)
        with open(solver.trace.name, 'rb') as raw:
            if _identity(raw) != identity:
                raise ValueError('Worker trace path no longer names its live owner')
            trace = _describe(raw)
        if solver.trace.tell() != trace['size']:
            raise ValueError('Worker trace has an external tail or invalid writer position')
    elif solver.trace is not None:
        raise ValueError('Unexpected worker trace owner')
    data = dict(kind=FORMAT, version=1, binding=_binding(solver), trace=trace,
                state=dict(time=solver.time, steps=solver.steps,
                           nodes=[dict(removed_volume=n['removed_volume'],
                                       active_steps=n['active_steps'],
                                       previous=solver.previous[n['index']]) for n in solver.records],
                           pollutants=[p['removed_quantity'] for p in solver.pollutants]))
    payload = JsonDocument.from_data(data).to_bytes()
    validate(solver, payload)
    return payload


def validate(solver, payload):
    """Validate and detach all Python state without opening or mutating resources."""
    _boundary(solver)
    if type(payload) is not bytes or len(payload) > MAX_BYTES:
        raise ValueError('Invalid worker checkpoint byte payload')
    data = JsonDocument.from_bytes(payload).data
    _shape(data, ('kind', 'version', 'binding', 'state', 'trace'))
    if data['kind'] != FORMAT or type(data['version']) is not int or data['version'] != 1:
        raise ValueError('Unsupported worker checkpoint format')
    # Canonical JSON keeps bool/int and signed-zero distinctions which ordinary
    # Python dictionary equality would discard in binding comparisons.
    canonical = lambda value: json.dumps(value, sort_keys=True, allow_nan=False, separators=(',', ':'))
    if canonical(data['binding']) != canonical(_binding(solver)):
        raise ValueError('Worker checkpoint execution binding mismatch')
    state = data['state']
    _shape(state, ('time', 'steps', 'nodes', 'pollutants'))
    now = _number(state['time'])
    steps = _counter(state['steps'])
    if now > solver.duration + 1e-6 or (steps == 0) != (now == 0):
        raise ValueError('Worker checkpoint clock/counter mismatch')
    if type(state['nodes']) is not list or len(state['nodes']) != len(solver.records):
        raise ValueError('Worker checkpoint node inventory mismatch')
    for row in state['nodes']:
        _shape(row, ('removed_volume', 'active_steps', 'previous'))
        _number(row['removed_volume'])
        active = _counter(row['active_steps'])
        if active > steps or (active == 0) != (row['removed_volume'] == 0):
            raise ValueError('Worker checkpoint removal counter mismatch')
        _shape(row['previous'], ('depth', 'volume', 'overflow'))
        _number(row['previous']['depth'])
        _number(row['previous']['volume'])
        _number(row['previous']['overflow'], minimum=-math.inf)
    if type(state['pollutants']) is not list or len(state['pollutants']) != len(solver.pollutants):
        raise ValueError('Worker checkpoint pollutant inventory mismatch')
    for value in state['pollutants']:
        _number(value)
        if not solver.parameters['quality_enabled'] and value != 0:
            raise ValueError('Disabled quality cannot accumulate a worker ledger')
    trace = data['trace']
    if solver.parameters['trace'] is None:
        if trace is not None:
            raise ValueError('Unexpected checkpoint trace')
    else:
        _shape(trace, ('size', 'sha256'))
        if _counter(trace['size']) == 0 or type(trace['sha256']) is not str or len(trace['sha256']) != 64:
            raise ValueError('Invalid checkpoint trace descriptor')
        if any(c not in '0123456789abcdef' for c in trace['sha256']):
            raise ValueError('Invalid checkpoint trace digest')
    return data


class PreparedWorker:
    """Staged ownership. Commit is only valid in the serialized coordinator.

    Apply after ALL native and Python preparations succeed. Close retired owners
    afterwards using discard(); a close error after commit must not be presented
    as rejection. Caller owns staged/retired paths and retains them as needed.
    """
    def __init__(self, solver, data):
        self.solver = solver
        self.committed = False
        self.disposed = False
        self.replacement = None
        self.retired = None
        state = data['state']
        self.time, self.steps = state['time'], state['steps']
        self.records = [dict(n, removed_volume=s['removed_volume'], active_steps=s['active_steps'])
                        for n, s in zip(solver.records, state['nodes'])]
        self.previous = {n['index']: dict(s['previous']) for n, s in zip(self.records, state['nodes'])}
        self.pollutants = [dict(p, removed_quantity=v) for p, v in zip(solver.pollutants, state['pollutants'])]

    def apply(self):
        if self.disposed or self.committed:
            raise ValueError('Worker checkpoint stage is no longer applicable')
        solver = self.solver
        solver.time, solver.steps = self.time, self.steps
        solver.records, solver.previous, solver.pollutants = self.records, self.previous, self.pollutants
        self.retired, solver.trace = solver.trace, self.replacement
        self.replacement = None
        self.committed = True

    def discard(self):
        if self.disposed:
            return
        self.disposed = True
        stream = self.retired if self.committed else self.replacement
        self.retired = self.replacement = None
        if stream is not None:
            stream.close()


def prepare(solver, payload, *, trace_path=None, forbidden_files=()):
    """Stage a verified independent trace prefix without changing live owners.

    `forbidden_files` includes every active/staged native output stream path in
    the coordinator. The trusted provider must create an independent private
    file before calling this function, and clean up that path on rejection.
    """
    data = validate(solver, payload)
    stage = PreparedWorker(solver, data)
    raw = None
    try:
        if data['trace'] is None:
            if trace_path is not None:
                raise ValueError('No trace resource expected')
        else:
            if trace_path is None:
                raise ValueError('Checkpoint trace prefix is required')
            raw = open(Path(trace_path), 'r+b')
            identity = _identity(raw)
            if solver.trace is not None and identity == _identity(solver.trace):
                raise ValueError('Checkpoint trace aliases the live trace')
            for path in forbidden_files:
                info = os.stat(path)
                if identity == (info.st_dev, info.st_ino):
                    raise ValueError('Checkpoint trace aliases another output')
            if _describe(raw) != data['trace']:
                raise ValueError('Checkpoint trace prefix content mismatch')
            raw.seek(0, os.SEEK_END)
            stage.replacement = io.TextIOWrapper(raw, encoding='utf-8', newline='\n')
            raw = None
        return stage
    except BaseException as error:
        cleanup = []
        def retain(issue):
            # Keep diagnostic values, not traceback cycles holding staged file
            # objects, buffers, locks and the live solver graph.
            cleanup.append(NativeFailure(stage='checkpoint_trace_cleanup', code=None,
                                         message=f'{type(issue).__name__}: {issue}'))
        if raw is not None:
            try:
                raw.close()
            except BaseException as issue:
                retain(issue)
        try:
            stage.discard()
        except BaseException as issue:
            retain(issue)
        if cleanup:
            error.checkpoint_cleanup_errors = tuple(cleanup)
        raise
