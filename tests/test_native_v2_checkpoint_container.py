"""Real solver/container integration; public worker RPC is a later layer."""
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

from easysewer.io.inp import InpDocument
from easysewer.io.json import JsonDocument
from easysewer.model import Model
from easysewer.runtime import FlexiblePondingBackend, FlexiblePondingPolicy
from easysewer.runtime._checkpoint_container import Builder, load, execution_digest
from easysewer.runtime._checkpoint_native import NativeCheckpoint
from easysewer.runtime import _checkpoint_worker as worker_state
from easysewer.runtime._native_solver import NativeSolver
from easysewer.runtime._native_flexible import NativeFlexibleSolver
from easysewer.runtime._preparation import inventory, stage
from easysewer.runtime.backend import BackendInfo
from easysewer.runtime.results import RunSnapshot
from test_native_v2_checkpoint_coordinator import native_source
from test_native_v2_checkpoint_worker import working, model_for
from test_flexible_v2 import configuration
from test_runner_v2 import config

EVIDENCE=[]


def open_solver(family, snapshot=None):
    solver=(NativeSolver if family=='standard' else NativeFlexibleSolver)(os.environ['EASYSEWER_CHECKPOINT_'+family.upper()])
    try:
        solver.open(['model.inp','model.rpt','model.out'])
        if family=='custom':
            assert snapshot is not None
            solver.configure(JsonDocument.from_bytes(snapshot.backend_settings).data)
        solver.start(True)
        return solver
    except BaseException:
        solver.cleanup();raise


def prepare(root, family, case):
    custom=family=='custom'
    if case in ('normal','adaptive','averages','no-trace'):
        model=model_for(case)
    else:
        source=native_source(root,case)
        if case=='combined':
            # The native-only observation fixture deliberately shares one
            # numeric probe curve between intensity and accumulated depth.
            # The Model contract requires distinct dimensional consumers.
            source=source.replace('RainProbe CONTROL 0 0 1000 1',
                                  'RainProbe CONTROL 0 0 1000 1\nIntensityProbe CONTROL 0 0 1000 1')
            source=source.replace('THEN OUTLET Probe0 SETTING = CURVE RainProbe',
                                  'THEN OUTLET Probe0 SETTING = CURVE IntensityProbe')
        model=Model.from_document(InpDocument.from_text(source),strict=True)
    if custom:model.update_options(allow_ponding=True)
    policy=FlexiblePondingPolicy(external_flooding_ratio=.37,depth_threshold_m=0,
                               flow_threshold_cms=0,record_steps=case!='no-trace')
    settings=configuration(root/'published',policy) if custom else config(root/'published')
    original=model.to_json_document().to_bytes()
    plans=inventory(model,input_directory=root,working_directory=root)
    resources,_,_=stage(model,plans,root,'assets',checkpoint=lambda:None)
    source=model.to_document().text.encode('utf-8');Path('model.inp').write_bytes(source)
    # Loading does not yet open/start the solver; capture its actual binary identity.
    probe=(NativeFlexibleSolver if custom else NativeSolver)(os.environ['EASYSEWER_CHECKPOINT_'+family.upper()])
    metadata=probe.metadata;probe.cleanup()
    info=BackendInfo(key='easysewer:flexible-ponding' if custom else 'swmm:standard',available=True,
        reason=None,**metadata,profiles=(model.profile.key,),capabilities=(),isolation='inprocess-test')
    snapshot=RunSnapshot(run_id='container-fixture',created_at=datetime(2020,1,1,tzinfo=timezone.utc),
        model_json=original,config_json=settings.to_json_document().to_bytes(),model_sha256=hashlib.sha256(original).hexdigest(),
        input_bytes=source,input_sha256=hashlib.sha256(source).hexdigest(),profile=model.profile,units=model.units,
        options=model.effective_options,backend=info,resources=resources,execution_directory=str(root))
    if custom:
        plan=FlexiblePondingBackend().prepare_run(model,settings,snapshot,artifact_directory=Path('assets/backend'))
        snapshot=replace(snapshot,backend_settings=plan.parameters.to_bytes())
        Path('assets/backend').mkdir()
    return snapshot,open_solver(family,snapshot)


def capture(solver,snapshot,destination):
    probe=NativeCheckpoint(solver,b'\0'*32)
    with Builder(destination,snapshot) as builder:
        for item in probe.inputs():builder.add_input(item.identity,item.initial_path)
        api=NativeCheckpoint(solver,builder.binding)
        state=api.capture()
        worker=worker_state.capture(solver) if isinstance(solver,NativeFlexibleSolver) else None
        return builder.finish(state,api.outputs(),worker_state=worker,
                              trace=solver.trace.name if worker is not None and solver.trace else None)


