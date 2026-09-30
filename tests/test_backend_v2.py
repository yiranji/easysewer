"""Execution contracts, transport faults and native cleanup without a real DLL."""

from contextlib import contextmanager
import gc
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import easysewer

from easysewer.model import Ref
from easysewer.runtime import (Backend, EngineObjects, Session, SessionCancelled,
    SessionError, SessionStateError, SessionTimeout, StandardBackend)


# A separate process deliberately misbehaves at the RPC boundary. It never
# loads C code, and does not replace the independent native result tests.
FAKE_WORKER = r'''
import hashlib, json, os, platform, struct, sys, time
mode=sys.argv[1]
if len(sys.argv)>2:sys.path.insert(0,sys.argv[2])
admission=None
for line in sys.stdin.buffer:
    q=json.loads(line); stage=q['command']; a=q['args']
    if mode=='hang:'+stage: time.sleep(30)
    if mode=='crash:'+stage:
        os.write(2,b'crash diagnostic\xff'); os._exit(23)
    if mode=='malformed:'+stage:
        print('not json',flush=True); continue
    if mode=='identity:'+stage:
        print(json.dumps(dict(id=q['id']+1,ok=True,value=None)),flush=True); continue
    if mode=='oversized:'+stage:
        os.write(1,b'X'*(16*1024*1024+1)); continue
    if mode in ('cleanup_hang','cleanup_crash') and stage=='step':
        print(json.dumps(dict(id=q['id'],event='failure',failure=dict(stage=stage,code=101,message='primary failure'))),flush=True)
        print(json.dumps(dict(id=q['id'],event='cleanup_failure',failure=dict(stage='end',code=202,message='end failure'))),flush=True)
        if mode=='cleanup_crash':os._exit(24)
        time.sleep(30)
    if mode=='fail:'+stage:
        os.write(2,b'x'*100000+b'final diagnostic\xff')
        print(json.dumps(dict(id=q['id'],ok=False,
            failure=dict(stage=stage,code=101,message='primary failure'),
            cleanup=[dict(stage='end',code=202,message='cleanup end'),dict(stage='close',code=303,message='cleanup close')])),flush=True)
        break
    if stage=='load':
        v=dict(library=a['library'],sha256='f'*64,engine_version=53000 if mode=='wrong_version' else 52004,
               platform=platform.system(),architecture=platform.machine(),abi='cdecl:'+str(struct.calcsize('P')*8))
        if a.get('execution_guard') is not None:
            from easysewer.runtime._admission import enter
            admission=enter(a['execution_guard'])
            v['execution_guard']='wrong' if mode=='invalid_admission' else a['execution_guard']['token']
    elif stage=='open':
        v=dict(groups=[['swmm:raingages',[]],['swmm:subcatchments',[]],['swmm:nodes',['O','J']],['swmm:links',['P']]],warnings=2,flow_units=0)
    elif stage=='step': v=dict(elapsed_days=0,finished=True,steps=1)
    elif stage=='end': v=dict(runoff_percent=0,flow_percent=.25,quality_percent=0)
    elif stage=='close':
        v=dict(cleanup=[dict(stage='end',code=204,message='close failed')] if mode=='cleanup_error' else [])
    else: v=None
    print(json.dumps(dict(id=q['id'],ok=True,value=v)),flush=True)
    if mode=='blocked_writer' and stage=='load':time.sleep(30)
    if stage=='close': break
'''


