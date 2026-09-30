"""Process sessions compared with independent literal inputs and direct C runs."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
import re
import tempfile
import unittest

from easysewer import get_native_capabilities
from easysewer.model import FileReference, Ref
from easysewer.model import climate as c
from easysewer.runtime import SessionError, StandardBackend
from test_climate_v2 import climate_model
from test_native_v2_climate import user_weather
import test_native_v2_project as native_project
from test_options_v2 import network


@unittest.skipUnless(get_native_capabilities()['swmm_solver'],'Native solver unavailable')
class NativeBackendTests(unittest.TestCase):
    def run_session(self, root, name, source, *, batch=100, report=True, save=True):
        root=Path(root);(root/(name+'.inp')).write_text(source,encoding='utf-8')
        with StandardBackend().session(working_directory=root) as session:
            session.open(name+'.inp',name+'.rpt',name+'.out')
            session.start(save_results=save)
            for _ in range(20000):
                result=session.step(max_steps=batch)
                if result.finished:break
            else:self.fail('Simulation did not finish')
            balance=session.end()
            if report:session.report()
        self.assertEqual(session.returncode,0)
        self.assertEqual(session.cleanup_errors,())
        return session,balance

    def test_actual_probe_fingerprint_abi_and_corrupt_library_are_distinct(self):
        backend=StandardBackend();info=backend.probe()
        self.assertTrue(info.available,info.reason)
        self.assertEqual(info.engine_version,52004)
        self.assertEqual(info.origin,'packaged-bytes')
        self.assertEqual(info.isolation,'process')
        self.assertEqual(len(info.sha256),64)
        self.assertEqual(info.numerical_policy,'easysewer:standard:5.2.4:16')
        self.assertIn('easysewer:standard-fixes:16',info.capabilities)
        self.assertIn('easysewer:path-io:1',info.capabilities)
        self.assertIn('easysewer:checkpoint:2',info.capabilities)
        self.assertIn('easysewer:climate-io:1',info.capabilities)
        self.assertIn('easysewer:timeseries-io:1',info.capabilities)
        self.assertIn('easysewer:report-io:1',info.capabilities)
        self.assertIn('easysewer:lid-report-io:1',info.capabilities)
        self.assertIn('easysewer:rdii-io:1',info.capabilities)
        self.assertIn('easysewer:routing-io:1',info.capabilities)
        self.assertIn('easysewer:solver-output-io:1',info.capabilities)
        self.assertIn('easysewer:runoff-physics:1',info.capabilities)
        self.assertIn('easysewer:runoff-rain-clock:1',info.capabilities)
        self.assertEqual(dict(info.output_semantics)['swmm:runoff-rain-clock'],'easysewer:runoff-rain-clock:1')
        self.assertEqual(dict(info.output_semantics)['swmm:runoff-replay'],'easysewer:runoff-physics:1')
        self.assertEqual(dict(info.output_semantics)['swmm:nws-rainfall-arithmetic'],'reference')
        unproven=backend.execution_info(replace(info,sha256='a'*64))
        self.assertEqual(unproven.numerical_policy,'user-library:unverified')
        self.assertNotIn('easysewer:standard-fixes:16',unproven.capabilities)
        self.assertNotIn('easysewer:path-io:1',unproven.capabilities)
        self.assertNotIn('easysewer:checkpoint:2',unproven.capabilities)
        self.assertNotIn('easysewer:climate-io:1',unproven.capabilities)
        self.assertNotIn('easysewer:timeseries-io:1',unproven.capabilities)
        self.assertNotIn('easysewer:report-io:1',unproven.capabilities)
        self.assertNotIn('easysewer:lid-report-io:1',unproven.capabilities)
        self.assertNotIn('easysewer:rdii-io:1',unproven.capabilities)
        self.assertNotIn('easysewer:routing-io:1',unproven.capabilities)
        self.assertNotIn('easysewer:solver-output-io:1',unproven.capabilities)
        self.assertNotIn('easysewer:runoff-rain-clock:1',unproven.capabilities)
        self.assertNotIn('swmm:runoff-rain-clock',dict(unproven.output_semantics))
        wrong=StandardBackend(expected_sha256='0'*64).probe()
        self.assertFalse(wrong.available)
        self.assertIn('SHA-256',wrong.reason)
        with tempfile.TemporaryDirectory() as directory:
            broken=Path(directory)/'broken.dll';broken.write_bytes(b'not a library')
            result=StandardBackend(library=str(broken)).probe()
            self.assertFalse(result.available)
            self.assertIn('load:',result.reason)

    @unittest.skipUnless(get_native_capabilities()['swmm_output'],'OUT oracle unavailable')
    def test_batched_and_single_steps_match_direct_native_out_and_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);model=network();model.update_options(report_step=timedelta(seconds=30))
            source=model.to_document().text+'[REPORT]\nNODES J\nLINKS P\n'
            oracle=native_project.NativeProjectTests().solve(directory,'oracle',source)
            for batch in (1,17,10000):
                with self.subTest(batch=batch):
                    session,balance=self.run_session(root,'worker'+str(batch),source,batch=batch)
                    self.assertEqual((root/('worker'+str(batch)+'.out')).read_bytes(),(root/'oracle.out').read_bytes())
                    report=(root/('worker'+str(batch)+'.rpt')).read_text(encoding='utf-8')
                    normalized='\n'.join(line for line in report.splitlines() if not line.strip().startswith(('Analysis begun on:','Analysis ended on:','Total elapsed time:')))
                    self.assertEqual(normalized,oracle['report'])
                    # Engine sees both nodes; selected OUT has only J.
                    self.assertEqual(session.objects.names('swmm:nodes'),('J','O'))
                    self.assertEqual(oracle['ids'][1],('J',))
                    self.assertEqual(session.objects.index(Ref(collection='swmm:nodes',key='o')),1)
                    expected=float(re.search(r'Continuity Error \(%\)\s+\.*\s+([-+]?\d+(?:\.\d+)?)',oracle['report'])[1])
                    self.assertAlmostEqual(balance.flow_percent,expected,delta=.001)

    def test_concurrent_projects_keep_global_state_cwd_and_relative_files_separate(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);cwd=Path.cwd()
            def run(index):
                folder=root/str(index);folder.mkdir()
                model=network();model.nodes.rename('J','J'+str(index));model.reinterpret_units('CFS' if index%2 else 'CMS')
                session,_=self.run_session(folder,'model',model.to_document().text)
                return session.objects.names('swmm:nodes'),session.flow_units,session.pid
            with ThreadPoolExecutor(max_workers=4) as executor:results=list(executor.map(run,range(4)))
            self.assertEqual(Path.cwd(),cwd)
            self.assertEqual(len({item[2] for item in results}),4)
            for i,(names,units,_) in enumerate(results):
                self.assertEqual(names,('J'+str(i),'O'))
                self.assertEqual(units,0 if i%2 else 3)

    def test_normal_climate_and_failed_climate_open_release_leaked_file_handles(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);weather=root/'weather.dat';user_weather(weather)
            model=climate_model()
            model.update_climate(file=c.ClimateFile(file=FileReference(path='weather.dat'),units='F'),
                wind=c.FileWind(),evaporation=c.Evaporation(source=c.FileEvaporation()))
            self.run_session(root,'climate',model.to_document().text,batch=2000)
            # Windows denies removal if any leaked FILE* remains in a process.
            weather.unlink();user_weather(weather)
            model.update_climate(file=replace(model.climate.file,start_date=date(2019,11,1)))
            (root/'missing_month.inp').write_text(model.to_document().text,encoding='utf-8')
            with StandardBackend().session(working_directory=root) as session:
                with self.assertRaises(SessionError) as caught:
                    session.open('missing_month.inp','missing_month.rpt','missing_month.out')
                self.assertEqual(caught.exception.failure.stage,'open')
                self.assertEqual(caught.exception.failure.code,339)
                self.assertIn('climate',(root/'missing_month.rpt').read_text(encoding='utf-8').lower())
                self.assertIsNotNone(session.returncode)
            weather.unlink()
            # A complete zero-record month at EOF is valid with or without LF.
            # Both runs must close their stream and produce identical results.
            model.update_climate(file=replace(model.climate.file,start_date=date(2020,1,30)))
            outputs=[]
            for name,ending in (('unterminated_month',b''),('terminated_month',b'\n')):
                weather.write_bytes(b'DLY12345600TMAX  2020019999000'+ending)
                self.run_session(root,name,model.to_document().text,batch=2000)
                outputs.append((root/(name+'.out')).read_bytes())
                weather.unlink()
            self.assertEqual(outputs[0],outputs[1])
            self.assertFalse(weather.exists())

    def test_partial_native_start_failure_attempts_end_then_close(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            source=network().to_document().text+'\n[FILES]\nUSE HOTSTART missing.hsf\n'
            (root/'model.inp').write_text(source,encoding='utf-8')
            with StandardBackend().session(working_directory=root) as session:
                session.open('model.inp','model.rpt','model.out')
                with self.assertRaises(SessionError) as caught:session.start()
                self.assertEqual(caught.exception.failure.stage,'start')
                self.assertEqual(caught.exception.failure.code,331)
                self.assertEqual(tuple((error.stage,error.code) for error in caught.exception.cleanup),(('end',331),))
                self.assertIsNotNone(session.returncode)
            (root/'model.rpt').unlink();(root/'model.out').unlink()

    def test_failed_open_then_new_session_and_exception_during_running_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'bad.inp').write_text('[OPTIONS]\nFLOW_UNITS BAD\n',encoding='ascii')
            with StandardBackend().session(working_directory=root) as session:
                with self.assertRaises(SessionError) as caught:session.open('bad.inp','bad.rpt','bad.out')
                self.assertEqual(caught.exception.failure.stage,'open')
                self.assertIsNotNone(session.returncode)
            (root/'bad.rpt').unlink()
            source=network().to_document().text;(root/'ok.inp').write_text(source,encoding='utf-8')
            with self.assertRaisesRegex(RuntimeError,'callback failed'):
                with StandardBackend().session(working_directory=root) as session:
                    session.open('ok.inp','ok.rpt','ok.out');session.start();session.step()
                    raise RuntimeError('callback failed')
            self.assertEqual(session.returncode,0)
            self.assertFalse(session.cleanup_errors)
            (root/'ok.out').unlink()
            self.run_session(root,'again',source)

    def test_report_is_explicit_and_no_save_still_completes(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            source=network().to_document().text+'[REPORT]\nNODES ALL\nLINKS ALL\n'
            self.run_session(root,'yes',source)
            self.run_session(root,'no_report',source,report=False)
            self.assertIn('<<< Node J >>>',(root/'yes.rpt').read_text())
            self.assertNotIn('<<< Node J >>>',(root/'no_report.rpt').read_text())
            self.run_session(root,'no_save',source,save=False,report=False)
            self.assertGreater((root/'yes.out').stat().st_size,(root/'no_save.out').stat().st_size)


if __name__=='__main__':unittest.main()
