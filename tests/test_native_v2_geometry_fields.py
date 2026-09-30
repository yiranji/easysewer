"""Geometry field contracts checked against both packaged native solvers."""

import ctypes
import hashlib
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from easysewer import get_native_capabilities
from easysewer.io.inp import InpDocument
from easysewer.model import Model
from easysewer.utils import probe_library_path
from test_native_v2_standard_io import direct_library
from test_native_v2_network import CASES, RESOURCES
from test_network_fields_v2 import source, NODE, PIPE
from test_geometry_fields_v2 import queries

EVIDENCE = []


def cases():
    for kind, parameters in CASES.items():
        yield kind, kind+' '+parameters, RESOURCES.get(kind,''), 0
    for kind, count in (('HORIZ_ELLIPSE',23), ('VERT_ELLIPSE',23), ('ARCH',102)):
        for code in range(1,count+1):
            yield f'{kind}-{code}-short', f'{kind} {code} 0 0 0', '', 0
            yield f'{kind}-{code}-third', f'{kind} 99 88 {code} 0', '', 0
        yield kind+'-invalid', f'{kind} {count+1} 0 0 0', '', 200
    for offset in ('-1000', '0.123456789', '1e12', '1e20'):
        resource=('[TRANSECTS]\nNC .03 .03 .02\n'
                  f'X1 Transect1 3 0 10 0 0 0 0 {offset}\nGR 2 0 0 5 2 10\n[REPORT]\n')
        yield 'transect-offset-'+offset, 'IRREGULAR Transect1', resource, 200 if offset=='1e20' else 0
    for label, parameters in (
        ('crown','5 .05 10 .016'),
        ('backing','5 .15 2 .016 .05 1 2 2 10 .03'),
        ('half','5 .15 2 .016 .05 1 1 2 10 .03'),
        ('ignored-backing','5 .15 2 .016 0 0 2 0 99 99'),
    ):
        yield 'street-'+label, 'STREET Street1', '[STREETS]\nStreet1 '+parameters+'\n', 0


@unittest.skipUnless(get_native_capabilities()['swmm_solver'] and get_native_capabilities()['flexible_ponding'],
                     'Standard/custom native solvers unavailable')
