"""Current Horton physics, deterministic workers and explicit identity boundaries.

No historical binary is required or simulated. Repeated current runs establish
worker determinism, while rainfall budgets and final native state independently
exercise the finite-capacity correction. Synthetic probe identities below test
Runner's rejection policy only; they are not evidence about an older solver.
"""
from dataclasses import replace
from datetime import timedelta
import hashlib
import math
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import easysewer
from easysewer.io.hotstart import HotstartData, HotstartLayout
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


def routed_model(text):
    text=text.replace('FLOW_ROUTING KINWAVE','FLOW_ROUTING DYNWAVE\nALLOW_PONDING YES')
    text=text.replace('S R O ', 'S R J ')
    text+='''[JUNCTIONS]
J 2 3 0 0 20
[CONDUITS]
P J O 100 .013 0 0
[XSECTIONS]
P RECT_OPEN 3 3 0 0 1
'''
    return Model.from_document(InpDocument.from_text(text),strict=True)


def model(units, case):
    return routed_model(fixture(units,case))


def selected(family):
    cls=StandardBackend if family=='standard' else FlexiblePondingBackend
    path=os.environ.get('EASYSEWER_HORTON_'+family.upper())
    return cls(library=path) if path else cls()


def engine(value):
    return Runner(backends={value.key:value})


def resume_child(checkpoint, destination, family, package_file):
    if Path(easysewer.__file__).resolve()!=Path(package_file).resolve():
        raise RuntimeError('Resume child imported a different easysewer package')
    backend=selected(family)
    result=engine(backend).resume(RunnerCheckpoint.load(checkpoint),
        ResumeConfig(output_directory=FileReference(path=destination,direction='output'),step_batch_size=19))
    if not result.succeeded:
        raise RuntimeError((result.failure,result.diagnostics))
    result.save(Path(destination)/'archive')


