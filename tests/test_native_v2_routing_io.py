"""Direct native routing frames, mapping, units and caller file ownership."""

import json
from dataclasses import replace
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest

from easysewer import get_native_capabilities
from easysewer.io.routing import RoutingInterface
from easysewer.utils import probe_library_path
from test_native_v2_rdii_io import SOURCE as RDII_SOURCE
from test_native_v2_standard_io import direct_library, execute, handles

SOURCE=RDII_SOURCE.split('[RAINGAGES]')[0]


def header(nodes=('J',), pollutants=(), units='CFS'):
    return ('SWMM5 Interface File\nfixture\n60\n'+str(1+len(pollutants))+'\nFLOW '+units+'\n'+
        ''.join(name+' '+unit+'\n' for name,unit in pollutants)+str(len(nodes))+'\n'+
        '\n'.join(nodes)+'\nNode Year Mon Day Hr Min Sec FLOW\n').encode()


def row(node='J',minute=0,flow='.5',quality=''):
    return f'{node} 2020 1 1 0 {minute} 0 {flow}{quality}\n'.encode()


def malformed_routing():
    h=header();r=row();data=h+r+row(minute=10);cases=[('header-only',h)]
    lines=h.splitlines(keepends=True)
    cases.extend(('header-'+str(i),b''.join(lines[:i])) for i in range(len(lines)))
    for n in (1,2,7,14):
        cases.extend((('partial-first-'+str(n),h+r[:n]),
            ('partial-late-'+str(n),data+row(minute=20)[:n])))
    for flow in ('nan','inf','-inf','1e300','junk','1.2suffix'):
        cases.append(('flow-'+flow,data.replace(b'.5',flow.encode())))
    cases.extend((('wrong-label',data.replace(b'J 2020',b'OTHER 2020')),
        ('duplicate-date',h+r+r),('backward-date',h+row(minute=10)+r),
        ('calendar',data.replace(b'2020 1 1',b'2020 2 30')),
        ('overflow-year',data.replace(b'2020',b'2147483648')),
        ('embedded-nul',data.replace(b'.5',b'.5\0junk')),
        ('embedded-cr',data.replace(b'.5',b'.5\rjunk')),
        ('extra-column',data.replace(b'.5',b'.5 1')),
        ('duplicate-nodes',header(nodes=('J','j'))+r+row(node='j')),
        ('mixed-date',header(nodes=('J','O'))+r+row(node='O',minute=1)),
        ('partial-frame',header(nodes=('J','O'))+r),
        ('long-row',h+b'J '*1800+b'\n'),
        ('negative-nodes',h.replace(b'\n1\nJ\n',b'\n-1\nJ\n')),
        ('zero-step',data.replace(b'\n60\n',b'\n0\n')),
        ('duplicate-pollutants',header(pollutants=(('X','MG/L'),('x','MG/L')))+row(quality=' 1 2')),
        ('invalid-unit',data.replace(b'FLOW CFS',b'FLOW XYZ')),
        ('blank-between',h+r+b'\n'+row(minute=10))))
    return cases


def load_routing(path,family):
    lib,_=direct_library(path,revision_symbol='swmm_getEasySewerStandardFixes' if family=='standard' else 'swmm_getEasySewerNativeIOFixes')
    if lib.swmm_getEasySewerRoutingIO()!=1:raise AssertionError('Routing I/O revision 1 required')
    return lib


def check_malformed(path,family,directory):
    root=Path(directory);lib=load_routing(path,family);cache=root/'routing.ifc'
    source=SOURCE+f'[FILES]\nUSE INFLOWS "{cache}"\n'
    valid=header()+row()+row(minute=10);cache.write_bytes(valid)
    assert not any(execute(lib,root,source))
    before=handles()
    for name,data in malformed_routing():
        cache.write_bytes(data);codes=execute(lib,root,source)
        if codes!=(0,353,353,0):raise AssertionError((name,codes))
        assert cache.read_bytes()==data and lib.swmm_close()==0
    assert handles()==before,'Routing handle leak'
    cache.write_bytes(valid);assert not any(execute(lib,root,source))
    print(json.dumps(dict(cases=len(malformed_routing()))))


