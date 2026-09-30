"""Real Python ponding policy and native checkpoint/output bundle integration.

The bridge uses private C test exports. Public Session/Runner resume and the
production snapshot container are not provided by these tests.
"""
from contextlib import contextmanager
import base64
import builtins
import copy
import ctypes as c
from datetime import time, timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from easysewer.io.json import JsonDocument
from easysewer.runtime import FlexiblePondingBackend, FlexiblePondingPolicy
from easysewer.runtime._native_flexible import NativeFlexibleSolver
from easysewer.runtime import _checkpoint_worker as owner
from easysewer.validation import ValidationError
from test_flexible_v2 import configuration, ponding_model
from test_native_v2_ponding_accounting import sealed_model
from test_native_v2_checkpoint_writes import PREFIX, OPEN
from test_native_v2_standard_io import handles

EVIDENCE = []

CHILD = '''import json,sys,base64
from pathlib import Path
from test_native_v2_checkpoint_worker import open_solver,Bridge,finish,working
from easysewer.runtime import _checkpoint_worker as owner
root,case,units=sys.argv[1:];root=Path(root)
saved=json.loads((root/'saved.json').read_text())
with working(root/'second'):
 solver=open_solver(root/'second',case,units);bridge=Bridge(solver)
 try:
  payload=base64.b64decode(saved['worker'])
  prefix=None
  if saved['trace'] is not None:
   prefix=Path('restored-trace');prefix.write_bytes(base64.b64decode(saved['trace']))
  stage=owner.prepare(solver,payload,trace_path=prefix)
  streams={int(k):dict(v,data=base64.b64decode(v['data'])) for k,v in saved['prefixes'].items()}
  assert bridge.restore(base64.b64decode(saved['native']),streams)==(0,True,0)
  stage.apply();stage.discard()
  assert owner.capture(solver)==payload
  for _ in range(10000):
   if solver.step(3)['finished']:break
  else:raise AssertionError('did not finish')
  result=finish(solver,bridge)
  (root/'result.json').write_text(json.dumps(result))
 finally:solver.cleanup()
'''


@contextmanager
def working(root):
    previous = Path.cwd()
    os.chdir(root)
    try:
        yield
    finally:
        os.chdir(previous)


def model_for(case, units='CFS'):
    model = ponding_model(units) if case == 'adaptive' else sealed_model(units, with_pump=True)
    model.update_options(end_time=time(0, 1, 2), routing_step=timedelta(seconds=7),
                         rule_step=timedelta(seconds=20), report_step=timedelta(seconds=20))
    if case == 'adaptive':
        model.update_options(variable_step=.75, minimum_step=timedelta(seconds=.1))
    if case == 'averages':
        model.update_report(averages=True)
    if case == 'ignore-quality':
        model.update_options(ignore_quality=True)
    if case == 'multiple':
        from dataclasses import replace
        from easysewer.model import Ref
        model.nodes.add(replace(model.nodes['J'], id='Second'))
        for record in tuple(model.inflows.values()):
            model.inflows.add(replace(record, node=Ref(collection='swmm:nodes', key='Second')))
    return model


def open_solver(root, case='normal', units='CFS'):
    model = model_for(case, units)
    model.to_inp('run.inp')
    policy = FlexiblePondingPolicy(external_flooding_ratio=0 if case == 'zero' else .37,
                                  depth_threshold_m=0, flow_threshold_cms=0,
                                  record_steps=case != 'no-trace')
    Path('assets').mkdir()
    parameters = FlexiblePondingBackend().prepare_run(
        model, configuration(root, policy),
        SimpleNamespace(input_sha256=hashlib.sha256(Path('run.inp').read_bytes()).hexdigest()),
        artifact_directory=Path('assets')).parameters.data
    solver = NativeFlexibleSolver(os.environ['EASYSEWER_CHECKPOINT_CUSTOM'])
    try:
        solver.open(['run.inp', 'run.rpt', 'run.out'])
        solver.configure(parameters)
        solver.start(True)
        return solver
    except BaseException:
        solver.cleanup()
        raise


