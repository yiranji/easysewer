"""Direct C qualification of complete RUNOFF cache frames and file ownership."""

from dataclasses import replace
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
from easysewer.io.interface_inspection import inspect_interface
from easysewer.io.runoff_cache import RunoffData, RunoffLayout
from easysewer.utils import probe_library_path
from test_native_v2_standard_io import direct_library, dry_quality_model, execute, handles


def runoff_model(pollutants=2, units='CFS'):
    return dry_quality_model(pollutants, units)


def runoff_bytes(*, frames=2, pollutants=2, units=0, step=300.):
    row = [0.] * (8 + pollutants)
    row[4] = .125
    return (b'SWMM5-RUNOFF' + struct.pack('<4i', 2, pollutants, units, frames) +
            (struct.pack('<f', step) + struct.pack('<'+'f'*len(row), *row)*2)*frames)


def malformed_runoff():
    raw = runoff_bytes(); cases = []
    def changed(name, offset, fmt, value):
        data = bytearray(raw); struct.pack_into(fmt, data, offset, value)
        cases.append((name, bytes(data)))
    for n in (0, 1, 10, 11, 15, 23, 26, 27, 30, 31, 35, len(raw)-1):
        cases.append((f'truncated-{n}', raw[:n]))
    cases.extend((('signature', b'X'+raw[1:]), ('trailing', raw+b'X')))
    for col in range(4):
        for value in (-1, 0, 2147483647):
            if col == 2 and value == 0: continue
            changed(f'header-{col}-{value}', 12+4*col, '<i', value)
    changed('different-units', 20, '<i', 3)
    for offset in (28, 112):
        for value in (0., -1., float('nan'), float('inf'), -float('inf')):
            changed(f'step-{offset}-{value}', offset, '<f', value)
    changed('nonadvancing-clock', 112, '<f', 1e-40)
    for offset in (32+4*i for i in range(20)):
        for value in (float('nan'), float('inf'), -float('inf')):
            changed(f'sample-{offset}-{value}', offset, '<f', value)
    changed('last-sample-nan', len(raw)-4, '<f', float('nan'))
    return cases


def load_runoff(path, family):
    symbol = 'swmm_getEasySewerStandardFixes' if family == 'standard' else 'swmm_getEasySewerNativeIOFixes'
    lib, _ = direct_library(path, revision_symbol=symbol)
    if getattr(lib, symbol)() < 3:
        raise AssertionError('RUNOFF I/O qualification requires revision 3 or later')
    return lib


def check_bad_cases(path, family, directory):
    root=Path(directory); cache=root/'runoff.bin'; lib=load_runoff(path,family)
    model=runoff_model(); source=model.to_document().text+f'[FILES]\nUSE RUNOFF "{cache}"\n'
    cache.write_bytes(runoff_bytes()); execute(lib,root,source,finish=False)
    before=handles()
    for name,data in malformed_runoff():
        cache.write_bytes(data)
        codes=execute(lib,root,source,finish=False)
        if codes != (0,325,325,0): raise AssertionError((name,codes))
        if inspect_interface(data,'RUNOFF',model).report.is_valid:
            raise AssertionError('Python inspection accepted '+name)
        cache.unlink()
    if before != handles(): raise AssertionError('RUNOFF failure leaked handles')
    print(json.dumps(dict(cases=len(malformed_runoff()),handles_released=True)))


@unittest.skipUnless(get_native_capabilities()['swmm_solver'] and
                    get_native_capabilities()['flexible_ponding'], 'Both native solvers required')
