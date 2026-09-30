"""Analytical water/quality balances and native post-adjustment statistics."""

from datetime import date, time, timedelta
import hashlib
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest

from easysewer import get_native_capabilities
from easysewer.io.inp import InpDocument
from easysewer.model import Model, Ref
from easysewer.runtime import FlexiblePondingBackend, FlexiblePondingPolicy, Runner
from test_flexible_v2 import SOURCE, configuration, ponding_model
from test_runner_v2 import config as standard_configuration


def sealed_model(units='CFS', *, pollutants=True, with_pump=False):
    source=SOURCE.replace('J FLOW Pulse FLOW 1 1','J FLOW "" FLOW 1 1 10')
    source=source.replace('[CONDUITS]\nP J O 100 .013 0 0\n[XSECTIONS]\nP CIRCULAR .2 0 0 0 1\n','')
    source=source.replace('J 0 1 0 0 100','J 0 0 0 0 100')
    source=source[:source.index('[TIMESERIES]')]+source[source.index('[REPORT]'):]
    if with_pump:source+='\n[PUMPS]\nClosed J O * OFF 0 0\n'
    if pollutants:
        source+='''
[POLLUTANTS]
Count #/L 0 0 0 0 NO * 0 0 7
Micro UG/L 0 0 0 0 NO * 0 0 5000
Mass MG/L 0 0 0 0 NO * 0 0 5
[INFLOWS]
J Count "" CONCEN 1 1 7
J Micro "" CONCEN 1 1 5000
J Mass "" CONCEN 1 1 5
'''
    model=Model.from_document(InpDocument.from_text(source),strict=True)
    if units!='CFS':model.convert_units(units)
    return model


def output_values(result, collection, name, variable):
    metadata=result.output_metadata;raw=result.output.read_bytes();offset=0
    codes=dict(metadata.variable_codes)
    for namespace in ('subcatchments','nodes','links','system'):
        key='swmm:'+namespace
        if key==collection:
            index=0 if namespace=='system' else metadata.index(Ref(collection=key,key=name))
            offset+=4*(index*len(codes[key])+codes[key].index(variable))
            break
        offset+=4*len(metadata.names(key))*len(codes[key])
    return tuple(struct.unpack_from('<f',raw,metadata.output_offset+p*metadata.period_bytes+8+offset)[0]
                 for p in range(metadata.periods))


ABI_GUARD=r'''
import ctypes as c,json,sys
library,input,report,output,mode=sys.argv[1:]
lib=c.CDLL(library)
for name,args,result in [('open',[c.c_char_p]*3,c.c_int),('start',[c.c_int],c.c_int),('execRouting',[],c.c_int),('saveResults',[],c.c_int),('end',[],c.c_int),('close',[],c.c_int),('getValue',[c.c_int,c.c_int],c.c_double),('setValue',[c.c_int,c.c_int,c.c_double],None),('getIndex',[c.c_int,c.c_char_p],c.c_int)]:
 f=getattr(lib,'swmm_'+name);f.argtypes=args;f.restype=result
assert lib.swmm_open(input.encode(),report.encode(),output.encode())==0
assert lib.swmm_start(1)==0
assert lib.swmm_execRouting()==0
if mode=='repeat-route':code=lib.swmm_execRouting()
elif mode=='missing-volume':
 j=lib.swmm_getIndex(2,b'J');lib.swmm_setValue(311,j,1.)
 code=lib.swmm_saveResults()
elif mode=='repeat-finalize':
 assert lib.swmm_saveResults()==0
 code=lib.swmm_saveResults()
else:code=lib.swmm_end()
assert code==508,(mode,code)
lib.swmm_end();assert lib.swmm_close()==0
print(json.dumps({'mode':mode,'error':code}))
'''