class BackendContractTests(unittest.TestCase):
    @contextmanager
    def fixture(self, mode='ok'):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); script=root/'worker.py'
            script.write_text(FAKE_WORKER,encoding='utf-8')
            (root/'model.inp').write_text('fixture',encoding='ascii')
            library=root/'fixture.dll';library.write_bytes(b'fixture')
            backend=StandardBackend(library=str(library))
            with patch('easysewer.runtime._process_session._worker_command', return_value=[sys.executable,'-I','-B','-u',str(script),mode,
                       str(Path(easysewer.__file__).resolve().parent.parent)]):
                yield root, backend

    def test_unconfirmed_execution_admission_is_rejected_and_worker_reaped(self):
        from easysewer.runtime import _admission, _process_session
        with self.fixture('invalid_admission') as (root,backend):
            guard=_admission.create(root);connections=[];factory=_process_session._WorkerConnection
            def traced(directory):
                value=factory(directory);connections.append(value);return value
            with patch.object(_process_session,'_WorkerConnection',traced):
                with self.assertRaisesRegex(SessionError,'did not confirm execution admission'):
                    backend.session(working_directory=root,_execution_guard=guard)
            self.assertEqual(len(connections),1)
            self.assertIsNotNone(connections[0].process.poll())
            _admission.revoke(guard)
            self.assertTrue(guard['revoked'])

    def open(self, session):
        session.open('model.inp','model.rpt','model.out',expected=(('swmm:nodes',('J','O')),))

    def test_protocol_state_machine_native_order_ascii_names_and_guards(self):
        with self.fixture() as (root,backend):
            self.assertIsInstance(backend,Backend)
            with backend.session(working_directory=root) as session:
                self.assertIsInstance(session,Session)
                with self.assertRaises(SessionStateError):session.step()
                self.open(session)
                self.assertEqual(session.objects.index(Ref(collection='swmm:nodes',key='j')),1)
                self.assertEqual(session.warnings,2)
                for call in (lambda:session.start(save_results=1),lambda:session.step(max_steps=True)):
                    with self.assertRaises((TypeError,SessionStateError)):call()
                session.start()
                with self.assertRaises(ValueError):session.step(max_steps=True)
                self.assertTrue(session.step().finished)
                with self.assertRaises(SessionStateError):session.step()
                self.assertEqual(session.end().flow_percent,.25)
                session.report()
                with self.assertRaises(SessionStateError):session.report()
            self.assertEqual(session.returncode,0)
            session.close()
            with self.assertRaises(SessionStateError):session.start()
        objects=EngineObjects(groups=(('swmm:nodes',('ß','SS')),))
        self.assertEqual(objects.index(Ref(collection='swmm:nodes',key='ß')),0)
        with self.assertRaises(ValueError):EngineObjects(groups=(('swmm:nodes',('j','J')),))
        with self.assertRaises(ValueError):objects.verify((('swmm:nodes',('ß','SS','ss')),))
        with self.assertRaises(TypeError):EngineObjects(groups=[])

    def test_each_native_stage_preserves_primary_and_both_cleanup_errors(self):
        for stage in ('load','open','start','step','end','report','close'):
            with self.subTest(stage=stage),self.fixture('fail:'+stage) as (root,backend):
                session=None
                with self.assertRaises(SessionError) as raised:
                    session=backend.session(working_directory=root)
                    with session:
                        self.open(session);session.start();session.step();session.end();session.report()
                error=raised.exception
                self.assertEqual((error.failure.stage,error.failure.code),(stage,101))
                self.assertEqual(tuple((f.stage,f.code) for f in error.cleanup),(('end',202),('close',303)))
                self.assertIn('final diagnostic\\xff',error.stderr)
                self.assertLessEqual(len(error.stderr),65540)
                self.assertIsNotNone(error.returncode)
                if session:
                    self.assertIsNotNone(session.returncode)
                    session.close()

    def test_transport_crash_corrupt_frames_oversize_and_wrong_request_are_reaped(self):
        for mode in ('crash','malformed','identity','oversized'):
            with self.subTest(mode=mode),self.fixture(mode+':step') as (root,backend):
                with backend.session(working_directory=root) as session:
                    self.open(session);session.start()
                    with self.assertRaises(SessionError) as caught:session.step()
                    self.assertEqual(caught.exception.failure.stage,'step')
                    self.assertIsNotNone(session.returncode)
                    if mode=='crash':
                        self.assertEqual(session.returncode,23)
                        self.assertIn('crash diagnostic\\xff',caught.exception.stderr)

    def test_timeout_in_native_call_and_blocked_request_writer(self):
        for mode in ('hang:step','blocked_writer'):
            with self.subTest(mode=mode),self.fixture(mode) as (root,backend):
                began=time.monotonic()
                with self.assertRaises(SessionTimeout):
                    with backend.session(working_directory=root,call_timeout=.6) as session:
                        if mode=='blocked_writer':session._rpc('open',{'paths':['x'*400000]})
                        else:self.open(session);session.start();session.step()
                self.assertLess(time.monotonic()-began,5)

    def test_primary_failure_survives_cleanup_hang_or_crash(self):
        for mode in ('cleanup_hang','cleanup_crash'):
            with self.subTest(mode=mode),self.fixture(mode) as (root,backend):
                with backend.session(working_directory=root,call_timeout=.6) as session:
                    self.open(session);session.start()
                    with self.assertRaises(SessionError) as caught:session.step()
                    self.assertEqual(caught.exception.failure.code,101)
                    self.assertEqual(caught.exception.cleanup[0].code,202)
                    self.assertEqual(caught.exception.cleanup[1].stage,'cleanup')
                    self.assertIsNotNone(session.returncode)

    def test_cross_thread_and_external_cancellation_interrupt_blocked_native_call(self):
        for external in (False,True):
            with self.subTest(external=external),self.fixture('hang:step') as (root,backend):
                event=threading.Event()
                with backend.session(working_directory=root,cancel_event=event if external else None) as session:
                    self.open(session);session.start()
                    timer=threading.Timer(.15,event.set if external else session.cancel)
                    timer.start()
                    try:
                        began=time.monotonic()
                        with self.assertRaises(SessionCancelled):session.step()
                        self.assertLess(time.monotonic()-began,3)
                        self.assertIsNotNone(session.returncode)
                    finally:timer.join()
                event.set()
                with self.assertRaises(SessionCancelled):backend.session(working_directory=root,cancel_event=event)

    def test_user_exception_survives_close_failure_and_cleanup_stays_inspectable(self):
        with self.fixture('cleanup_error') as (root,backend):
            marker=LookupError('user callback')
            with self.assertRaises(LookupError) as caught:
                with backend.session(working_directory=root) as session:
                    raise marker
            self.assertIs(caught.exception,marker)
            self.assertEqual(session.cleanup_errors[0].code,204)
            self.assertEqual(session.state,'CLOSED')
            with self.assertRaises(SessionError) as caught:
                with backend.session(working_directory=root):pass
            self.assertEqual(caught.exception.failure.code,204)

    def test_path_alias_overwrite_preflight_and_snapshot_mismatch(self):
        with self.fixture() as (root,backend):
            with backend.session(working_directory=root) as session:
                for args,error in ((('absent.inp','a.rpt','a.out'),FileNotFoundError),
                    (('model.inp','model.inp','a.out'),ValueError),
                    (('model.inp','same','same'),ValueError),
                    (('model.inp','absent/a.rpt','a.out'),ValueError)):
                    with self.assertRaises(error):session.open(*args)
                    self.assertEqual(session.state,'LOADED')
                (root/'a.rpt').write_text('do not overwrite',encoding='ascii')
                with self.assertRaises(FileExistsError):session.open('model.inp','a.rpt','a.out')
                self.assertEqual((root/'a.rpt').read_text(),'do not overwrite')
                with self.assertRaises(ValueError):
                    session.open('model.inp','a.rpt','a.out',overwrite=True,expected=(('swmm:nodes',('Wrong',)),))
                self.assertEqual(session.state,'CLOSED')
                self.assertIsNotNone(session.returncode)

    def test_probe_checks_runtime_version_loadability_and_launch_failure(self):
        with self.fixture('wrong_version') as (_,backend):
            info=backend.probe()
            self.assertFalse(info.available)
            self.assertEqual(info.engine_version,53000)
            self.assertIn('5.2.4',info.reason)
        with self.fixture() as (_,backend),patch('easysewer.runtime._process_session.subprocess.Popen',side_effect=OSError('cannot spawn')):
            self.assertIn('cannot spawn',backend.probe().reason)
        original_start=threading.Thread.start
        original_launch=subprocess.Popen
        spawned=[];starts=[]
        def launch(*args,**kwargs):
            process=original_launch(*args,**kwargs);spawned.append(process);return process
        def start(thread):
            starts.append(thread)
            if len(starts)==2:raise RuntimeError('cannot create transport thread')
            original_start(thread)
        with self.fixture() as (_,backend),patch('easysewer.runtime._process_session.subprocess.Popen',side_effect=launch),patch('threading.Thread.start',start):
            self.assertIn('cannot create transport thread',backend.probe().reason)
        self.assertEqual(len(spawned),1)
        self.assertIsNotNone(spawned[0].poll())
        with patch('easysewer.runtime.native.probe_library_path',return_value=None):
            self.assertFalse(StandardBackend().probe().available)
        with self.assertRaises(ValueError):StandardBackend(library='relative')
        with self.assertRaises(ValueError):StandardBackend(expected_sha256='invalid')

    def test_finalizer_reaps_abandoned_worker_and_does_not_hold_session_alive(self):
        with self.fixture() as (root,backend):
            session=backend.session(working_directory=root)
            process=session._connection.process
            del session;gc.collect()
            self.assertIsNotNone(process.poll())

    def test_native_cleanup_attempts_close_after_failed_end_once(self):
        from easysewer.runtime._native_solver import NativeCallFailure, NativeSolver
        class Library:
            def __init__(self):self.calls=[]
            def swmm_end(self):self.calls.append('end');return 100
            def swmm_close(self):self.calls.append('close');return 200
        solver=object.__new__(NativeSolver)
        solver.lib=Library();solver.closed=False;solver.start_attempted=True;solver.open_attempted=True;solver.ended=False
        def check(stage,code):raise NativeCallFailure(stage,code,stage+' failed')
        solver.check=check
        self.assertEqual(tuple(e.code for e in solver.cleanup()),(100,200))
        self.assertEqual(solver.cleanup(),[])
        self.assertEqual(solver.lib.calls,['end','close'])

    def test_cleanup_diagnostic_distinguishes_return_code_from_retained_primary(self):
        from easysewer.runtime._native_solver import NativeCallFailure, NativeSolver
        class Library:
            def swmm_getError(self, buffer, size):
                buffer.value = b'Output metadata exceeded its size limit'
                return 308
        solver = object.__new__(NativeSolver)
        solver.lib = Library()
        with self.assertRaises(NativeCallFailure) as caught:
            solver.check('close', 309)
        failure = caught.exception.failure
        self.assertEqual((failure.stage, failure.code), ('close', 309))
        self.assertIn('Native returned 309; retained engine error 308:', failure.message)
        self.assertIn('size limit', failure.message)

    def test_import_and_probe_never_load_ctypes_into_parent(self):
        root=str(Path(__file__).resolve().parents[1]/'src')
        code='import sys;sys.path.insert(0,'+repr(root)+');from easysewer.runtime import StandardBackend; b=StandardBackend(); b.probe(); assert "ctypes" not in sys.modules; assert "easysewer.runtime._solver_api" not in sys.modules'
        subprocess.run([sys.executable,'-I','-B','-c',code],check=True,capture_output=True,timeout=30)

    def test_real_worker_exits_on_parent_eof_during_call_or_unexpected_cleanup(self):
        package=str(Path(__file__).resolve().parents[1]/'src')
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);marker=root/'entered.txt';script=root/'blocked.py'
            script.write_text('import sys;sys.path.insert(0,'+repr(package)+')\n'+r'''
import pathlib, time, types
module=types.ModuleType('easysewer.runtime._native_solver')
class Solver:
    def __init__(self,*a,**kw):self.metadata={}
    def open(self,paths):
        pathlib.Path(sys.argv[1]).write_text('entered')
        time.sleep(30)
    def cleanup(self,**kw):
        if sys.argv[2]=='cleanup':
            pathlib.Path(sys.argv[1]).write_text('cleanup')
            time.sleep(30)
        return []
module.NativeSolver=Solver
module.NativeCallFailure=type('NativeCallFailure',(Exception,),{})
sys.modules[module.__name__]=module
from easysewer.runtime._solver_worker import main
main()
''',encoding='utf-8')
            for mode in ('call','cleanup'):
                marker.unlink(missing_ok=True)
                process=subprocess.Popen([sys.executable,'-I','-B',str(script),str(marker),mode],stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,stderr=subprocess.PIPE,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                try:
                    process.stdin.write(json.dumps(dict(version=1,id=1,command='load',args=dict(library='fixture'))).encode()+b'\n')
                    process.stdin.flush()
                    self.assertTrue(json.loads(process.stdout.readline())['ok'])
                    request=dict(version=1,id=2,command='open',args=dict(paths=[])) if mode=='call' else []
                    process.stdin.write(json.dumps(request).encode()+b'\n')
                    process.stdin.flush()
                    deadline=time.monotonic()+3
                    while not marker.exists() and time.monotonic()<deadline:time.sleep(.01)
                    self.assertTrue(marker.exists())
                    process.stdin.close()
                    self.assertEqual(process.wait(timeout=3),74)
                finally:
                    if process.poll() is None:process.kill();process.wait(timeout=3)
                    for pipe in (process.stdin,process.stdout,process.stderr):pipe.close()


if __name__=='__main__':unittest.main()