class Bridge:
    def __init__(self, solver):
        self.solver, self.lib = solver, solver.lib
        self.counter = 0
        self.prefixes = {}
        for name, args, result in (
            ('es_test_writes_save', [c.c_void_p,c.c_size_t,c.POINTER(c.c_size_t),c.c_int], c.c_int),
            ('es_test_writes_restore', [c.c_void_p,c.c_size_t,c.c_int,c.c_int,OPEN,PREFIX,c.c_int,c.POINTER(c.c_int),c.POINTER(c.c_int)], c.c_int),
            ('es_test_writes_info', [c.c_uint32,c.POINTER(c.c_int),c.POINTER(c.c_int),c.POINTER(c.c_uint64),c.c_void_p,c.c_size_t], c.c_int),
        ):
            function = getattr(self.lib, name)
            function.argtypes, function.restype = args, result
        def read(context, key, error):
            error[0] = 7
            return None
        self.read = OPEN(read)
        def write(context, role, index, text, size, destination, capacity):
            try:
                item = self.prefixes[index]
                assert (role, text, size) == (item['role'], item['text'], len(item['data']))
                path = Path('native-prefix-' + str(self.counter));self.counter += 1
                path.write_bytes(item['data'])
                raw = os.fsencode(path)
                assert len(raw) < capacity
                c.memmove(destination, raw + b'\0', len(raw) + 1)
                return 0
            except BaseException:
                return 7
        self.write = PREFIX(write)

    def inventory(self):
        result = {}
        for index in range(100):
            role, text, size = c.c_int(), c.c_int(), c.c_uint64()
            path = c.create_string_buffer(4096)
            code = self.lib.es_test_writes_info(index, c.byref(role), c.byref(text), c.byref(size), path, len(path))
            if code == 0:
                return result
            assert code == 1, code
            name = os.fsdecode(path.value);raw = Path(name).read_bytes()
            assert len(raw) == size.value
            result[index] = dict(role=role.value, text=text.value, data=raw, path=name)
        raise AssertionError('unbounded inventory')

    def capture(self):
        size = c.c_size_t()
        assert self.lib.es_test_writes_save(None, 0, c.byref(size), 0) == 0
        raw = c.create_string_buffer(size.value)
        assert self.lib.es_test_writes_save(raw, size.value, c.byref(size), 0) == 0
        return raw.raw, self.inventory()

    def restore(self, raw, prefixes, fail=-1):
        self.prefixes = prefixes
        cleanup, committed = c.c_int(), c.c_int()
        code = self.lib.es_test_writes_restore(c.create_string_buffer(raw), len(raw), -1, fail,
                                              self.read, self.write, 0, c.byref(cleanup), c.byref(committed))
        return code, bool(committed.value), cleanup.value


def finish(solver, bridge):
    outputs = bridge.inventory()
    trace = Path(solver.trace.name) if solver.trace else None
    balance = solver.end();solver.report()
    results = solver.execution_results()
    assert not solver.cleanup()
    raw = {}
    for index, item in outputs.items():
        data = Path(item['path']).read_bytes()
        if item['role'] == 0:
            data = re.sub(rb'(?m)^[ \t]*(?:Analysis (?:begun|ended)|Total elapsed time)[^\r\n]*', b'', data)
        raw[str(index)] = hashlib.sha256(data).hexdigest()
    return dict(outputs=raw, trace=hashlib.sha256(trace.read_bytes()).hexdigest() if trace else None,
                results=results, balance=balance)