@unittest.skipUnless(get_native_capabilities()['flexible_ponding'],'Custom native solver unavailable')
class NativePondingAccountingTests(unittest.TestCase):
    def run_model(self, model, path, *, ratio=.5, **options):
        policy=FlexiblePondingPolicy(external_flooding_ratio=ratio,depth_threshold_m=0,flow_threshold_cms=0)
        result=Runner().run(model,configuration(path,policy,**options))
        self.assertTrue(result.succeeded,repr(result.failure)+' '+repr(result.diagnostics.errors))
        self.assertTrue(result.backend.abi.endswith(':flexible:201'))
        self.assertEqual(result.backend_results.data['accounting'],'discrete-volume:1')
        return result

    def trace(self, result):
        rows=[json.loads(line) for line in result.artifact('easysewer:flexible-ponding-steps').read_bytes().decode().splitlines()]
        self.assertEqual(rows[-1]['kind'],'completed')
        return rows[1:-1]

    def conserved(self, result, ratio, *, duration=60):
        rows=self.trace(result)
        # A sealed node has no active outflow. SWMM integrates a zero initial
        # inflow and then a constant 10 cfs using the ordinary trapezoidal rule.
        incoming=10*(duration-rows[0]['dt_seconds']/2)
        vf=1 if result.output_metadata.flow_units in ('CFS','GPM','MGD') else .02832
        data=result.backend_results.data
        self.assertAlmostEqual(data['removed_volume']/vf,ratio*incoming,places=6)
        self.assertAlmostEqual(rows[-1]['after']['volume']/vf,(1-ratio)*incoming,places=6)
        self.assertAlmostEqual((data['removed_volume']+rows[-1]['after']['volume'])/vf,incoming,places=6)
        self.assertEqual(result.mass_balance.applicability('flow').status,'computed')
        self.assertLess(abs(result.mass_balance.flow_percent),1e-4)
        if data['quality_enabled']:self.assertLess(abs(result.mass_balance.quality_percent),1e-4)
        stats=next(row for row in data['native_statistics'] if row['id']=='J')
        self.assertAlmostEqual(stats['flooding_volume'],data['removed_volume'],places=7)
        self.assertAlmostEqual(stats['maximum_depth'],max(row['after']['depth'] for row in rows),places=8)
        concentrations={'Mass':5,'Micro':5000,'Count':7}
        if data['quality_enabled']:
            for pollutant in data['pollutants']:
                expected=ratio*incoming*28.317*concentrations[pollutant['id']]
                self.assertAlmostEqual(pollutant['removed_quantity']/max(1,expected),expected/max(1,expected),places=8)
                self.assertAlmostEqual(pollutant['native_removed_quantity']/max(1,expected),expected/max(1,expected),places=8)
        return rows

    def test_sealed_node_six_units_and_three_pollutant_units_conserve(self):
        with tempfile.TemporaryDirectory() as directory:
            for units in ('CFS','GPM','MGD','CMS','LPS','MLD'):
                with self.subTest(units=units):
                    result=self.run_model(sealed_model(units),Path(directory)/units)
                    self.conserved(result,.5)
                    self.assertEqual(result.engine_objects.names('swmm:pollutants'),('Count','Micro','Mass'))
                    self.assertEqual(result.output_metadata.pollutant_units,('#/L','UG/L','MG/L'))
                    self.assertTrue(all(value>0 for value in output_values(result,'swmm:nodes','J',5)))

    def test_irregular_steps_last_interval_report_modes_and_ignored_quality(self):
        with tempfile.TemporaryDirectory() as directory:
            for mode in ('normal','averages','disabled','ignore-quality','skip-steady'):
                with self.subTest(mode=mode):
                    model=sealed_model(with_pump=True)
                    model.update_options(end_time=time(0,1,2),routing_step=timedelta(seconds=7),
                        rule_step=timedelta(seconds=20),report_step=timedelta(seconds=10))
                    self.assertEqual(model.effective_options.duration,timedelta(seconds=62))
                    if mode=='averages':model.update_report(averages=True)
                    if mode=='disabled':model.update_report(disabled=True)
                    if mode=='ignore-quality':model.update_options(ignore_quality=True)
                    if mode=='skip-steady':model.update_options(skip_steady_state=True)
                    result=self.run_model(model,Path(directory)/mode,ratio=1 if mode=='skip-steady' else .37)
                    rows=self.conserved(result,1 if mode=='skip-steady' else .37,duration=62)
                    self.assertEqual(rows[-1]['dt_seconds'],2)
                    self.assertEqual({row['dt_seconds'] for row in rows},{2.,6.,7.})
                    if mode=='ignore-quality':
                        self.assertFalse(result.backend_results.data['quality_enabled'])
                        self.assertTrue(all(row['quality'] is None for row in rows))
                        self.assertTrue(all(row['removed_quantity'] is None for row in result.backend_results.data['pollutants']))
                    if mode=='disabled':self.assertNotIn(b'Flow Routing Continuity',result.report.read_bytes())

    def test_small_removal_is_not_clipped_and_zero_ratio_is_inert(self):
        with tempfile.TemporaryDirectory() as directory:
            for ratio in (0,1e-8):
                result=self.run_model(sealed_model(),Path(directory)/str(ratio),ratio=ratio)
                self.conserved(result,ratio)
                flows=output_values(result,'swmm:nodes','J',5)
                if ratio:
                    self.assertTrue(all(0<value<1e-4 for value in flows))
                    self.assertGreater(result.backend_results.data['pollutants'][0]['removed_quantity'],0)
                else:self.assertEqual(set(flows),{0.})

    def test_adaptive_steps_and_late_report_statistics_have_explicit_scopes(self):
        with tempfile.TemporaryDirectory() as directory:
            histories=[]
            for averages in (False,True):
                model=ponding_model()
                model.update_options(end_time=time(0,1,2),routing_step=timedelta(seconds=7),
                    report_step=timedelta(seconds=10),variable_step=.75,minimum_step=timedelta(seconds=.1),
                    report_start_date=date(2020,1,1),report_start_time=time(0,0,30))
                model.update_report(averages=averages)
                result=self.run_model(model,Path(directory)/str(averages))
                rows=self.trace(result);histories.append(rows)
                self.assertAlmostEqual(sum(row['dt_seconds'] for row in rows),62,places=9)
                self.assertTrue(any(0<row['dt_seconds']<1 for row in rows))
                reported=[row for row in rows if row['time_seconds']>=30]
                data=result.backend_results.data
                stats=next(row for row in data['native_statistics'] if row['id']=='J')
                self.assertAlmostEqual(stats['removed_volume'],sum(row['removed_volume'] for row in rows),places=8)
                self.assertAlmostEqual(stats['flooding_volume'],sum(row['removed_volume'] for row in reported),places=8)
                self.assertGreater(stats['removed_volume'],stats['flooding_volume'])
                self.assertAlmostEqual(stats['routing_step_mean_depth'],sum(row['after']['depth']+1 for row in reported)/len(reported),places=8)
                peak=max(reported,key=lambda row:row['after']['depth'])
                self.assertAlmostEqual(stats['maximum_depth'],peak['after']['depth']+1,places=8)
                self.assertAlmostEqual((stats['maximum_depth_date']-43831)*86400,peak['time_seconds'],delta=.002)
                self.assertEqual(data['statistics_metadata']['depth_unit'],'ft')
                self.assertEqual(data['statistics_metadata']['removed_volume_scope'],'entire simulation')
                self.assertLess(abs(result.mass_balance.flow_percent),1e-4)
            self.assertEqual(histories[0],histories[1])

    def test_no_binary_output_still_finalizes_water_quality_and_statistics(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);model=sealed_model();model.to_inp(root/'model.inp')
            policy=FlexiblePondingPolicy(external_flooding_ratio=.5,depth_threshold_m=0,
                flow_threshold_cms=0,record_steps=False)
            backend=FlexiblePondingBackend()
            plan=backend.prepare_run(model,configuration(root,policy),
                SimpleNamespace(input_sha256=hashlib.sha256((root/'model.inp').read_bytes()).hexdigest()),
                artifact_directory=Path('assets'))
            outcomes=[]
            for save in (False,True):
                with backend.session(working_directory=root) as session:
                    session.open('model.inp',str(save)+'.rpt',str(save)+'.out')
                    session.configure_execution(plan.parameters.data)
                    session.start(save_results=save)
                    while not session.step(max_steps=100).finished:pass
                    mass=session.end();outcomes.append(session.execution_results())
                    self.assertLess(abs(mass.flow_percent),1e-4)
                    self.assertLess(abs(mass.quality_percent),1e-4)
                self.assertEqual(session.returncode,0)
            self.assertEqual(outcomes[0],outcomes[1])
            self.assertAlmostEqual(outcomes[0]['removed_volume'],297.5,places=8)
            self.assertGreater((root/'True.out').stat().st_size,(root/'False.out').stat().st_size)

    def test_regular_overflow_uses_standard_totals_instead_of_doubling(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            model=Model.from_document(InpDocument.from_text(SOURCE.replace('J 0 1 0 0 100','J 0 1 0 0 0')),strict=True)
            standard=Runner().run(model,standard_configuration(root/'standard'))
            self.assertTrue(standard.succeeded,standard.failure)
            custom=self.run_model(model,root/'custom')
            self.assertEqual(custom.backend_results.data['removed_volume'],0)
            self.assertAlmostEqual(custom.mass_balance.flow_percent,standard.mass_balance.flow_percent,places=4)
            regular=output_values(standard,'swmm:nodes','J',5)
            actual=output_values(custom,'swmm:nodes','J',5)
            self.assertGreater(max(regular),0)
            for before,after in zip(regular,actual):self.assertAlmostEqual(before,after,places=5)

    def test_peak_statistics_use_adjusted_state_and_exact_removal_volume(self):
        with tempfile.TemporaryDirectory() as directory:
            result=self.run_model(ponding_model(),Path(directory)/'run')
            rows=self.trace(result)
            stats=next(row for row in result.backend_results.data['native_statistics'] if row['id']=='J')
            maximum=max(row['after']['depth']+1 for row in rows)
            self.assertAlmostEqual(stats['maximum_depth'],maximum,places=8)
            self.assertGreater(max(row['before']['depth']+1 for row in rows),maximum)
            self.assertAlmostEqual(stats['maximum_flooding_flow'],max(row['external_flow'] for row in rows),places=8)
            self.assertAlmostEqual(stats['flooding_volume'],sum(row['removed_volume'] for row in rows),places=8)
            self.assertEqual(dict(result.output_metadata.semantics)['swmm:statistics'],'native-after-ponding-adjustment')

    def test_native_commit_rejects_repeated_or_inconsistent_steps(self):
        from easysewer.runtime import FlexiblePondingBackend
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);script=root/'guard.py';script.write_text(ABI_GUARD,encoding='utf-8')
            model=sealed_model(pollutants=False);model.to_inp(root/'model.inp')
            library=FlexiblePondingBackend().probe().library
            for mode in ('repeat-route','repeat-finalize','missing-volume','unfinished-end'):
                with self.subTest(mode=mode):
                    completed=subprocess.run([sys.executable,'-I','-B',str(script),library,str(root/'model.inp'),
                        str(root/(mode+'.rpt')),str(root/(mode+'.out')),mode],cwd=root,capture_output=True,text=True,
                        timeout=30,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                    self.assertEqual(completed.returncode,0,completed.stdout+completed.stderr)
                    self.assertEqual(json.loads(completed.stdout)['error'],508)


if __name__=='__main__':unittest.main()