class NativeRunoffIOTests(unittest.TestCase):
    def libraries(self):
        for family,name,variable in (
            ('standard','swmm5','EASYSEWER_STANDARD_TEST_LIBRARY'),
            ('custom','flexible_ponding','EASYSEWER_CUSTOM_TEST_LIBRARY')):
            yield family,os.environ.get(variable) or probe_library_path(name)

    def test_damaged_headers_frames_and_clocks_fail_before_state_is_applied(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for family,path in self.libraries():
                with self.subTest(family=family):
                    folder=root/family;folder.mkdir()
                    result=subprocess.run([sys.executable,'-B','-c',
                        'import sys; from test_native_v2_runoff_io import check_bad_cases; check_bad_cases(*sys.argv[1:])',
                        str(path),family,str(folder)],capture_output=True,text=True,timeout=40,
                        creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                    self.assertEqual(result.returncode,0,result.stdout+result.stderr)
                    self.assertEqual(json.loads(result.stdout)['cases'],len(malformed_runoff()))

    def test_save_layout_readonly_reuse_and_independent_rewrite(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); cache=root/'runoff.bin'
            for family,path in self.libraries():
                lib=load_runoff(path,family)
                for units in ('CFS','GPM','MGD','CMS','LPS','MLD'):
                    for pollutants in (0,2,8):
                        with self.subTest(family=family,units=units,pollutants=pollutants):
                            model=runoff_model(pollutants,units);source=model.to_document().text
                            self.assertFalse(any(execute(lib,root,source+f'[FILES]\nSAVE RUNOFF "{cache}"\n')))
                            data=cache.read_bytes();layout=RunoffLayout.from_model(model)
                            self.assertEqual(data[:12],b'SWMM5-RUNOFF')
                            header=struct.unpack_from('<4i',data,12)
                            self.assertEqual(header[:3],(2,pollutants,('CFS','GPM','MGD','CMS','LPS','MLD').index(units)))
                            self.assertEqual(len(data),28+header[3]*(4+2*(8+pollutants)*4))
                            decoded=RunoffData.from_bytes(data,layout=layout)
                            self.assertEqual(decoded.to_bytes(),data)
                            self.assertEqual(decoded.duration_seconds,600.)
                            cache.chmod(stat.S_IREAD|stat.S_IRGRP|stat.S_IROTH)
                            try:
                                self.assertFalse(any(execute(lib,root,source+f'[FILES]\nUSE RUNOFF "{cache}"\n')))
                                first=(root/'model.out').read_bytes()
                                self.assertEqual(cache.read_bytes(),data)
                            finally:cache.chmod(stat.S_IREAD|stat.S_IWRITE)
                            cache.write_bytes(decoded.to_bytes())
                            self.assertFalse(any(execute(lib,root,source+f'[FILES]\nUSE RUNOFF "{cache}"\n')))
                            self.assertEqual((root/'model.out').read_bytes(),first)

    def test_exact_frame_exhaustion_is_327_and_no_state_is_read_past_end(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);cache=root/'short.bin';cache.write_bytes(runoff_bytes(frames=1,step=1.))
            source=runoff_model().to_document().text+f'[FILES]\nUSE RUNOFF "{cache}"\n'
            for family,path in self.libraries():
                with self.subTest(family=family):
                    self.assertEqual(execute(load_runoff(path,family),root,source),(0,0,327,327,0))

    def test_complete_unused_frames_are_checked_without_physical_value_clamping(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);cache=root/'long.bin'
            # Negative finite elevations/flows are not format errors. Samples
            # after the consumer's duration still have to be structurally valid.
            raw=bytearray(runoff_bytes(step=600.));struct.pack_into('<f',raw,32+6*4,-10.)
            source=runoff_model().to_document().text+f'[FILES]\nUSE RUNOFF "{cache}"\n'
            for family,path in self.libraries():
                lib=load_runoff(path,family)
                cache.write_bytes(raw)
                self.assertEqual(execute(lib,root,source,finish=False),(0,0,0,0))
                bad=bytearray(raw);struct.pack_into('<f',bad,len(bad)-4,float('nan'));cache.write_bytes(bad)
                self.assertEqual(execute(lib,root,source,finish=False),(0,325,325,0))

    def test_runoff_start_failure_stops_later_hotstart_initialization(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);cache=root/'bad.bin';saved=root/'untouched.hsf'
            raw=bytearray(runoff_bytes());struct.pack_into('<f',raw,28,float('nan'));cache.write_bytes(raw)
            for family,path in self.libraries():
                lib=load_runoff(path,family)
                saved.write_bytes(b'existing-hotstart')
                source=runoff_model().to_document().text+f'[FILES]\nUSE RUNOFF "{cache}"\n'
                self.assertEqual(execute(lib,root,source+f'SAVE HOTSTART "{saved}"\n',finish=False),(0,325,325,0))
                self.assertEqual(saved.read_bytes(),b'existing-hotstart')
                self.assertEqual(execute(lib,root,source+f'USE HOTSTART "{root / "absent.hsf"}"\n',finish=False),(0,325,325,0))


if __name__ == '__main__': unittest.main()
