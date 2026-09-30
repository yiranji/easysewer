"""Direct native RDII I/O; independent bytes bypass Model and file preflight."""

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
from easysewer.io.runoff_cache import RdiiData
from easysewer.utils import probe_library_path
from test_native_v2_standard_io import direct_library, execute, handles


SOURCE = '''[OPTIONS]
FLOW_UNITS CFS
START_DATE 01/01/2020
END_DATE 01/01/2020
END_TIME 0:10
REPORT_STEP 0:01
WET_STEP 0:01
ROUTING_STEP 1
[JUNCTIONS]
J 2 10
[OUTFALLS]
O 0 FREE
[CONDUITS]
C J O 100 .013 0 0
[XSECTIONS]
C CIRCULAR 2 0 0 0 1
[RAINGAGES]
R INTENSITY 0:01 1 TIMESERIES Rain
[TIMESERIES]
Rain 0 .5 0:10 0
[HYDROGRAPHS]
UH R
UH ALL SHORT .1 .1 1
[RDII]
J UH 2
'''


def binary_header(step=60, indices=(0,)):
    return b'SWMM5-RDII'+struct.pack('<ii',step,len(indices))+struct.pack('<'+'i'*len(indices),*indices)


def binary_frame(date=43831., flows=(.5,)):
    return struct.pack('<d'+'f'*len(flows),date,*flows)


def text_header(units='CFS', nodes=('J',)):
    return ('SWMM5\nRDII fixture\n60 - reporting seconds\n1 - constituents\nFLOW '+units+
            '\n'+str(len(nodes))+'\n'+'\n'.join(nodes)+'\nNode Year Mon Day Hr Min Sec Flow\n').encode()


def malformed_rdii():
    header=binary_header();frame=binary_frame();cases=[]
    for n in range(len(header)):
        cases.append(('header-'+str(n),header[:n]))
    for n in range(1,len(frame)):
        cases.append(('frame-'+str(n),header+frame[:n]))
    for step in (-2147483648,-1,0):
        cases.append(('step-'+str(step),binary_header(step)+frame))
    for count in (-2147483648,-1,0,3,2147483647):
        cases.append(('count-'+str(count),b'SWMM5-RDII'+struct.pack('<ii',60,count)))
    for indices in ((-1,),(2,),(2147483647,),(1,),(0,0)):
        cases.append(('indices-'+str(indices),binary_header(indices=indices)+binary_frame(flows=(.5,)*len(indices))))
    for x in (float('nan'),float('inf'),-float('inf')):
        cases.extend((('date-'+str(x),header+binary_frame(x)),('flow-'+str(x),header+binary_frame(flows=(x,)))))
    for x in (-1e100,-693595.,2958466.,1e100):
        cases.append(('date-range-'+str(x),header+binary_frame(x)))
    for x in (43830.,43831.,43831.+30/86400):
        cases.append(('overlap-'+str(x),header+frame+binary_frame(x)))
    cases.append(('later-partial',header+frame+binary_frame(43832.)+b'x'))
    cases.append(('later-nan',header+frame+binary_frame(43832.,(float('nan'),))))
    text=text_header();row=b'J 2020 1 1 0 0 0 .5\n'
    cases.extend((('text-wrong-label',text+row.replace(b'J ',b'OTHER ')),
        ('text-missing-flow',text+b'J 2020 1 1 0 0 0\n'),
        ('text-extra-column',text+row.rstrip()+b' 1\n'),
        ('text-trailing-partial',text+row+b'J 2020 1\n'),
        ('text-duplicate',text+row+row),
        ('text-nul',text+row.rstrip()+b'\0ignored\n'),
        ('text-long',text+b'J '*2000+b'\n'),
        ('text-duplicate-node',text_header(nodes=('J','J'))+row+row),
        ('text-unknown-node',text_header(nodes=('OTHER',))+row)))
    for index,values in ((0,('0','10000','2147483648','-1')),(1,('0','13')),
                         (2,('0','32')),(3,('-1','24')),(4,('-1','60')),
                         (5,('-1','60')),(6,('nan','inf','-inf','1e300','1.2x'))):
        for value in values:
            fields=['2020','1','1','0','0','0','.5'];fields[index]=value
            cases.append(('text-field-'+str(index)+'-'+value,text+('J '+' '.join(fields)+'\n').encode()))
    cases.append(('text-invalid-calendar',text+b'J 2020 2 30 0 0 0 .5\n'))
    cases.append(('text-mixed-dates',text_header(nodes=('J','O'))+row+b'O 2020 1 1 0 1 0 .5\n'))
    cases.append(('text-partial-frame',text_header(nodes=('J','O'))+row))
    return cases


