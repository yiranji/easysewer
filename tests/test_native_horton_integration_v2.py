"""Actual old/new solvers, full worker continuation and cross-revision rejection."""
from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from easysewer.io.inp import InpDocument
from easysewer.model import Model, Ref, FileReference
from easysewer.model.files import InterfaceFile
from easysewer.runtime import (StandardBackend, FlexiblePondingBackend, Runner,
    CheckpointSchedule, RunnerCheckpoint, ResumeConfig, RunResult, RunConfig)
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
from qualify_horton_network import fixture

EVIDENCE=[]


def config(path, **options):
    return RunConfig(output_directory=FileReference(path=str(path),direction='output'),**options)


def bind(value, kind, mode, path):
    value.files.add(InterfaceFile(kind=kind,mode=mode,file=FileReference(
        path=str(path),direction='input' if mode=='USE' else 'output')))


def model(units, case):
    text=fixture(units,case).replace('FLOW_ROUTING KINWAVE','FLOW_ROUTING DYNWAVE\nALLOW_PONDING YES')
    text=text.replace('S R O ', 'S R J ')
    text+='''[JUNCTIONS]
J 2 3 0 0 20
[CONDUITS]
P J O 100 .013 0 0
[XSECTIONS]
P RECT_OPEN 3 3 0 0 1
'''
    return Model.from_document(InpDocument.from_text(text),strict=True)


def selected(family, *, old=False):
    cls=StandardBackend if family=='standard' else FlexiblePondingBackend
    name='EASYSEWER_HORTON_'+('OLD_' if old else '')+family.upper()
    path=os.environ.get(name)
    return cls(library=path) if path else cls()


def engine(value):
    return Runner(backends={value.key:value})


def resume_child(checkpoint, destination, family):
    backend=selected(family)
    result=engine(backend).resume(RunnerCheckpoint.load(checkpoint),
        ResumeConfig(output_directory=FileReference(path=destination,direction='output'),step_batch_size=19))
    if not result.succeeded:
        raise RuntimeError((result.failure,result.diagnostics))
    result.save(Path(destination)/'archive')


@unittest.skipUnless(os.environ.get('EASYSEWER_HORTON_OLD_STANDARD') and
                     os.environ.get('EASYSEWER_HORTON_OLD_CUSTOM'), 'Requires previous qualified native libraries')