def run_case(case, units='CFS', restore=False):
    with tempfile.TemporaryDirectory() as directory, working(directory):
        solver = open_solver(Path(directory), case, units);bridge = Bridge(solver)
        try:
            for step in range(10000):
                if restore and step % 3 == 0:
                    native, prefixes = bridge.capture()
                    payload = owner.capture(solver)
                    trace = None
                    if solver.trace:
                        trace = Path('worker-prefix-' + str(step))
                        trace.write_bytes(Path(solver.trace.name).read_bytes())
                    stage = owner.prepare(solver, payload, trace_path=trace,
                                          forbidden_files=[item['path'] for item in prefixes.values()])
                    assert bridge.restore(native, prefixes) == (0, True, 0)
                    # Poison every Python persistent owner after preparation,
                    # proving prepared values are detached from the live ones.
                    solver.previous.clear();solver.records.clear();solver.pollutants.clear()
                    solver.time = -1;solver.steps = -1
                    stage.apply();stage.discard()
                    assert owner.capture(solver) == payload
                if solver.step(1)['finished']:
                    break
            else:
                raise AssertionError('did not finish')
            return finish(solver, bridge)
        finally:
            solver.cleanup()


@unittest.skipUnless(os.environ.get('EASYSEWER_CHECKPOINT_CUSTOM'), 'Requires internal native writes candidate')
class NativeWorkerCheckpointTests(unittest.TestCase):
    def test_real_policy_restores_outputs_trace_and_water_quality_statistics(self):
        cases = [('normal', u) for u in ('CFS','GPM','MGD','CMS','LPS','MLD')]
        cases += [(case, 'CFS') for case in ('multiple','adaptive','averages','ignore-quality','zero','no-trace')]
        for case, units in cases:
            with self.subTest(case=case, units=units):
                expected = run_case(case, units)
                self.assertEqual(run_case(case, units, True), expected)
                EVIDENCE.append(dict(kind='restore', case=case, units=units, result=expected))

    def test_fresh_process_restores_real_worker_and_all_native_owners(self):
        cases = [('normal', u) for u in ('CFS','GPM','MGD','CMS','LPS','MLD')]
        cases += [(case, 'CFS') for case in ('multiple','adaptive','averages','ignore-quality','zero','no-trace')]
        for case, units in cases:
            with self.subTest(case=case, units=units), tempfile.TemporaryDirectory() as directory:
                expected = run_case(case, units)
                root = Path(directory);(root/'first').mkdir();(root/'second').mkdir()
                with working(root/'first'):
                    solver = open_solver(root/'first', case, units);bridge = Bridge(solver)
                    try:
                        solver.step(3)
                        native, streams = bridge.capture();payload = owner.capture(solver)
                        saved = dict(worker=base64.b64encode(payload).decode(), native=base64.b64encode(native).decode(),
                                     trace=base64.b64encode(Path(solver.trace.name).read_bytes()).decode() if solver.trace else None,
                                     prefixes={str(k):dict(v,data=base64.b64encode(v['data']).decode()) for k,v in streams.items()})
                    finally:solver.cleanup()
                (root/'saved.json').write_text(json.dumps(saved))
                env = dict(os.environ, PYTHONPATH=os.pathsep.join((str(Path(__file__).parent),str(Path(__file__).parents[1]/'src'))))
                process = subprocess.run([sys.executable,'-B','-c',CHILD,str(root),case,units],env=env,
                                         capture_output=True,text=True,timeout=60,
                                         creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                self.assertEqual(process.returncode,0,process.stdout+process.stderr)
                self.assertEqual(json.loads((root/'result.json').read_text()), expected)
                EVIDENCE.append(dict(kind='fresh-worker',case=case,units=units,result=expected))

    def test_rewind_and_omission_counterexamples(self):
        expected = run_case('normal')
        for omitted in (None,'previous','steps','water','quality','trace'):
            with self.subTest(omitted=omitted), tempfile.TemporaryDirectory() as directory, working(directory):
                solver = open_solver(Path(directory));bridge = Bridge(solver)
                try:
                    solver.step(3);native, streams = bridge.capture();payload = owner.capture(solver)
                    prefix = Path('rewound-trace');prefix.write_bytes(Path(solver.trace.name).read_bytes())
                    solver.step(2)
                    future = copy.deepcopy(solver.previous)
                    old_trace = Path(solver.trace.name)
                    solver.trace.flush();future_trace = old_trace.read_bytes()
                    later_streams = bridge.inventory()
                    stage = owner.prepare(solver,payload,trace_path=prefix)
                    self.assertEqual(bridge.restore(native, streams), (0,True,0));stage.apply()
                    if omitted == 'trace':
                        stage.retired, solver.trace = solver.trace, stage.retired
                    stage.discard()
                    self.assertEqual(old_trace.read_bytes(),future_trace)
                    for item in later_streams.values():
                        self.assertEqual(Path(item['path']).read_bytes(),item['data'])
                    if omitted == 'previous':solver.previous = future
                    if omitted == 'steps':solver.steps = 0
                    if omitted == 'water':
                        for row in solver.records:row['removed_volume'] = 0
                    if omitted == 'quality':
                        for row in solver.pollutants:row['removed_quantity'] = 0
                    rejected = None
                    try:
                        while not solver.step(1)['finished']:pass
                        result = finish(solver,bridge)
                    except ValueError as error:
                        rejected = str(error)
                        if omitted not in ('previous','water','quality'):raise
                    if omitted:
                        if not rejected:self.assertNotEqual(result,expected)
                    else:self.assertEqual(result,expected)
                    EVIDENCE.append(dict(kind='omission' if omitted else 'rewind',field=omitted,rejection=rejected))
                finally:solver.cleanup()

    def test_corruption_and_trace_rejection_preserve_live_session(self):
        with tempfile.TemporaryDirectory() as directory, working(directory):
            solver = open_solver(Path(directory));bridge = Bridge(solver)
            try:
                solver.step(3);payload = owner.capture(solver);before = handles()
                variants = []
                for key, value in [('time', -1), ('steps', True), ('steps', 0), ('nodes', []), ('pollutants', [])]:
                    data = JsonDocument.from_bytes(payload).data;data['state'][key] = value;variants.append(data)
                for key, value in [('version', True), ('version', 2), ('trace', None), ('unexpected', 1)]:
                    data = JsonDocument.from_bytes(payload).data;data[key] = value;variants.append(data)
                data = JsonDocument.from_bytes(payload).data;data['binding']['parameters']['policy']['external_flooding_ratio'] = .5;variants.append(data)
                data = JsonDocument.from_bytes(payload).data;data['state']['nodes'][0]['active_steps'] = 10000;variants.append(data)
                data = JsonDocument.from_bytes(payload).data;data['state']['nodes'][0]['previous']['depth'] = -1;variants.append(data)
                for data in variants:
                    with self.assertRaises(ValueError):
                        owner.prepare(solver, JsonDocument.from_data(data).to_bytes())
                    self.assertEqual(owner.capture(solver), payload)
                malformed = [payload[:-1][:-1], b'{"kind":1,"kind":2}', b'{"state":NaN}',
                             b'{"state":1e999}', b'{"state":9007199254740992}', b'[]', b'null']
                for raw in malformed:
                    with self.assertRaises((ValueError, ValidationError)):
                        owner.prepare(solver, raw)
                    self.assertEqual(owner.capture(solver), payload)
                live = Path(solver.trace.name);prefix = live.read_bytes()
                for mode in ('live','hardlink','missing','short','changed','other-output'):
                    path = Path(mode)
                    forbidden = ()
                    if mode == 'live':path = live
                    elif mode == 'hardlink':os.link(live, path)
                    elif mode == 'short':path.write_bytes(prefix[:-1])
                    elif mode == 'changed':path.write_bytes(b'X' + prefix[1:])
                    elif mode == 'other-output':path.write_bytes(prefix);forbidden = [path]
                    with self.assertRaises((ValueError, OSError)):
                        owner.prepare(solver, payload, trace_path=path, forbidden_files=forbidden)
                    self.assertEqual(owner.capture(solver), payload)
                    self.assertEqual(handles(), before)
                # Native preparation rejection must discard the staged Python
                # trace while both live Python and native owners remain usable.
                path = Path('valid');path.write_bytes(prefix)
                stage = owner.prepare(solver, payload, trace_path=path)
                native, prefixes = bridge.capture()
                code, committed, cleanup = bridge.restore(native, prefixes, fail=0)
                self.assertNotEqual(code, 0);self.assertFalse(committed);stage.discard()
                self.assertEqual(owner.capture(solver), payload)
                self.assertEqual(handles(), before)
                while not solver.step(1)['finished']:pass
                finish(solver, bridge)
                EVIDENCE.append(dict(kind='rejection', corrupted=len(variants), malformed=len(malformed), trace_modes=6, native_stage=1))
            finally:solver.cleanup()

    def test_failed_step_and_end_are_not_capture_boundaries(self):
        with tempfile.TemporaryDirectory() as directory, working(directory):
            solver = open_solver(Path(directory))
            try:
                with patch.object(solver, '_write', side_effect=OSError('injected trace write failure')):
                    with self.assertRaises(OSError):solver.step(1)
                with self.assertRaises(ValueError):owner.capture(solver)
                with self.assertRaises(ValueError):owner.prepare(solver, b'{}')
                EVIDENCE.append(dict(kind='failed-boundary'))
            finally:solver.cleanup()

    def test_trace_capture_stage_and_retired_close_failures(self):
        # MagicMock keeps its call arguments alive. On Windows Python 3.10 a
        # retained *closed* BufferedRandom still owns a kernel-backed lock.
        # Functions inject the same faults without retaining those owners.
        def fail_wrap(*args, **kwargs):raise MemoryError('injected allocation')
        def fail_describe(*args, **kwargs):raise ValueError('primary integrity failure')
        class FailingStream:
            def __init__(self, stream, operation):self.stream,self.operation=stream,operation
            def __getattr__(self, name):return getattr(self.stream,name)
            def flush(self):
                if self.operation=='flush':raise OSError('injected flush')
                return self.stream.flush()
            def close(self):
                self.stream.close()
                if self.operation=='close':raise OSError('injected close')
        with tempfile.TemporaryDirectory() as directory, working(directory):
            solver = open_solver(Path(directory));bridge = Bridge(solver)
            try:
                solver.step(3);payload = owner.capture(solver);stream = solver.trace
                solver.trace = FailingStream(stream,'flush')
                with self.assertRaises(OSError):owner.capture(solver)
                solver.trace = stream;self.assertEqual(owner.capture(solver),payload)
                prefix = Path('staged');prefix.write_bytes(Path(stream.name).read_bytes())
                before = handles()
                with patch.object(owner.io,'TextIOWrapper',new=fail_wrap):
                    with self.assertRaises(MemoryError):owner.prepare(solver,payload,trace_path=prefix)
                self.assertEqual(handles(),before);self.assertEqual(owner.capture(solver),payload)
                with patch.object(owner,'open',lambda *a,**kw:FailingStream(builtins.open(*a,**kw),'close'),create=True):
                    with patch.object(owner,'_describe',new=fail_describe):
                        with self.assertRaisesRegex(ValueError,'primary integrity failure') as caught:
                            owner.prepare(solver,payload,trace_path=prefix)
                self.assertEqual(caught.exception.checkpoint_cleanup_errors[0].message,'OSError: injected close')
                self.assertEqual(handles(),before)
                stage = owner.prepare(solver,payload,trace_path=prefix)
                stage.apply();stage.retired = FailingStream(stage.retired,'close')
                with self.assertRaises(OSError):stage.discard()
                self.assertTrue(stage.committed);stage.discard()
                self.assertTrue(stream.closed)
                del stream  # Release the test's reference to the retired buffer lock.
                self.assertEqual(handles(),before);self.assertEqual(owner.capture(solver),payload)
                while not solver.step(1)['finished']:pass
                finish(solver,bridge)
                with self.assertRaises(ValueError):owner.capture(solver)
                EVIDENCE.append(dict(kind='trace-faults',capture=1,allocation=1,retired_close=1,primary_and_cleanup=1))
            finally:solver.cleanup()


if __name__ == '__main__':
    unittest.main()