def load_rdii(path,family):
    lib,_=direct_library(path,revision_symbol='swmm_getEasySewerStandardFixes' if family=='standard' else 'swmm_getEasySewerNativeIOFixes')
    if lib.swmm_getEasySewerRdiiIO()!=1:raise AssertionError('RDII I/O revision 1 required')
    return lib


def check_malformed(path,family,directory):
    root=Path(directory);lib=load_rdii(path,family);cache=root/'rdii.bin'
    source=SOURCE+f'[FILES]\nUSE RDII "{cache}"\n'
    cache.write_bytes(binary_header()+binary_frame());assert not any(execute(lib,root,source))
    before=handles()
    for name,raw in malformed_rdii():
        cache.write_bytes(raw);codes=execute(lib,root,source)
        if codes!=(0,345,345,0):raise AssertionError((name,codes))
        if lib.swmm_close()!=0:raise AssertionError('repeated close')
        cache.unlink()
    if handles()!=before:raise AssertionError('RDII handle leak')
    cache.write_bytes(binary_header()+binary_frame());assert not any(execute(lib,root,source))
    print(json.dumps(dict(cases=len(malformed_rdii()),handles_released=True)))


@unittest.skipUnless(get_native_capabilities()['swmm_solver'] and get_native_capabilities()['flexible_ponding'],'Both native families required')
class NativeRdiiIOTests(unittest.TestCase):
    def libraries(self):
        for family,name,variable in (('standard','swmm5','EASYSEWER_STANDARD_TEST_LIBRARY'),
                                     ('custom','flexible_ponding','EASYSEWER_CUSTOM_TEST_LIBRARY')):
            yield family,os.environ.get(variable) or probe_library_path(name)

    def test_malformed_frames_indices_and_text_identity_fail_without_leaks(self):
        with tempfile.TemporaryDirectory() as directory:
            for family,path in self.libraries():
                with self.subTest(family=family):
                    folder=Path(directory)/family;folder.mkdir()
                    run=subprocess.run([sys.executable,'-B','-c',
                        'import sys;from test_native_v2_rdii_io import check_malformed;check_malformed(*sys.argv[1:])',
                        str(path),family,str(folder)],capture_output=True,text=True,timeout=45,
                        creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                    self.assertEqual(run.returncode,0,run.stdout+run.stderr)
                    self.assertEqual(json.loads(run.stdout)['cases'],len(malformed_rdii()))

    def test_sparse_dry_and_readonly_binary_reuse(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);cache=root/'rdii.bin'
            for family,path in self.libraries():
                lib=load_rdii(path,family)
                for frames in (b'',binary_frame(),binary_frame()+binary_frame(43831.+300/86400),
                               binary_frame()+binary_frame(43832.)):
                    with self.subTest(family=family,bytes=len(frames)):
                        raw=binary_header()+frames;cache.write_bytes(raw)
                        cache.chmod(stat.S_IREAD|stat.S_IRGRP|stat.S_IROTH)
                        try:
                            self.assertFalse(any(execute(lib,root,SOURCE+f'[FILES]\nUSE RDII "{cache}"\n')))
                            self.assertEqual(cache.read_bytes(),raw)
                        finally:cache.chmod(stat.S_IREAD|stat.S_IWRITE)

    def test_text_units_match_independent_binary_and_named_nodes_need_no_rdii_binding(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);cache=root/'rdii.bin'
            for family,path in self.libraries():
                lib=load_rdii(path,family);source=SOURCE+f'[FILES]\nUSE RDII "{cache}"\n'
                cache.write_bytes(binary_header()+binary_frame())
                self.assertFalse(any(execute(lib,root,source)));expected=(root/'model.out').read_bytes()
                # Constants are pinned independently from swmm5.c's Qcf table.
                for units,factor in (('CFS',1.),('GPM',448.831),('MGD',.64632),('CMS',.02832),('LPS',28.317),('MLD',2.4466)):
                    with self.subTest(family=family,units=units):
                        cache.write_bytes(text_header(units)+f'j 2020 1 1 0 0 0 {.5*factor:.17g}\n'.encode())
                        self.assertFalse(any(execute(lib,root,source)))
                        self.assertEqual((root/'model.out').read_bytes(),expected)
                cache.write_bytes(text_header(nodes=('J','O'))+b'J 2020 1 1 0 0 0 .5\nO 2020 1 1 0 0 0 0\n')
                self.assertFalse(any(execute(lib,root,source)))
                self.assertEqual((root/'model.out').read_bytes(),expected)

    def test_save_calendar_steps_legacy_and_unused_unbound_group(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);cache=root/'saved.bin'
            for family,path in self.libraries():
                lib=load_rdii(path,family)
                for step in (1,7,17,60,61,120):
                    with self.subTest(family=family,step=step):
                        source=SOURCE.replace('WET_STEP 0:01',f'WET_STEP 0:00:{step}' if step<60 else f'WET_STEP 0:{step//60}:{step%60}')
                        self.assertFalse(any(execute(lib,root,source+f'[FILES]\nSAVE RDII "{cache}"\n')))
                        raw=cache.read_bytes();data=RdiiData.from_bytes(raw)
                        self.assertGreater(len(data.frames),1);self.assertEqual(raw,data.to_bytes())
                        self.assertFalse(any(execute(lib,root,source+f'[FILES]\nUSE RDII "{cache}"\n')))
                for replacement in ('UH ALL .1 .1 1 0 0 0 0 0 0 0 0 0',
                                    'UH ALL SHORT .1 .1 1\nUnused ALL SHORT .1 .1 1'):
                    source=SOURCE.replace('UH ALL SHORT .1 .1 1',replacement)
                    self.assertFalse(any(execute(lib,root,source)))

    def test_nonfinite_parameters_and_cast_overflow_are_input_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for family,path in self.libraries():
                lib=load_rdii(path,family)
                for value in ('nan','inf','-inf','1e300'):
                    for row in (f'UH ALL SHORT .1 {value} 1',f'UH ALL .1 {value} 1 0 0 0 0 0 0 0 0 0'):
                        with self.subTest(family=family,row=row):
                            self.assertEqual(execute(lib,root,SOURCE.replace('UH ALL SHORT .1 .1 1',row)),(200,0))
                self.assertEqual(execute(lib,root,SOURCE.replace('J UH 2','J UH nan')),(200,0))
                # Finite input can still produce a flow outside the float32
                # cache range. Fail generation before casting or publishing it.
                cache=root/'overflow.bin'
                source=SOURCE.replace('J UH 2','J UH 1e100')+f'[FILES]\nSAVE RDII "{cache}"\n'
                self.assertEqual(execute(lib,root,source),(0,346,346,0))
                self.assertEqual(cache.read_bytes(),binary_header())

    def test_used_hydrograph_needs_explicit_gage_for_generation_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);cache=root/'rdii.bin';cache.write_bytes(binary_header()+binary_frame())
            source=SOURCE.replace('UH R\n','')
            for family,path in self.libraries():
                lib=load_rdii(path,family)
                self.assertEqual(execute(lib,root,source),(0,154,154,0))
                self.assertFalse(any(execute(lib,root,source+f'[FILES]\nUSE RDII "{cache}"\n')))


if __name__=='__main__':unittest.main()
