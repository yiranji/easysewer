"""Bounded, direct C RAIN tests for both independently built engine families."""

import ctypes
import json
import os
from pathlib import Path
import stat
import struct
import subprocess
import sys
import tempfile
import unittest

from easysewer import get_native_capabilities
from easysewer.utils import probe_library_path
from test_native_v2_standard_io import direct_library, execute, handles
from test_rain_interface_v2 import malformed_rain, rain_bytes, rain_model


def load_rain_library(path, family):
    symbol = 'swmm_getEasySewerStandardFixes' if family == 'standard' else 'swmm_getEasySewerNativeIOFixes'
    lib, path = direct_library(path, revision_symbol=symbol)
    if getattr(lib, symbol)() < 2:
        raise AssertionError('RAIN qualification requires native I/O revision 2 or later')
    return lib


def check_cases(path, family, directory):
    root=Path(directory);lib=load_rain_library(path,family)
    source=rain_model(root/'not-present.dat').to_document().text
    source+=f'[FILES]\nUSE RAINFALL "{root / "rain.ifc"}"\n'
    # Warm runtime state before measuring same-process descriptor ownership.
    (root/'rain.ifc').write_bytes(rain_bytes());execute(lib,root,source,finish=False)
    before=handles();results=[]
    for name,data,expected in malformed_rain():
        (root/'rain.ifc').write_bytes(data)
        codes=execute(lib,root,source,finish=False)
        if codes != (0,expected,expected,0):
            raise AssertionError((name,expected,codes,(root/'model.rpt').read_text()))
        results.append(name)
        (root/'rain.ifc').unlink()
    if handles()!=before:raise AssertionError('RAIN failure leaked native handles')
    print(json.dumps(dict(cases=len(results),handles_released=True,names=results)))


@unittest.skipUnless(get_native_capabilities()['swmm_solver'] and
                    get_native_capabilities()['flexible_ponding'], 'Both native solvers required')
class NativeRainIOTests(unittest.TestCase):
    def libraries(self):
        for family,name,variable in (
            ('standard','swmm5','EASYSEWER_STANDARD_TEST_LIBRARY'),
            ('custom','flexible_ponding','EASYSEWER_CUSTOM_TEST_LIBRARY')):
            path=os.environ.get(variable) or probe_library_path(name)
            yield family,path

    def test_bad_counts_headers_ranges_and_records_return_in_bounded_processes(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for family,path in self.libraries():
                with self.subTest(family=family):
                    child=root/family;child.mkdir()
                    command=[sys.executable,'-B','-c',
                        'import sys; from test_native_v2_rain_io import check_cases; check_cases(*sys.argv[1:])',
                        str(path),family,str(child)]
                    result=subprocess.run(command,capture_output=True,text=True,timeout=20,
                        creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                    self.assertEqual(result.returncode,0,result.stdout+result.stderr)
                    self.assertEqual(json.loads(result.stdout)['cases'],len(malformed_rain()))

    def test_save_use_readonly_and_zero_filled_station_padding_preserve_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);raw=root/'user.dat';cache=root/'rain.ifc'
            for family,path in self.libraries():
                lib=load_rain_library(path,family)
                for units in ('CFS','CMS'):
                    with self.subTest(family=family,units=units):
                        raw.write_text('Station 2020 1 30 0 0 0\nStation 2020 1 30 0 5 .2\nStation 2020 1 30 0 10 .4\n')
                        model=rain_model(raw);model.reinterpret_units(units)
                        source=model.to_document().text
                        self.assertFalse(any(execute(lib,root,source+f'[FILES]\nSAVE RAINFALL "{cache}"\n')))
                        original=(root/'model.out').read_bytes();data=cache.read_bytes()
                        self.assertEqual(data[:14],b'SWMM5-RAIN'+struct.pack('<i',1))
                        self.assertEqual(data[14:1039],b'Station'+b'\0'*1018)
                        self.assertEqual(struct.unpack_from('<iii',data,1039),(300,1051,1087))
                        self.assertEqual(data[1051:],rain_bytes()[1051:])
                        raw.unlink();cache.chmod(stat.S_IREAD|stat.S_IRGRP|stat.S_IROTH)
                        try:
                            self.assertFalse(any(execute(lib,root,source+f'[FILES]\nUSE RAINFALL "{cache}"\n')))
                            self.assertEqual(cache.read_bytes(),data)
                            self.assertEqual((root/'model.out').read_bytes(),original)
                        finally:
                            cache.chmod(stat.S_IREAD|stat.S_IWRITE)

    def test_duplicate_first_assignment_shared_spans_and_adjacent_calendars(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);cache=root/'rain.ifc'
            source=rain_model(root/'not-present.dat').to_document().text+f'[FILES]\nUSE RAINFALL "{cache}"\n'
            separate=rain_bytes([('Station',300,((43860.,.2),(43870.,.4))),('Other',300,((43860.,.1),))])
            duplicate=rain_bytes([('Station',300,((43860.,.2),)),('Station',600,())])
            count=2000;start=14+1037*count
            row=b'Station'+b'\0'*1018+struct.pack('<iii',300,start,start+24)
            shared=b'SWMM5-RAIN'+struct.pack('<i',count)+row*count+struct.pack('<dfdf',43860.,0.,43870.,.2)
            for family,path in self.libraries():
                lib=load_rain_library(path,family)
                for name,data in (('separate',separate),('duplicate',duplicate),('shared',shared)):
                    with self.subTest(family=family,case=name):
                        cache.write_bytes(data)
                        self.assertFalse(any(execute(lib,root,source,finish=False)))

    def test_invalid_raw_calendar_nonfinite_and_oversized_clock_do_not_enter_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);raw=root/'user.dat';cache=root/'rain.ifc'
            source=rain_model(raw).to_document().text+f'[FILES]\nSAVE RAINFALL "{cache}"\n'
            cases=('Station 2020 2 31 0 5 .2\n','Station 2020 1 30 0 5 nan\n',
                   'Station 2020 1 30 0 5 inf\n','Station 2020 1 30 2147483647 5 .2\n',
                   'Station 2020 1 30 0 60 .2\n')
            for family,path in self.libraries():
                lib=load_rain_library(path,family)
                for row in cases:
                    with self.subTest(family=family,row=row):
                        raw.write_text('Station 2020 1 30 0 0 .1\n'+row)
                        codes=execute(lib,root,source,finish=False)
                        self.assertEqual(codes,(0,319,319,0))
                        self.assertFalse(cache.exists())

    def test_short_historical_records_and_invalid_online_offsets_are_bounded(self):
        from test_historical_rainfall_v2 import fixture
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);raw=root/'history.dat';cache=root/'rain.ifc'
            source=rain_model(raw,station='Archive').to_document().text+f'[FILES]\nSAVE RAINFALL "{cache}"\n'
            for family,path in self.libraries():
                lib=load_rain_library(path,family)
                for data in (b'HPCP\nCOOP:1 :\n',b'x'*100+b'HPCP\nCOOP:1 20200130 00:00\n'):
                    raw.write_bytes(data)
                    self.assertEqual(execute(lib,root,source,finish=False),(0,319,319,0))
                for kind in ('NWS_SPACE_DELIMITED','AES_HLY','CMC_HLY','CMC_FIF'):
                    with self.subTest(family=family,format=kind):
                        data=fixture(kind);raw.write_bytes(data+b'\n1\n'+data.splitlines()[0][:32]+b'\n')
                        self.assertFalse(any(execute(lib,root,source,finish=False)))


if __name__=='__main__':
    unittest.main()
