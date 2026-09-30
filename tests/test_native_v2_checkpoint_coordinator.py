"""Exercise the non-instrumented native ABI, not private C test exports.

Fixture generators are shared with owner tests. The complete public container
and Session/Runner protocol remain separate acceptance requirements.
"""
import ctypes as c
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from easysewer.runtime._checkpoint_native import NativeCheckpoint, CheckpointError, INPUT, OUTPUT
import test_native_v2_checkpoint_worker as worker
from test_native_v2_checkpoint_writes import fixture
from test_native_v2_checkpoint_climate import fixture as climate_fixture
from test_native_v2_checkpoint_streams import combined, rdii, routing
from test_native_v2_standard_io import handles

EVIDENCE = []
BINDING = hashlib.sha256(b'coordinator integration fixture identity v1').digest()


def native_source(root, name, units='CFS'):
    if name == 'climate':
        return climate_fixture(root, days=2, file_series=True, temperature='series',
                               evaporation='TIMESERIES', snow=True, units=units)
    if name == 'combined': return combined(root)
    if name.startswith('rdii-'): return rdii(root, name.removeprefix('rdii-'))
    if name == 'routing': return routing(root)
    return fixture(root, name, units)


NATIVE_CHILD = '''import base64,json,sys
from pathlib import Path
from test_native_v2_checkpoint_coordinator import Engine,Bridge
saved=json.loads(Path(sys.argv[1]).read_text())
for path,raw in saved['files'].items():Path(path).write_bytes(base64.b64decode(raw))
solver=Engine(saved['family'],saved['source'])
try:
 bridge=Bridge(solver)
 bridge.inputs={k:base64.b64decode(v) for k,v in saved['inputs'].items()}
 prefixes={int(k):dict(v,data=base64.b64decode(v['data'])) for k,v in saved['outputs'].items()}
 result=bridge.restore(base64.b64decode(saved['state']),prefixes)
 assert result==(0,True,0),result
 for _ in range(20000):
  if not solver.step():break
 else:raise AssertionError('did not finish')
 Path('result.json').write_text(json.dumps(solver.finish(bridge)))
finally:solver.lib.swmm_close()
'''


class Bridge:
    def __init__(self, solver):
        self.api = NativeCheckpoint(solver, BINDING)
        self.counter = 0
        self.inputs = {}
        assert not hasattr(solver.lib, 'es_test_writes_save'), 'Must use clean native candidates'
        for item in self.api.inputs():
            raw = Path(item.initial_path).read_bytes()
            assert self.inputs.setdefault(item.identity, raw) == raw

    def inventory(self):
        result = {}
        for item in self.api.outputs():
            raw = Path(item.path).read_bytes()
            assert len(raw) == item.size
            result[item.index] = dict(role=item.role, text=int(item.text), data=raw, path=item.path)
        return result

    def capture(self):
        return self.api.capture(), self.inventory()

    def new_path(self, raw):
        path = Path('coordinator-prefix-' + str(self.counter))
        self.counter += 1
        path.write_bytes(raw)
        return path

    def restore(self, raw, prefixes, fail=-1):
        def output(role, index, text, size):
            if index == fail:
                raise OSError('injected output provider failure')
            item = prefixes[index]
            assert (role, text, size) == (item['role'], bool(item['text']), len(item['data']))
            return self.new_path(item['data'])
        result = self.api.restore(raw, input_provider=lambda key: self.new_path(self.inputs[key]),
                                  output_provider=output)
        return result.error, result.committed, result.cleanup_error


class Engine:
    def __init__(self, family, source):
        self.lib = c.CDLL(os.environ['EASYSEWER_CHECKPOINT_' + family.upper()])
        for name, args in [('open', [c.c_char_p]*3), ('start', [c.c_int]),
                           ('step', [c.POINTER(c.c_double)]), ('end', []), ('report', []), ('close', [])]:
            f = getattr(self.lib, 'swmm_' + name)
            f.argtypes, f.restype = args, c.c_int
        Path('run.inp').write_text(source, encoding='utf-8')
        try:
            self.check(self.lib.swmm_open(b'run.inp', b'run.rpt', b'run.out'))
            self.check(self.lib.swmm_start(1))
        except BaseException:
            self.lib.swmm_close()
            raise

    @staticmethod
    def check(code):
        assert code == 0, code

    def step(self):
        now = c.c_double()
        self.check(self.lib.swmm_step(c.byref(now)))
        return now.value

    def finish(self, bridge):
        outputs = bridge.inventory()
        self.check(self.lib.swmm_end())
        self.check(self.lib.swmm_report())
        self.check(self.lib.swmm_close())
        result = {}
        for i, item in outputs.items():
            raw = Path(item['path']).read_bytes()
            if item['role'] == 0:
                raw = re.sub(rb'(?m)^[ \t]*(?:Analysis (?:begun|ended)|Total elapsed time)[^\r\n]*', b'', raw)
            if item['text']:
                raw = raw.replace(os.fsencode(Path.cwd()), b'<workspace>')
            result[i] = (item['role'], len(raw), hashlib.sha256(raw).hexdigest())
        return result