class NativeGeometryFieldTests(unittest.TestCase):
    def test_barrel_count_boundaries(self):
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory)
            inp,rpt,out=(base/('model'+suffix) for suffix in ('.inp','.rpt','.out'))
            for family,name,symbol in (('standard','swmm5','swmm_getEasySewerStandardFixes'),
                                       ('custom','flexible_ponding','swmm_getEasySewerNativeIOFixes')):
                lib,path=direct_library(probe_library_path(name),revision_symbol=symbol)
                rows=[]
                for count in (1,127,128,255,256,257):
                    text=source(section=f'CIRCULAR 1 0 0 0 {count}')
                    model=Model.from_document(InpDocument.from_text(text))
                    fact=model.inspect_field(PIPE,('section','barrels')).semantics.effective
                    inp.write_text(text,encoding='utf-8')
                    try:
                        error=lib.swmm_open(os.fsencode(inp),os.fsencode(rpt),os.fsencode(out))
                        self.assertEqual(error==0,count in (1,127,257))
                        self.assertEqual(fact.status,'known' if count<=127 else 'unknown')
                        rows.append(dict(count=count,error=error,status=fact.status,value=fact.value))
                    finally:
                        self.assertEqual(lib.swmm_close(),0)
                EVIDENCE.append(dict(kind='geometry-barrel-boundaries',family=family,rows=rows,
                                     library_sha256=hashlib.sha256(Path(path).read_bytes()).hexdigest()))

    def test_all_standard_sizes_variants_units_and_resource_preprocessing(self):
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory)
            inp,rpt,out=(base/('model'+suffix) for suffix in ('.inp','.rpt','.out'))
            for family,name,symbol in (('standard','swmm5','swmm_getEasySewerStandardFixes'),
                                       ('custom','flexible_ponding','swmm_getEasySewerNativeIOFixes')):
                lib,path=direct_library(probe_library_path(name),revision_symbol=symbol)
                lib.swmm_getValue.argtypes=[ctypes.c_int,ctypes.c_int];lib.swmm_getValue.restype=ctypes.c_double
                lib.swmm_getIndex.argtypes=[ctypes.c_int,ctypes.c_char_p];lib.swmm_getIndex.restype=ctypes.c_int
                rows=[]
                for units in ('CFS','GPM','MGD','CMS','LPS','MLD'):
                    for label,geometry,resource,error in cases():
                        with self.subTest(family=family,units=units,case=label):
                            text=source(section=geometry,units=units)+resource
                            model=Model.from_document(InpDocument.from_text(text))
                            if label.startswith('transect-offset-'):
                                self.assertEqual(model.transects['Transect1'].elevation_offset,
                                                 float(label.removeprefix('transect-offset-')))
                            fact=model.inspect_field(NODE,'max_depth').semantics.effective
                            inp.write_text(text,encoding='utf-8')
                            try:
                                actual_error=lib.swmm_open(os.fsencode(inp),os.fsencode(rpt),os.fsencode(out))
                                if error:
                                    self.assertNotEqual(actual_error,0)
                                    self.assertEqual(fact.status,'invalid')
                                    rows.append(dict(units=units,case=label,error=actual_error))
                                    continue
                                self.assertEqual(actual_error,0)
                                self.assertEqual(fact.status,'known')
                                index=lib.swmm_getIndex(2,b'J');self.assertGreaterEqual(index,0)
                                actual=lib.swmm_getValue(302,index)
                                self.assertAlmostEqual(actual,fact.value,places=10)
                                link_index=lib.swmm_getIndex(3,b'P');self.assertGreaterEqual(link_index,0)
                                full=lib.swmm_getValue(405,link_index)
                                shape=model.links['P'].section.geometry
                                if hasattr(shape,'full_depth'):
                                    info=model.inspect_field(PIPE,('section','geometry','full_depth'))
                                    self.assertEqual(info.semantics.effective.status,'known')
                                    self.assertAlmostEqual(full,info.semantics.effective.value,places=10)
                                rows.append(dict(units=units,case=label,full_depth=full,max_depth=actual,
                                                 query_max_depth=fact.value))
                            finally:
                                self.assertEqual(lib.swmm_close(),0)
                EVIDENCE.append(dict(kind='geometry-fields-native',family=family,cases=len(rows),rows=rows,
                                     library_sha256=hashlib.sha256(Path(path).read_bytes()).hexdigest()))

    def test_geometry_queries_survive_archive_and_relocated_checkpoint(self):
        from easysewer.runtime import RunResult
        from test_native_v2_runner_checkpoint import NativeRunnerCheckpointTests, runner, resume_config
        helper=NativeRunnerCheckpointTests()
        with patch.dict(os.environ,EASYSEWER_CHECKPOINT_PACKAGED='1'):
            for family in ('standard','custom'):
                for kind, geometry in (('ARCH','ARCH 3 0 0 0'), ('IRREGULAR','IRREGULAR Transect1'), ('STREET','STREET Street1')):
                    with self.subTest(family=family,kind=kind),tempfile.TemporaryDirectory() as directory:
                        root=Path(directory)
                        text=source(tail=' 4',section=geometry)+RESOURCES.get(kind,'')+'[INFLOWS]\nJ FLOW "" FLOW 1 1 0.01\n'
                        model=Model.from_document(InpDocument.from_text(text,source='geometry-source.inp'),strict=True)
                        model.update_options(allow_ponding=True)
                        before=queries(model)
                        original,saved=helper.original(root,family,model=model)
                        helper.success(original);self.assertEqual(queries(original.snapshot.model()),before)
                        original.save(root/'expected');expected=RunResult.load(root/'expected')
                        self.assertEqual(queries(expected.snapshot.model()),before)
                        shutil.rmtree(root/'first');(root/'original.hsf').unlink();(root/'saved').rename(root/'moved')
                        actual=runner(family).resume(root/'moved'/saved[0].directory.name,resume_config(root/'resumed'))
                        helper.equivalent(expected,actual);self.assertEqual(queries(actual.snapshot.model()),before)
                        EVIDENCE.append(dict(kind='geometry-fields-checkpoint',family=family,geometry=kind,
                            fields=len(before),out_sha256=actual.output.sha256,original_workspace_removed=True))


if __name__=='__main__':
    unittest.main()