def restore(solver,archive):
    api=NativeCheckpoint(solver,archive.binding)
    inputs=[]
    for item in api.inputs():
        raw=Path(item.initial_path).read_bytes()
        inputs.append(dict(identity=item.identity,blob=dict(sha256=hashlib.sha256(raw).hexdigest(),size=len(raw))))
    assert execution_digest(archive.snapshot,sorted(inputs,key=lambda i:i['identity']))==archive.binding.hex()
    for name in ('sha256','engine_version','platform','architecture','abi'):
        assert solver.metadata[name]==getattr(archive.snapshot.backend,name)
    api.validate(archive.native_state)
    data=archive.data;folder=Path('restored');folder.mkdir()
    input_paths={};output_paths={}
    for index,item in enumerate(data['native_inputs']):
        input_paths[item['identity']]=archive.copy_blob(item['blob'],folder/('i'+str(index)))
    for item in data['outputs']:
        output_paths[item['index']]=archive.copy_blob(item['blob'],folder/('o'+str(item['index'])))
    trace=archive.copy_blob(data['trace'],folder/'trace') if data['trace'] is not None else None
    python=None
    try:
        if archive.worker_state is not None:
            python=worker_state.prepare(solver,archive.worker_state,trace_path=trace,
                                        forbidden_files=[*output_paths.values(),*(item.path for item in api.outputs())])
        def output(role,index,text,size):
            item=data['outputs'][index]
            assert (role,text,size)==(item['role'],item['text'],item['blob']['size'])
            return output_paths[index]
        result=api.restore(archive.native_state,input_provider=input_paths.__getitem__,output_provider=output)
        assert (result.error,result.committed,result.cleanup_error)==(0,True,0),result
        if python:python.apply()
    finally:
        if python:python.discard()
    return api


def finish(solver,api):
    outputs=api.outputs();trace=Path(solver.trace.name) if isinstance(solver,NativeFlexibleSolver) and solver.trace else None
    for _ in range(20000):
        if solver.step(3)['finished']:break
    else:raise AssertionError('did not finish')
    balance=solver.end();solver.report()
    result=solver.execution_results() if isinstance(solver,NativeFlexibleSolver) else None
    assert not solver.cleanup()
    hashes={}
    for item in outputs:
        raw=Path(item.path).read_bytes()
        if item.role==0:raw=re.sub(rb'(?m)^[ \t]*(?:Analysis (?:begun|ended)|Total elapsed time)[^\r\n]*',b'',raw)
        if item.text:raw=raw.replace(os.fsencode(Path.cwd()),b'<workspace>')
        hashes[str(item.index)]=dict(role=item.role,sha256=hashlib.sha256(raw).hexdigest(),size=len(raw))
    return dict(outputs=hashes,trace=hashlib.sha256(trace.read_bytes()).hexdigest() if trace else None,
                balance=balance,results=result)


CHILD='''import json,sys
from pathlib import Path
from test_native_v2_checkpoint_container import load,open_solver,restore,finish,working
archive=load(sys.argv[1]);root=Path(sys.argv[2]);snapshot=archive.materialize(root)
with working(root):
 solver=open_solver(sys.argv[3],snapshot)
 try:
  api=restore(solver,archive)
  (root/'result.json').write_text(json.dumps(finish(solver,api)))
 finally:solver.cleanup()
'''


@unittest.skipUnless(os.environ.get('EASYSEWER_CHECKPOINT_STANDARD') and os.environ.get('EASYSEWER_CHECKPOINT_CUSTOM'),
                     'Requires clean ABI2 candidate libraries')
class NativeCheckpointContainerTests(unittest.TestCase):
    def test_real_archives_resume_after_original_directory_is_deleted(self):
        cases=[('standard',x) for x in ('all','climate','combined','rdii-text','routing','gwater','snow')]
        cases += [('custom',x) for x in ('normal','adaptive','averages','no-trace','climate')]
        for family,case in cases:
            with self.subTest(family=family,case=case),tempfile.TemporaryDirectory() as directory:
                root=Path(directory);first=root/'first';first.mkdir()
                with working(first):
                    snapshot,solver=prepare(first,family,case)
                    try:
                        solver.step(3)
                        archive=capture(solver,snapshot,root/'saved')
                        expected=finish(solver,NativeCheckpoint(solver,archive.binding))
                    finally:solver.cleanup()
                shutil.rmtree(first)
                (root/'saved').rename(root/'搬移存档')
                env=dict(os.environ,PYTHONPATH=os.pathsep.join((str(Path(__file__).parent),str(Path(__file__).parents[1]/'src'))))
                p=subprocess.run([sys.executable,'-B','-c',CHILD,str(root/'搬移存档'),str(root/'second'),family],
                                 env=env,capture_output=True,text=True,timeout=90,
                                 creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                self.assertEqual(p.returncode,0,p.stdout+p.stderr)
                self.assertEqual(json.loads((root/'second/result.json').read_bytes()),expected)
                EVIDENCE.append(dict(kind='fresh-container',family=family,case=case,result=expected))

    def test_mixed_native_worker_times_reject_without_changing_live_solver(self):
        with tempfile.TemporaryDirectory() as directory,working(directory):
            root=Path(directory);snapshot,solver=prepare(root,'custom','normal')
            try:
                solver.step(3);saved=capture(solver,snapshot,root/'saved')
                solver.step(1)
                api=NativeCheckpoint(solver,saved.binding)
                before=api.capture();python=worker_state.capture(solver)
                with self.assertRaisesRegex(ValueError,'clocks disagree'):
                    with Builder(root/'mixed',snapshot) as builder:
                        for item in api.inputs():builder.add_input(item.identity,item.initial_path)
                        self.assertEqual(builder.binding,saved.binding)
                        builder.finish(saved.native_state,api.outputs(),worker_state=python,trace=solver.trace.name)
                self.assertFalse((root/'mixed').exists())
                self.assertEqual(api.capture(),before)
                self.assertEqual(worker_state.capture(solver),python)
                finish(solver,api)
                EVIDENCE.append(dict(kind='mixed-clock-rejection'))
            finally:solver.cleanup()