@unittest.skipUnless(os.environ.get('EASYSEWER_CHECKPOINT_STANDARD') and os.environ.get('EASYSEWER_CHECKPOINT_CUSTOM'),
                     'Requires clean coordinator candidates')
class NativeCoordinatorTests(unittest.TestCase):
    def test_real_worker_restore_and_fresh_process_use_new_abi(self):
        child = worker.CHILD.replace('open_solver,Bridge,finish,working', 'open_solver,finish,working')
        child = child.replace('root,case,units=', 'from test_native_v2_checkpoint_coordinator import Bridge\nroot,case,units=')
        case = worker.NativeWorkerCheckpointTests()
        before = len(worker.EVIDENCE)
        with patch.object(worker, 'Bridge', Bridge), patch.object(worker, 'CHILD', child):
            case.test_real_policy_restores_outputs_trace_and_water_quality_statistics()
            case.test_fresh_process_restores_real_worker_and_all_native_owners()
            case.test_rewind_and_omission_counterexamples()
            case.test_corruption_and_trace_rejection_preserve_live_session()
        EVIDENCE.extend(worker.EVIDENCE[before:])

    def run_native(self, family, name, units, restore):
        with tempfile.TemporaryDirectory() as folder, worker.working(folder):
            root = Path(folder)
            source = native_source(root, name, units)
            solver = Engine(family, source)
            try:
                bridge = Bridge(solver)
                roles = set()
                timeline = []
                for step in range(20000):
                    if step % 3 == 0:
                        raw, outputs = bridge.capture()
                        roles.update(item['role'] for item in outputs.values())
                        if restore:
                            self.assertEqual(bridge.restore(raw, outputs), (0, True, 0))
                            self.assertEqual(bridge.api.capture(), raw)
                    now = solver.step()
                    timeline.append(now)
                    if not now:
                        break
                else:
                    self.fail('did not finish')
                result = solver.finish(bridge)
                return timeline, result, sorted(roles), len(bridge.inputs)
            finally:
                solver.lib.swmm_close()

    def test_native_complete_outputs_and_input_paths(self):
        for family in ('standard', 'custom'):
            for name in ('hydraulic', 'averages', 'runoff', 'lid', 'all', 'empty',
                         'no-routing', 'report-disabled', 'climate', 'combined',
                         'rdii-scratch', 'rdii-binary', 'rdii-text', 'routing', 'gwater', 'snow'):
                for units in ('CFS', 'CMS'):
                    with self.subTest(family=family, name=name, units=units):
                        expected = self.run_native(family, name, units, False)
                        self.assertEqual(self.run_native(family, name, units, True), expected)
                        if name == 'all':
                            self.assertEqual(expected[2], list(range(6)))
                        if name == 'climate':
                            self.assertGreater(expected[3], 0)
                        EVIDENCE.append(dict(kind='native', family=family, name=name, units=units,
                                             steps=len(expected[0]), outputs=expected[1], inputs=expected[3]))

    def test_both_engines_resume_in_fresh_process_with_relocated_resources(self):
        for family in ('standard', 'custom'):
            for name in ('all', 'combined', 'climate', 'rdii-binary', 'rdii-text', 'routing', 'gwater', 'snow'):
                with self.subTest(family=family, name=name), tempfile.TemporaryDirectory() as folder:
                    root = Path(folder)
                    for label in ('first', 'second'): (root/label).mkdir()
                    with worker.working(root/'first'):
                        # Canonical relative paths allow reconstruction in a different directory.
                        source = native_source(Path('.'), name)
                        files = {str(p):base64.b64encode(p.read_bytes()).decode() for p in Path('.').iterdir() if p.is_file()}
                        solver = Engine(family, source)
                        try:
                            bridge = Bridge(solver)
                            for _ in range(3): self.assertGreater(solver.step(), 0)
                            raw, outputs = bridge.capture()
                            self.assertNotIn(os.fsencode(root/'first'), raw)
                            saved = dict(family=family, source=source, files=files,
                                         inputs={k:base64.b64encode(v).decode() for k,v in bridge.inputs.items()},
                                         state=base64.b64encode(raw).decode(),
                                         outputs={k:dict(v,data=base64.b64encode(v['data']).decode()) for k,v in outputs.items()})
                            while solver.step(): pass
                            expected = json.loads(json.dumps(solver.finish(bridge)))
                        finally: solver.lib.swmm_close()
                    (root/'saved.json').write_text(json.dumps(saved))
                    # Remove original resources: continuation may only use the saved copies.
                    for path in (root/'first').iterdir():
                        if path.is_file(): path.unlink()
                    env = dict(os.environ, PYTHONPATH=os.pathsep.join((str(Path(__file__).parent),str(Path(__file__).parents[1]/'src'))))
                    process = subprocess.run([sys.executable,'-B','-c',NATIVE_CHILD,str(root/'saved.json')],
                                             cwd=root/'second',env=env,capture_output=True,text=True,timeout=60,
                                             creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                    self.assertEqual(process.returncode,0,process.stdout+process.stderr)
                    self.assertEqual(json.loads((root/'second'/'result.json').read_text()), expected)
                    EVIDENCE.append(dict(kind='fresh-native',family=family,name=name,outputs=expected))

    def test_rejections_callbacks_and_boundary_keep_session_usable(self):
        for family in ('standard', 'custom'):
            with self.subTest(family=family), tempfile.TemporaryDirectory() as folder, worker.working(folder):
                solver = Engine(family, fixture(Path(folder), 'all'))
                try:
                    bridge = Bridge(solver)
                    solver.step()
                    raw, prefixes = bridge.capture()
                    api = bridge.api
                    api.validate(raw)
                    before = handles()
                    calls = []
                    def forbidden(*args):
                        calls.append(args)
                        raise AssertionError('provider called before validation')
                    bad = [raw[:1], raw[:51], raw[:-1], raw+b'x']
                    for offset in (0, 8, 12, 16, 20, 51):
                        data = bytearray(raw); data[offset] ^= 1; bad.append(bytes(data))
                    for data in bad:
                        with self.assertRaises(CheckpointError): api.validate(data)
                        result = api.restore(data, input_provider=forbidden, output_provider=forbidden)
                        self.assertNotEqual(result.error, 0)
                        self.assertFalse(result.committed)
                        self.assertFalse(calls)
                        self.assertEqual(api.capture(), raw)
                        self.assertEqual(handles(), before)
                    # Insufficient capture capacity preserves the entire caller buffer.
                    buffer = c.create_string_buffer(b'Z' * (len(raw)-1), len(raw)-1)
                    size = c.c_size_t()
                    self.assertEqual(api.lib.swmm_checkpointCapture(BINDING, buffer, len(buffer), c.byref(size)), 2)
                    self.assertEqual(size.value, len(raw))
                    self.assertEqual(buffer.raw, b'Z' * len(buffer))
                    for path in ('', 'bad\0path', 'x'*4096, 'nonexistent-prefix'):
                        result = api.restore(raw, input_provider=forbidden, output_provider=lambda *args: path)
                        self.assertNotEqual(result.error, 0)
                        self.assertFalse(result.committed)
                        self.assertEqual(api.capture(), raw)
                        self.assertEqual(handles(), before)
                    # A provider may retain caller storage: restore must use its private copy.
                    original = c.create_string_buffer(raw)
                    callback_errors = []
                    def mutate(context, role, index, text, size, destination, capacity):
                        try:
                            c.memset(original, 0, len(raw))
                            path = os.fsencode(bridge.new_path(prefixes[index]['data']))
                            assert len(path) < capacity
                            c.memmove(destination, path+b'\0', len(path)+1)
                            return 0
                        except BaseException as error:
                            callback_errors.append(str(error))
                            return 7
                    committed, cleanup = c.c_int(), c.c_int()
                    code = api.lib.swmm_checkpointRestore(BINDING, original, len(raw),
                        INPUT(lambda *args: 7), OUTPUT(mutate), None, c.byref(committed), c.byref(cleanup))
                    self.assertEqual((code, committed.value, cleanup.value), (0, 1, 0))
                    self.assertFalse(callback_errors)
                    self.assertEqual(original.raw[:len(raw)], b'\0'*len(raw))
                    self.assertEqual(api.capture(), raw)
                    before = handles()
                    for error in (OSError('provider failed'), KeyboardInterrupt('cancelled')):
                        def fail(*args): raise error
                        if isinstance(error, KeyboardInterrupt):
                            with self.assertRaises(KeyboardInterrupt) as caught:
                                api.restore(raw, input_provider=fail, output_provider=fail)
                            result = caught.exception.checkpoint_result
                        else:
                            result = api.restore(raw, input_provider=fail, output_provider=fail)
                        self.assertNotEqual(result.error, 0)
                        self.assertFalse(result.committed)
                        self.assertEqual(len(result.provider_errors), 1)
                        self.assertEqual(api.capture(), raw)
                        self.assertEqual(handles(), before)
                    # Reentering coordinator callbacks is rejected, without clearing the outer guard.
                    def reenter(role, index, text, size):
                        for _ in range(2):
                            with self.assertRaises(CheckpointError) as caught: api.capture()
                            self.assertEqual(caught.exception.code, 5)
                        return bridge.new_path(prefixes[index]['data'])
                    result = api.restore(raw, input_provider=forbidden, output_provider=reenter)
                    self.assertEqual((result.error, result.committed, result.cleanup_error), (0, True, 0))
                    self.assertEqual(api.capture(), raw)
                    while solver.step(): pass
                    solver.finish(bridge)
                    with self.assertRaises(CheckpointError): api.capture()
                    EVIDENCE.append(dict(kind='abi-rejection', family=family, corruptions=len(bad)))
                finally:
                    solver.lib.swmm_close()