@unittest.skipUnless(get_native_capabilities()['swmm_solver'] and get_native_capabilities()['flexible_ponding'],'Both native families required')
class NativeRoutingIOTests(unittest.TestCase):
    def test_runner_uses_probed_capabilities_and_preserves_outputs_on_rejection(self):
        from easysewer.io.inp import InpDocument
        from easysewer.model import Model, FileReference
        from easysewer.runtime import Runner, RunConfig, StandardBackend, FlexiblePondingBackend
        from test_files_v2 import bind
        class WithoutRoutingProof(StandardBackend):
            def execution_info(self,info):
                actual=super().execution_info(info)
                return replace(actual,capabilities=tuple(c for c in actual.capabilities if c!='easysewer:routing-io:1'))
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);cache=root/'routing.ifc'
            raw=header(pollutants=(('X','MG/L'),))+row(quality=' 7')+row(minute=10,quality=' 7')
            cache.write_bytes(raw)
            model=Model.from_document(InpDocument.from_text(SOURCE),strict=True)
            model.update_options(allow_ponding=True);bind(model,'INFLOWS','USE',cache)
            for backend in (StandardBackend(),FlexiblePondingBackend()):
                output=root/backend.worker_kind
                config=RunConfig(output_directory=FileReference(path=str(output),direction='output'),backend=backend.key,
                    required_capabilities=('easysewer:routing-io:1',))
                result=Runner().run(model,config)
                self.assertTrue(result.succeeded,repr(result.failure)+' '+repr(result.diagnostics.errors))
                self.assertTrue(result.native_completed)
                self.assertIn('easysewer:routing-io:1',result.backend.capabilities)
                self.assertEqual(cache.read_bytes(),raw)
                if backend.key=='swmm:standard':
                    old={suffix:(output/('model'+suffix)).read_bytes() for suffix in ('.inp','.rpt','.out')}
                    phases=[]
                    rejected=Runner(backends={backend.key:WithoutRoutingProof()}).run(model,
                        replace(config,required_capabilities=(),overwrite=True),progress=lambda p:phases.append(p.phase))
                    self.assertFalse(rejected.succeeded)
                    self.assertIn('files.backend_capabilities_missing',{d.code for d in rejected.diagnostics.errors})
                    self.assertNotIn('opening',phases)
                    self.assertFalse(rejected.native_completed)
                    self.assertEqual(old,{suffix:(output/('model'+suffix)).read_bytes() for suffix in old})

    def libraries(self):
        for family,name,variable in (('standard','swmm5','EASYSEWER_STANDARD_TEST_LIBRARY'),
                                     ('custom','flexible_ponding','EASYSEWER_CUSTOM_TEST_LIBRARY')):
            yield family,os.environ.get(variable) or probe_library_path(name)

    def test_malformed_frames_fail_without_partial_success_or_leaks(self):
        with tempfile.TemporaryDirectory() as directory:
            for family,path in self.libraries():
                with self.subTest(family=family):
                    folder=Path(directory)/family;folder.mkdir()
                    result=subprocess.run([sys.executable,'-B','-c',
                        'import sys;from test_native_v2_routing_io import check_malformed;check_malformed(*sys.argv[1:])',
                        str(path),family,str(folder)],capture_output=True,text=True,timeout=45,
                        creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                    self.assertEqual(result.returncode,0,result.stdout+result.stderr)
                    self.assertEqual(json.loads(result.stdout)['cases'],len(malformed_routing()))

    def test_six_units_readonly_and_ascii_case_have_equal_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);cache=root/'routing.ifc'
            for family,path in self.libraries():
                lib=load_routing(path,family);source=SOURCE+f'[FILES]\nUSE INFLOWS "{cache}"\n'
                expected=None
                for units,factor in (('CFS',1.),('GPM',448.831),('MGD',.64632),('CMS',.02832),('LPS',28.317),('MLD',2.4466)):
                    with self.subTest(family=family,units=units):
                        raw=(header(units=units)+row(node='j',flow=str(.5*factor))+row(node='j',minute=10,flow=str(.5*factor))).rstrip(b'\n')
                        cache.write_bytes(raw);cache.chmod(stat.S_IREAD|stat.S_IRGRP|stat.S_IROTH)
                        try:
                            self.assertFalse(any(execute(lib,root,source)))
                            self.assertEqual(cache.read_bytes(),raw)
                        finally:cache.chmod(stat.S_IREAD|stat.S_IWRITE)
                        actual=(root/'model.out').read_bytes()
                        if expected is None:expected=actual
                        else:self.assertEqual(actual,expected)

    def test_pollutant_subset_reordering_missing_and_zero_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);cache=root/'routing.ifc'
            for family,path in self.libraries():
                lib=load_routing(path,family)
                source=SOURCE+'[POLLUTANTS]\nY MG/L 0 0 0 0\nZ UG/L 0 0 0 0\n'+f'[FILES]\nUSE INFLOWS "{cache}"\n'
                cache.write_bytes(header(pollutants=(('Y','MG/L'),('Z','UG/L')))+row(quality=' 7 0')+row(minute=10,quality=' 7 0'))
                self.assertFalse(any(execute(lib,root,source)));expected=(root/'model.out').read_bytes()
                raw=header(nodes=('Foreign','J'),pollutants=(('Unused','MG/L'),('y','MG/L')))
                for minute in (0,10):raw+=row(node='foreign',minute=minute,flow='1e300',quality=' 1e300 1e300')+row(minute=minute,quality=' 1e300 7')
                cache.write_bytes(raw);self.assertFalse(any(execute(lib,root,source)))
                self.assertEqual((root/'model.out').read_bytes(),expected)
                cache.write_bytes(raw.replace(b'y MG/L',b'y UG/L'))
                self.assertEqual(execute(lib,root,source),(0,355,355,0))
                no_quality=SOURCE+f'[FILES]\nUSE INFLOWS "{cache}"\n'
                cache.write_bytes(raw);self.assertFalse(any(execute(lib,root,no_quality)))
                actual=(root/'model.out').read_bytes()
                cache.write_bytes(header()+row()+row(minute=10))
                self.assertFalse(any(execute(lib,root,no_quality)))
                self.assertEqual((root/'model.out').read_bytes(),actual)

    def test_aliases_and_invalid_input_preserve_callers_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);cache=root/'routing.ifc';output=root/'saved.ifc';alias=root/'alias.ifc'
            for family,path in self.libraries():
                lib=load_routing(path,family);raw=header()+row()+row(minute=10);cache.write_bytes(raw)
                source=SOURCE+f'[FILES]\nUSE INFLOWS "{cache}"\nSAVE OUTFLOWS "{cache}"\n'
                self.assertEqual(execute(lib,root,source),(0,357,357,0));self.assertEqual(cache.read_bytes(),raw)
                os.link(cache,alias)
                try:
                    self.assertEqual(execute(lib,root,source.replace(f'SAVE OUTFLOWS "{cache}"',f'SAVE OUTFLOWS "{alias}"')),(0,357,357,0))
                    self.assertEqual(alias.read_bytes(),raw)
                finally:alias.unlink()
                cache.write_bytes(b'bad');output.write_bytes(b'keep existing output')
                self.assertEqual(execute(lib,root,source.replace(f'SAVE OUTFLOWS "{cache}"',f'SAVE OUTFLOWS "{output}"')),(0,353,353,0))
                self.assertEqual(output.read_bytes(),b'keep existing output')

    def test_native_save_is_complete_and_reusable_with_pollutants(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);cache=root/'saved.ifc'
            for family,path in self.libraries():
                lib=load_routing(path,family)
                source=SOURCE+'[POLLUTANTS]\nX MG/L 0 0 0 0\n[DWF]\nJ FLOW .5\nJ X 7\n'
                self.assertFalse(any(execute(lib,root,source+f'[FILES]\nSAVE OUTFLOWS "{cache}"\n')))
                parsed=RoutingInterface.read(cache)
                self.assertEqual(parsed.nodes,('O',));self.assertEqual(len(parsed.frames),11)
                self.assertEqual(tuple(c.name for c in parsed.constituents),('FLOW','X'))
                self.assertEqual(parsed.frames[0].values,((0.,0.),))
                self.assertGreater(parsed.frames[-1].values[0][0],0.)
                consumer=SOURCE.replace('O 0 FREE','End 0 FREE').replace('C J O','C O End').replace('J 2 10','O 2 10')
                self.assertFalse(any(execute(lib,root,consumer+f'[FILES]\nUSE INFLOWS "{cache}"\n')))
