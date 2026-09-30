"""Configured regulator facts against the two packaged SWMM implementations."""

import ctypes
import hashlib
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from easysewer import get_native_capabilities
from easysewer.model import network as n
from easysewer.model.values import Offset
from easysewer.utils import probe_library_path
from test_native_v2_standard_io import direct_library
from test_regulator_fields_v2 import fixture, load, queries, NODE, PIPE
from test_regulators_v2 import KINDS

EVIDENCE = []
FAMILIES = (('standard','swmm5','swmm_getEasySewerStandardFixes'),
            ('custom','flexible_ponding','swmm_getEasySewerNativeIOFixes'))


def library(name, symbol):
    lib,path=direct_library(probe_library_path(name),revision_symbol=symbol)
    lib.swmm_getValue.argtypes=[ctypes.c_int,ctypes.c_int]
    lib.swmm_getValue.restype=ctypes.c_double
    lib.swmm_getIndex.argtypes=[ctypes.c_int,ctypes.c_char_p]
    lib.swmm_getIndex.restype=ctypes.c_int
    return lib,path


@unittest.skipUnless(get_native_capabilities()['swmm_solver'] and get_native_capabilities()['flexible_ponding'],
                     'Standard/custom native solvers unavailable')
class NativeRegulatorFieldTests(unittest.TestCase):
    def test_crowns_offsets_and_initial_settings(self):
        cases=(('depth',{}),('negative',dict(offset=-1)),('raised',dict(offset=-1,downstream=12)),
               ('absolute',dict(mode='ELEVATION',offset=10.5)),
               ('below-invert',dict(mode='ELEVATION',offset=9)),
               ('invert-marker',dict(mode='ELEVATION',offset=Offset.NODE_INVERT)),
               ('raised-marker',dict(mode='ELEVATION',offset=Offset.NODE_INVERT,downstream=12)))
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory)
            inp,rpt,out=(base/('model'+suffix) for suffix in ('.inp','.rpt','.out'))
            for family,name,symbol in FAMILIES:
                lib,path=library(name,symbol);rows=[]
                for units in ('CFS','GPM','MGD','CMS','LPS','MLD'):
                    for kind in KINDS:
                        for label,args in cases:
                            with self.subTest(family=family,units=units,kind=kind,case=label):
                                model=load(fixture(kind,units=units,**args))
                                if isinstance(model.links['P'],n.Pump):
                                    model.links.update('P',initially_on=label!='negative')
                                text=model.to_document().text
                                fact=model.inspect_field(NODE,'max_depth').semantics.effective
                                inp.write_text(text,encoding='utf-8');started=False
                                try:
                                    self.assertEqual(lib.swmm_open(os.fsencode(inp),os.fsencode(rpt),os.fsencode(out)),0,
                                                     rpt.read_text(errors='replace'))
                                    node=lib.swmm_getIndex(2,b'J');link=lib.swmm_getIndex(3,b'P')
                                    self.assertGreaterEqual(node,0);self.assertGreaterEqual(link,0)
                                    actual=lib.swmm_getValue(302,node);height=lib.swmm_getValue(405,link)
                                    self.assertEqual(fact.status,'known')
                                    self.assertAlmostEqual(actual,fact.value,places=10)
                                    offset=None
                                    if not isinstance(model.links['P'],n.Pump) and kind!='BOTTOM':
                                        field='crest_height' if isinstance(model.links['P'],n.Weir) else 'offset'
                                        offset=model.inspect_field(PIPE,field).semantics.effective.value
                                        self.assertAlmostEqual(actual-height,offset,places=10)
                                    self.assertEqual(lib.swmm_start(0),0);started=True
                                    setting=lib.swmm_getValue(407,link)
                                    if isinstance(model.links['P'],n.Pump):
                                        self.assertEqual(setting,float(model.inspect_field(PIPE,'initially_on').semantics.effective.value))
                                    rows.append(dict(units=units,variant=kind,case=label,full_depth=height,
                                                     max_depth=actual,offset=offset,initial_setting=setting))
                                finally:
                                    if started:self.assertEqual(lib.swmm_end(),0)
                                    self.assertEqual(lib.swmm_close(),0)
                EVIDENCE.append(dict(kind='regulator-fields-native',family=family,rows=rows,cases=len(rows),
                                     library_sha256=hashlib.sha256(Path(path).read_bytes()).hexdigest()))

    def test_non_dynamic_routing_does_not_raise_crest(self):
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory)
            inp,rpt,out=(base/('model'+suffix) for suffix in ('.inp','.rpt','.out'))
            for family,name,symbol in FAMILIES:
                lib,path=library(name,symbol);rows=[]
                for units in ('CFS','CMS'):
                    for routing in ('DYNWAVE','KINWAVE','STEADY'):
                        for kind in ('SIDE','TRANSVERSE','FUNCTIONAL/HEAD'):
                            with self.subTest(family=family,units=units,routing=routing,kind=kind):
                                # Closed storage permits crown expansion; open storage retains its declared depth.
                                for surcharge,depth in ((1,0),(None,4)):
                                    model=load(fixture(kind,units=units,routing=routing,downstream=12,offset=-1,
                                                       storage=True,surcharge=surcharge,max_depth=depth))
                                    inp.write_text(model.to_document().text,encoding='utf-8')
                                    try:
                                        self.assertEqual(lib.swmm_open(os.fsencode(inp),os.fsencode(rpt),os.fsencode(out)),0,
                                                         rpt.read_text(errors='replace'))
                                        actual=lib.swmm_getValue(302,lib.swmm_getIndex(2,b'J'))
                                        height=lib.swmm_getValue(405,lib.swmm_getIndex(3,b'P'))
                                        field='crest_height' if isinstance(model.links['P'],n.Weir) else 'offset'
                                        offset=model.inspect_field(PIPE,field).semantics.effective.value
                                        self.assertEqual(offset,2. if routing=='DYNWAVE' else 0.)
                                        self.assertAlmostEqual(actual,offset+height if surcharge else depth,places=10)
                                        rows.append(dict(units=units,routing=routing,variant=kind,surcharge=surcharge,
                                                         max_depth=actual,full_depth=height,offset=offset))
                                    finally:self.assertEqual(lib.swmm_close(),0)
                EVIDENCE.append(dict(kind='regulator-routing-native',family=family,rows=rows,cases=len(rows),
                                     library_sha256=hashlib.sha256(Path(path).read_bytes()).hexdigest()))

    def test_queries_survive_archive_and_relocated_checkpoint(self):
        from easysewer.runtime import RunResult
        from test_native_v2_runner_checkpoint import NativeRunnerCheckpointTests, runner, resume_config
        helper=NativeRunnerCheckpointTests()
        with patch.dict(os.environ,EASYSEWER_CHECKPOINT_PACKAGED='1'):
            for family in ('standard','custom'):
                for kind in ('PUMP2','SIDE','TRAPEZOIDAL','FUNCTIONAL/HEAD'):
                    with self.subTest(family=family,kind=kind),tempfile.TemporaryDirectory() as directory:
                        root=Path(directory)
                        model=load(fixture(kind,storage=True,max_depth=8)+'[MAP]\nUNITS METERS\n'
                                   '[INFLOWS]\nJ FLOW "" FLOW 1 1 0.01\n')
                        model.nodes.update('J',initial_depth=2)
                        before=queries(model)
                        original,saved=helper.original(root,family,model=model)
                        helper.success(original);self.assertEqual(queries(original.snapshot.model()),before)
                        self.assertTrue(saved)
                        original.save(root/'expected');expected=RunResult.load(root/'expected')
                        self.assertEqual(queries(expected.snapshot.model()),before)
                        shutil.rmtree(root/'first');(root/'original.hsf').unlink();(root/'saved').rename(root/'moved')
                        actual=runner(family).resume(root/'moved'/saved[0].directory.name,resume_config(root/'resumed'))
                        helper.equivalent(expected,actual);self.assertEqual(queries(actual.snapshot.model()),before)
                        EVIDENCE.append(dict(kind='regulator-fields-checkpoint',family=family,variant=kind,
                                             fields=len(before),out_sha256=actual.output.sha256,original_workspace_removed=True))


if __name__=='__main__':
    unittest.main()