class HortonAssertions:
    """Shared assertions, not additional discovered tests."""

    def success(self, result):
        self.assertTrue(result.succeeded,repr(result.failure)+' '+repr(result.diagnostics))
        return result

    def run_model(self, value, path, backend, *, batch=37, **kwargs):
        return self.success(engine(backend).run(value,
            config(path,backend=backend.key,step_batch_size=batch),**kwargs))

    def current_backend(self, family):
        backend=selected(family);info=backend.probe()
        self.assertTrue(info.available,info.reason)
        self.assertEqual(info.sha256,hashlib.sha256(Path(info.library).read_bytes()).hexdigest())
        for capability in ('horton-capacity:1','horton-state:1','outfall-gate:1','checkpoint:2'):
            self.assertIn('easysewer:'+capability,info.capabilities)
        self.assertEqual(dict(info.output_semantics)['swmm:modified-horton'],'easysewer:horton-state:1')
        return backend,info

    def hydrology(self, result, value, units, case):
        scale=25.4 if units in ('CMS','LPS','MLD') else 1.
        with result.open_output() as output:
            target=Ref(collection='swmm:subcatchments',key='S')
            series={name:tuple(output.series(target,'swmm:'+name).values)
                    for name in ('rainfall','infiltration','runoff')}
        rain=tuple(v/scale for v in series['rainfall'])
        infiltration=tuple(v/scale for v in series['infiltration'])
        # The literal 2020-01-01 one-hour interval floors to 3599 seconds in
        # SWMM 5.2.4. Each rate sample covers one report second.
        self.assertEqual({len(v) for v in series.values()},{3599})
        for values in series.values():
            self.assertTrue(all(math.isfinite(v) and v>=0 for v in values))
        self.assertAlmostEqual(sum(rain)/3600,1./3.,delta=1e-7)
        self.assertLessEqual(sum(infiltration),sum(rain)+1e-4)
        self.assertTrue(all(math.isfinite(v) for v in result.mass_balance.raw_percentages))
        # This is a runoff budget assertion, not a new routing-accuracy claim.
        self.assertLess(abs(result.mass_balance.runoff_percent),.01)
        hotstart=next(item for item in result.produced_caches if item.kind=='HOTSTART')
        raw=hotstart.artifact.read_bytes()
        state=HotstartData.from_bytes(raw,layout=HotstartLayout.from_model(value)).subcatchments[0]
        soil=state.infiltration.named_values
        if state.infiltration.method in ('HORTON','MODIFIED_HORTON'):
            # Independent v4 layout: 15-byte stamp, six int32 counts, four
            # catchment doubles, then native tp (time) and Fe (feet).
            self.assertEqual(raw[:15],b'SWMM5-HOTSTART4')
            self.assertEqual(struct.unpack_from('<2d',raw,39+32),(soil['tp'],soil['Fe']))
        if state.infiltration.method=='MODIFIED_HORTON':
            self.assertGreaterEqual(soil['Fe'],0.)
            cap=value.subcatchments['S'].infiltration.parameters.maximum_volume
            if cap:
                self.assertLessEqual(soil['Fe'],cap/(scale*12)+1e-15)
        if case=='zero-rates':
            self.assertEqual(max(infiltration),0.)
            self.assertEqual(soil['Fe'],0.)
            self.assertGreater(max(series['runoff']),0.)
        else:
            self.assertGreater(max(infiltration),0.)
        if case in ('finite-cap','unlimited','zero-decay','zero-decay-unlimited'):
            # Two 10-minute, 1 in/h pulses fit below the 0.5-inch excess cap.
            self.assertAlmostEqual(sum(infiltration)/3600,1./3.,delta=1e-7)
            self.assertEqual(max(series['runoff']),0.)
            self.assertGreater(max(infiltration[2400:3000]),.99)
            self.assertGreater(soil['Fe'],0.)
            # fmin=0.2 in/h: without drying, excess would be 0.8/3 inches.
            # The smaller final state establishes dry recovery in this fixture.
            self.assertLess(soil['Fe'],(.8/3.)/12)
        if case in ('small-cap','zero-decay-small-cap'):
            # At 1 in/h with fmin=0.2 in/h, a 0.01-inch excess capacity is
            # reached after 45 seconds, rounded UP to the next 10-second wet
            # step. Existing flux semantics apply the cap on the following
            # step, so exactly 50 seconds infiltrate. This is not a claim of
            # substep physical accuracy or a reimplementation of the solver.
            wet_seconds=math.ceil(.01/(1.-.2)*3600/10)*10
            self.assertAlmostEqual(sum(infiltration),wet_seconds,delta=1e-4)
            self.assertAlmostEqual(soil['Fe'],.01/12,delta=1e-15)
            self.assertEqual(max(infiltration[600:]),0.)
            self.assertGreater(state.ponded_depths[2],0.)
            self.assertGreater(max(series['runoff']),0.)
        if case in ('constant-rate','ordinary-constant'):
            self.assertAlmostEqual(max(infiltration),.2,delta=1e-7)
            self.assertTrue(all(abs(v-.2)<1e-7 for v in infiltration[400:800]))
            self.assertEqual(soil['Fe'],0.)
        if case in ('zero-decay','zero-decay-unlimited','ordinary-zero-decay'):
            self.assertTrue(all(abs(v-1.)<1e-7 for v in infiltration[400:800]))
        return rain,infiltration

    def check_matrix(self, factory, units, cases, evidence):
        reference={}
        for family in ('standard','custom'):
            backend,info=self.current_backend(family)
            for unit in units:
                for case in cases:
                    with self.subTest(family=family,units=unit,case=case),tempfile.TemporaryDirectory() as temporary:
                        root=Path(temporary);value=factory(unit,case)
                        bind(value,'HOTSTART','SAVE',root/'final.hsf')
                        first=self.run_model(value,root/'first',backend)
                        repeated=factory(unit,case)
                        bind(repeated,'HOTSTART','SAVE',root/'repeat.hsf')
                        again=self.run_model(repeated,root/'again',backend,batch=19)
                        # Batch sizes change process orchestration, never hydrology.
                        self.assertEqual(first.output.read_bytes(),again.output.read_bytes())
                        self.assertEqual(first.mass_balance.raw_percentages,again.mass_balance.raw_percentages)
                        self.assertEqual(first.produced_caches[0].artifact.read_bytes(),
                                         again.produced_caches[0].artifact.read_bytes())
                        series=self.hydrology(first,value,unit,case)
                        if case not in reference:
                            reference[case]=series
                        else:
                            # Fixtures have identical physical rain/soil. Only
                            # depth units and downstream routing differ.
                            for expected,actual in zip(reference[case],series):
                                self.assertLess(max(abs(a-b) for a,b in zip(expected,actual)),1e-6)
                        evidence.append(dict(kind='current-whole-model',family=family,units=unit,case=case,
                            backend_sha256=info.sha256,output_sha256=first.output.sha256,
                            different_batches_exact=True,physical_budget_checked=True))

    def rejected_before_open(self, result, phases, blocked, destination, code):
        self.assertEqual(result.status,'rejected')
        self.assertIn(code,{d.code for d in result.diagnostics.errors})
        self.assertNotIn('opening',phases)
        blocked.assert_not_called()
        for filename in ('model.inp','model.rpt','model.out'):
            self.assertEqual((destination/filename).read_bytes(),b'previous-success')

    def rejection_target(self, root, name):
        destination=root/name;destination.mkdir()
        for filename in ('model.inp','model.rpt','model.out'):
            (destination/filename).write_bytes(b'previous-success')
        return destination

    def check_continuation(self, factory, units, case, evidence):
        for family in ('standard','custom'):
            backend,info=self.current_backend(family)
            for unit in units:
                with self.subTest(family=family,units=unit),tempfile.TemporaryDirectory() as temporary:
                    root=Path(temporary);base=factory(unit,case);value=base.copy()
                    bind(value,'RUNOFF','SAVE',root/'current.runoff')
                    bind(value,'HOTSTART','SAVE',root/'current.hsf')
                    checkpoints=[]
                    schedule=CheckpointSchedule(directory=FileReference(path=str(root/'saved'),direction='output'),
                        interval=timedelta(seconds=600),on_saved=checkpoints.append)
                    expected=self.run_model(value,root/'current',backend,checkpoints=schedule)
                    self.assertTrue(checkpoints)
                    saved=checkpoints[0]
                    self.assertGreaterEqual(saved.simulation_seconds,600.)
                    self.assertLess(saved.simulation_seconds,637.)
                    expected.save(root/'expected')
                    command=('import sys; sys.path[:0]='+repr(sys.path)+'; '
                             'from test_native_horton_integration_v2 import resume_child; '
                             'resume_child(*sys.argv[1:])')
                    completed=subprocess.run([sys.executable,'-B','-c',command,str(saved.directory),
                        str(root/'resumed'),family,str(Path(easysewer.__file__).resolve())],capture_output=True,text=True,timeout=120)
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
                    # These intentionally synthetic probe values exercise each
                    # identity boundary separately. No historical solver ran.
                    other_sha=hashlib.sha256(b'test-only incompatible Horton engine').hexdigest()
                    self.assertNotEqual(other_sha,info.sha256)
                    semantics=tuple((key,'test-only:incompatible-horton' if key=='swmm:modified-horton' else val)
                                    for key,val in info.output_semantics)
                    variants={'bytes':replace(info,sha256=other_sha),
                              'policy':replace(info,numerical_policy='test-only:incompatible-horton'),
                              'semantics':replace(info,output_semantics=semantics),
                              'capability':replace(info,capabilities=tuple(c for c in info.capabilities
                                                                          if c!='easysewer:horton-state:1'))}
                    for label,incompatible in variants.items():
                        with self.subTest(boundary=label):
                            rejected=self.rejection_target(root,'reject-'+label);phases=[]
                            with patch.object(type(backend),'probe',return_value=incompatible), \
                                 patch.object(type(backend),'session',side_effect=AssertionError('must reject before session')) as blocked:
                                result=engine(backend).resume(saved,ResumeConfig(
                                    output_directory=FileReference(path=str(rejected),direction='output'),overwrite=True),
                                    progress=lambda event:phases.append(event.phase))
                            self.rejected_before_open(result,phases,blocked,rejected,'run.checkpoint_backend')
                    produced=next(item for item in expected.produced_caches if item.kind=='RUNOFF')
                    consumer=base.copy();bind(consumer,'RUNOFF','USE',produced.artifact.path)
                    rejected=self.rejection_target(root,'reject-cache');phases=[]
                    with patch.object(type(backend),'probe',return_value=variants['bytes']), \
                         patch.object(type(backend),'session',side_effect=AssertionError('must reject before session')) as blocked:
                        result=engine(backend).run(consumer,config(rejected,backend=backend.key,overwrite=True,
                            cache_reuse=(('RUNOFF','require_match'),)),producers={'RUNOFF':produced},
                            progress=lambda event:phases.append(event.phase))
                    self.rejected_before_open(result,phases,blocked,rejected,'run.cache_engine')
                    matched=self.success(engine(backend).run(consumer,config(root/'consume-current',backend=backend.key,
                        cache_reuse=(('RUNOFF','require_match'),)),producers={'RUNOFF':produced}))
                    self.assertEqual(matched.consumed_caches[0].reuse.status,'matched')
                    evidence.append(dict(kind='current-continuation',family=family,units=unit,
                        split_seconds=saved.simulation_seconds,fresh_parent=True,full_output_exact=True,cache_bytes_exact=True,
                        synthetic_checkpoint_identity_rejected_before_session=tuple(variants),
                        synthetic_runoff_identity_rejected_before_session=True,matching_runoff_reused=True))


class HortonIntegrationTests(HortonAssertions,unittest.TestCase):
    def test_nine_hydrology_cases_both_families_and_units(self):
        self.check_matrix(model,('CFS','CMS'),
            ('horton','green-ampt','modified-green-ampt','curve-number','unlimited',
             'constant-rate','zero-decay','finite-cap','small-cap'),EVIDENCE)

    def test_finite_capacity_fresh_parent_resume_and_revision_boundaries(self):
        self.check_continuation(model,('CFS','CMS'),'finite-cap',EVIDENCE)


if __name__=='__main__':
    unittest.main()