class HortonIntegrationTests(unittest.TestCase):
    def success(self, result):
        self.assertTrue(result.succeeded,repr(result.failure)+' '+repr(result.diagnostics))
        return result

    def run_model(self, value, path, backend, **kwargs):
        return self.success(engine(backend).run(value,config(path,backend=backend.key,step_batch_size=37),**kwargs))

    def test_nine_hydrology_cases_both_families_and_units(self):
        for family in ('standard','custom'):
            old,new=selected(family,old=True),selected(family)
            oi,ni=old.probe(),new.probe()
            self.assertTrue(oi.available,oi.reason);self.assertTrue(ni.available,ni.reason)
            self.assertNotEqual(oi.sha256,ni.sha256)
            self.assertNotEqual(oi.numerical_policy,ni.numerical_policy)
            self.assertNotIn('easysewer:horton-capacity:1',oi.capabilities)
            self.assertIn('easysewer:horton-capacity:1',ni.capabilities)
            self.assertEqual(dict(ni.output_semantics)['swmm:modified-horton'],'easysewer:horton-capacity:1')
            for units in ('CFS','CMS'):
                for case in ('horton','green-ampt','modified-green-ampt','curve-number','unlimited',
                             'constant-rate','zero-decay','finite-cap','small-cap'):
                    with self.subTest(family=family,units=units,case=case),tempfile.TemporaryDirectory() as temporary:
                        root=Path(temporary);value=model(units,case)
                        a=self.run_model(value,root/'old',old)
                        b=self.run_model(value,root/'new',new)
                        affected=case in ('finite-cap','small-cap')
                        self.assertEqual(a.output.read_bytes()==b.output.read_bytes(),not affected)
                        with a.open_output() as left,b.open_output() as right:
                            target=Ref(collection='swmm:subcatchments',key='S')
                            before=left.series(target,'swmm:infiltration').values
                            after=right.series(target,'swmm:infiltration').values
                            self.assertEqual(len(before),len(after));self.assertGreater(len(after),3500)
                            self.assertGreater(max(after),0)
                            if affected:self.assertGreater(sum(after),sum(before))
                        EVIDENCE.append(dict(kind='whole-model',family=family,units=units,case=case,
                            previous_sha256=oi.sha256,candidate_sha256=ni.sha256,
                            old_out=hashlib.sha256(a.output.read_bytes()).hexdigest(),
                            new_out=hashlib.sha256(b.output.read_bytes()).hexdigest(),
                            old_balance=repr(a.mass_balance),new_balance=repr(b.mass_balance)))

    def test_finite_capacity_fresh_parent_resume_and_revision_boundaries(self):
        for family in ('standard','custom'):
            for units in ('CFS','CMS'):
                with self.subTest(family=family,units=units),tempfile.TemporaryDirectory() as temporary:
                    root=Path(temporary);base=model(units,'finite-cap')
                    old,new=selected(family,old=True),selected(family)
                    produced={};saved={}
                    for label,backend in [('old',old),('new',new)]:
                        value=base.copy();bind(value,'RUNOFF','SAVE',root/(label+'.runoff'))
                        bind(value,'HOTSTART','SAVE',root/(label+'.hsf'))
                        checkpoints=[]
                        schedule=CheckpointSchedule(directory=FileReference(path=str(root/(label+'-saved')),direction='output'),
                            interval=timedelta(seconds=600),on_saved=checkpoints.append)
                        result=self.run_model(value,root/label,backend,checkpoints=schedule)
                        self.assertTrue(checkpoints)
                        # Checkpoints are captured after a completed 37-step
                        # batch; the 600-second interval is a lower bound.
                        self.assertGreaterEqual(checkpoints[0].simulation_seconds,600.)
                        self.assertLess(checkpoints[0].simulation_seconds,637.)
                        produced[label]=result;saved[label]=checkpoints[0]
                    expected=produced['new'];expected.save(root/'expected')
                    command=('import sys; sys.path[:0]='+repr(sys.path)+'; '
                             'from test_native_horton_integration_v2 import resume_child; '
                             'resume_child(*sys.argv[1:])')
                    completed=subprocess.run([sys.executable,'-B','-c',command,str(saved['new'].directory),
                        str(root/'resumed'),family],capture_output=True,text=True,timeout=120)
                    self.assertEqual(completed.returncode,0,completed.stdout+completed.stderr)
                    resumed=RunResult.load(root/'resumed/archive')
                    self.assertEqual(expected.output.read_bytes(),resumed.output.read_bytes())
                    self.assertEqual(expected.mass_balance,resumed.mass_balance)
                    self.assertEqual(expected.backend_results,resumed.backend_results)
                    self.assertEqual([x.artifact.read_bytes() for x in expected.produced_caches],
                                     [x.artifact.read_bytes() for x in resumed.produced_caches])
                    for artifact in expected.artifacts:
                        if artifact.role=='easysewer:flexible-ponding-steps':
                            self.assertEqual(artifact.read_bytes(),resumed.artifact(artifact.role).read_bytes())
                    # Reject an old checkpoint before opening the solver or replacing outputs.
                    rejected=root/'rejected';rejected.mkdir()
                    for filename in ('model.inp','model.rpt','model.out'):
                        (rejected/filename).write_bytes(b'previous-success')
                    phases=[]
                    result=engine(new).resume(saved['old'],ResumeConfig(
                        output_directory=FileReference(path=str(rejected),direction='output'),overwrite=True),
                        progress=lambda event:phases.append(event.phase))
                    self.assertEqual(result.status,'rejected')
                    self.assertIn('run.checkpoint_backend',{d.code for d in result.diagnostics.errors})
                    self.assertNotIn('opening',phases)
                    for filename in ('model.inp','model.rpt','model.out'):
                        self.assertEqual((rejected/filename).read_bytes(),b'previous-success')
                    for label in ('old','new'):
                        evidence=next(item for item in produced[label].produced_caches if item.kind=='RUNOFF')
                        consumer=base.copy();bind(consumer,'RUNOFF','USE',evidence.artifact.path)
                        phases=[]
                        result=engine(new).run(consumer,config(root/('consume-'+label),backend=new.key,
                            cache_reuse=(('RUNOFF','require_match'),)),producers={'RUNOFF':evidence},
                            progress=lambda event:phases.append(event.phase))
                        if label=='old':
                            self.assertEqual(result.status,'rejected')
                            self.assertIn('run.cache_engine',{d.code for d in result.diagnostics.errors})
                            self.assertNotIn('opening',phases)
                        else:
                            self.success(result)
                            self.assertEqual(result.consumed_caches[0].reuse.status,'matched')
                    EVIDENCE.append(dict(kind='continuation',family=family,units=units,
                        split_seconds=saved['new'].simulation_seconds,fresh_parent=True,full_output_exact=True,cache_bytes_exact=True,
                        old_checkpoint_rejected_before_open=True,old_runoff_rejected_before_open=True,
                        matching_new_runoff_reused=True))


if __name__=='__main__':
    unittest.main()
