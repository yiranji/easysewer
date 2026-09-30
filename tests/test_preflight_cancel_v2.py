"""Preflight checkpoints retain exception identity and isolate work scopes."""

from dataclasses import replace
from datetime import timedelta
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from easysewer.io import timeseries
from easysewer.io.interface_inspection import InterfaceInspection
from easysewer.model import FileReference
from easysewer.model.resources import FileTimeSeries
from easysewer.runtime import check_files, Runner
from easysewer.runtime import runner as runner_module
from easysewer.validation._cooperative import checkpoint_scope, checkpointed
from test_options_v2 import network
from test_runner_v2 import config
import test_backend_v2 as protocol_fixture


def fixture(root, raw=None):
    path=root/'series.dat'
    path.write_bytes(raw if raw is not None else b''.join(f'{i} 0\n'.encode() for i in range(2000)))
    model=network();model.timeseries.add(FileTimeSeries(id='T',file=FileReference(path=str(path))))
    return model,path


class PreflightCheckpointTests(unittest.TestCase):
    def test_checkpoint_preserves_explicit_and_implicit_exception_chains(self):
        for explicit in (False,True):
            with self.subTest(explicit=explicit):
                errors=[]
                try:
                    try:raise LookupError('cause')
                    except LookupError as cause:
                        if explicit:raise ValueError('checkpoint') from cause
                        raise ValueError('checkpoint')
                except ValueError as error:errors.append(error)
                failure=errors[0]
                expected=(failure.__context__,failure.__cause__,failure.__suppress_context__)
                def checkpoint():raise failure
                with self.assertRaises(ValueError) as caught:
                    with checkpoint_scope(checkpoint):pass
                self.assertIs(caught.exception,failure)
                self.assertEqual((failure.__context__,failure.__cause__,failure.__suppress_context__),expected)

    def test_read_loop_interruptions_preserve_original_exception_identity(self):
        for kind in (ValueError,OSError,RuntimeError,KeyboardInterrupt):
            with self.subTest(kind=kind),tempfile.TemporaryDirectory() as directory:
                model,path=fixture(Path(directory),b';'+b'x'*100000)
                error=kind('checkpoint failure');calls=[]
                def checkpoint():
                    calls.append(1)
                    if len(calls)==5:raise error
                with self.assertRaises(kind) as caught:check_files(model,checkpoint=checkpoint)
                self.assertIs(caught.exception,error)
                self.assertEqual(path.stat().st_size,100001)

    def test_parser_and_invalid_sequence_fallback_do_not_translate_checkpoints(self):
        for phase in ('parse','fallback'):
            with self.subTest(phase=phase),tempfile.TemporaryDirectory() as directory:
                raw=(b'0 0\n0 0\n' if phase=='fallback' else b'')+b''.join(f'{i+1} 0\n'.encode() for i in range(2000))
                model,_=fixture(Path(directory),raw);active=[];seen=[];failure=ValueError('stop '+phase)
                original=timeseries._parse_points;post=timeseries.TimeSeriesData.__post_init__
                def points(*a,**k):
                    for point in original(*a,**k):
                        seen.append(1)
                        if phase=='parse' and len(seen)==50:active.append(True)
                        yield point
                def validate(document):
                    if phase=='fallback':active.append(True)
                    return post(document)
                def checkpoint():
                    if active:raise failure
                with patch.object(timeseries,'_parse_points',points),patch.object(timeseries.TimeSeriesData,'__post_init__',validate):
                    with self.assertRaises(ValueError) as caught:check_files(model,checkpoint=checkpoint)
                self.assertIs(caught.exception,failure)
                if phase=='parse':self.assertLess(len(seen),600)

    def test_custom_inspector_contract_nested_scopes_and_primary_failures(self):
        with tempfile.TemporaryDirectory() as directory:
            model,_=fixture(Path(directory));active=[];error=OSError('nested checkpoint')
            def checkpoint():
                if active:raise error
            def inspect(data,*,use,model,encoding,source):
                active.append(True)
                return check_files(model,inspect_data=False)
            with self.assertRaises(OSError) as caught:
                check_files(model,inspectors={'swmm:timeseries.data':inspect},checkpoint=checkpoint)
            self.assertIs(caught.exception,error)
            self.assertTrue(check_files(model,backend_capabilities=('easysewer:timeseries-io:1',)).complete)
            active.clear();primary=RuntimeError('inspector primary')
            def failing(data,*,use,model,encoding,source):
                active.append(True)
                raise primary
            with self.assertRaises(RuntimeError) as caught:
                check_files(model,inspectors={'swmm:timeseries.data':failing},checkpoint=checkpoint)
            self.assertIs(caught.exception,primary)

    def test_default_results_and_custom_call_signature_are_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            model,_=fixture(Path(directory));calls=[]
            def custom(data,*,use,model,encoding,source):
                calls.append(source)
                return InterfaceInspection(format=use.format,status='validated')
            options=dict(inspectors={'swmm:timeseries.data':custom})
            self.assertEqual(check_files(model,**options),check_files(model,checkpoint=lambda:None,**options))
            self.assertEqual(len(calls),2)
            with self.assertRaises(TypeError):check_files(model,checkpoint=1)

    def test_text_binary_and_derived_readers_preserve_checkpoint_exceptions(self):
        from easysewer.io.rainfall import RainfallData
        from easysewer.io.climate_data import ClimateData
        from easysewer.io.historical_rainfall import HistoricalRainfallData
        from easysewer.io.hotstart import HotstartData
        from easysewer.io.runoff_cache import RunoffData,RdiiData
        from easysewer.io.routing import RoutingInterface
        from test_climate_data_v2 import ghcnd_fixture
        from test_historical_rainfall_v2 import fixture as historic_fixture
        from test_hotstart_v2 import fixture as hotstart_fixture
        from test_runoff_cache_v2 import runoff_fixture,rdii_fixture
        from test_files_v2 import routing
        raw_rain=b'A 2020 1 1 0 0 1\nA 2020 1 1 1 0 2\n'
        climate_raw=ghcnd_fixture();historic_raw=historic_fixture('NWS_TAPE')
        hot_layout,hot_raw=hotstart_fixture();runoff_layout,runoff_raw=runoff_fixture();_,rdii_raw=rdii_fixture()
        routing_raw=routing().to_bytes()
        rain=RainfallData.from_bytes(raw_rain);climate=ClimateData.from_bytes(climate_raw)
        historical=HistoricalRainfallData.from_bytes(historic_raw)
        operations=[lambda:RainfallData.from_bytes(raw_rain),lambda:rain.station_series('A'),
                    lambda:ClimateData.from_bytes(climate_raw),lambda:climate.daily(unit_system='US',ghcnd_units='C10'),
                    lambda:HistoricalRainfallData.from_bytes(historic_raw),historical.interpret,
                    lambda:HotstartData.from_bytes(hot_raw,layout=hot_layout),
                    lambda:RunoffData.from_bytes(runoff_raw,layout=runoff_layout),
                    lambda:RdiiData.from_bytes(rdii_raw),lambda:RoutingInterface.from_bytes(routing_raw)]
        for index,operation in enumerate(operations):
            with self.subTest(reader=index):
                expected=operation();calls=[];error=OSError('reader checkpoint')
                def checkpoint():
                    calls.append(1)
                    if len(calls)==2:raise error
                with self.assertRaises(OSError) as caught:
                    with checkpoint_scope(checkpoint):operation()
                self.assertIs(caught.exception,error)
                self.assertEqual(operation(),expected)

    def test_scopes_are_thread_local_and_restored_after_failure(self):
        entered=threading.Event();release=threading.Event();observed=[]
        failure=ValueError('thread checkpoint')
        def worker():
            try:
                def checkpoint():
                    entered.set();release.wait(5);raise failure
                with checkpoint_scope(checkpoint):pass
            except ValueError as error:observed.append(error)
        thread=threading.Thread(target=worker);thread.start()
        try:
            self.assertTrue(entered.wait(5))
            self.assertEqual(list(checkpointed(range(1000))),list(range(1000)))
        finally:
            release.set();thread.join(5)
        self.assertFalse(thread.is_alive());self.assertEqual(observed,[failure])

    def test_runner_cancel_and_deadline_in_preflight_preserve_outputs(self):
        for status in ('cancelled','timed_out'):
            for keep in (False,True):
                with self.subTest(status=status,keep=keep),protocol_fixture.BackendContractTests().fixture('ok') as (root,backend):
                    model,_=fixture(root);target=root/'published';target.mkdir();(target/'model.out').write_bytes(b'previous')
                    event=threading.Event();controls=[];observed=[]
                    init=runner_module._Cancellation.__init__;points=timeseries._parse_points
                    def capture(control,*a,**k):
                        init(control,*a,**k);controls.append(control)
                    def scan(*a,**k):
                        for point in points(*a,**k):
                            observed.append(1)
                            if len(observed)==50:
                                if status=='cancelled':event.set()
                                else:controls[-1].deadline=time.monotonic()-1
                            yield point
                    with patch.object(runner_module._Cancellation,'__init__',capture),patch.object(timeseries,'_parse_points',scan):
                        result=Runner(backends={'swmm:standard':backend}).run(model,config(target,overwrite=True,keep_failed_artifacts=keep),cancel_event=event)
                    self.assertEqual(result.status,status,result.failure)
                    self.assertEqual(result.failure.stage,'preflight')
                    self.assertFalse(result.native_completed)
                    self.assertEqual((target/'model.out').read_bytes(),b'previous')
                    self.assertFalse(list(target.glob('.easysewer-lock-*')))
                    self.assertEqual(result.retained_directory is not None,keep)
                    self.assertLess(len(observed),600)
                    result.save(root/'saved-result')
                    restored=type(result).load(root/'saved-result')
                    self.assertEqual(len(restored.artifacts),len(result.artifacts))
                    for actual,previous in zip(restored.artifacts,result.artifacts):
                        self.assertEqual(actual.read_bytes(),previous.read_bytes())
                    self.assertEqual(replace(restored,artifacts=tuple(replace(a,path=b.path)
                        for a,b in zip(restored.artifacts,result.artifacts))),result)


if __name__=='__main__':unittest.main()
