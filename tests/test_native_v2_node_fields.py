"""Node field facts against both packaged engines and actual result recovery."""
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
from test_native_v2_regulator_fields import FAMILIES,library
from test_node_fields_v2 import load,storage_source,outfall_model,queries,NODE,OUTFALL,UNITS,SHAPES
from test_nodes_v2 import divider_model

EVIDENCE=[]


@unittest.skipUnless(get_native_capabilities()['swmm_solver'] and get_native_capabilities()['flexible_ponding'],
                     'Standard/custom native solvers unavailable')
class NativeNodeFieldTests(unittest.TestCase):
    def observe(self,lib,base,text,*,full=False):
        inp,rpt,out=(base/('model'+suffix) for suffix in ('.inp','.rpt','.out'))
        inp.write_text(text,encoding='utf-8');started=False
        try:
            self.assertEqual(lib.swmm_open(os.fsencode(inp),os.fsencode(rpt),os.fsencode(out)),0,
                             rpt.read_text(errors='replace'))
            indexes=[lib.swmm_getIndex(2,name) for name in (b'J',b'O')]
            self.assertTrue(all(i>=0 for i in indexes))
            maxima=[lib.swmm_getValue(302,i) for i in indexes]
            self.assertEqual(lib.swmm_start(int(full)),0);started=True
            initial=[lib.swmm_getValue(303,i) for i in indexes]
            history=[]
            if full:
                for _ in range(2000):
                    elapsed=ctypes.c_double();self.assertEqual(lib.swmm_step(ctypes.byref(elapsed)),0)
                    if not elapsed.value:break
                    history.append((elapsed.value,*[lib.swmm_getValue(code,i) for i in indexes for code in (303,304,305)]))
                else:self.fail('Fixture exceeded step bound')
                self.assertGreater(len(history),10)
            self.assertEqual(lib.swmm_end(),0);started=False
        finally:
            if started:self.assertEqual(lib.swmm_end(),0)
            self.assertEqual(lib.swmm_close(),0)
        return dict(maxima=maxima,initial=initial,history=history,out_sha256=hashlib.sha256(out.read_bytes()).hexdigest() if full else None)

    def test_storage_and_divider_depths_in_all_units_and_routing_modes(self):
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory)
            for family,name,symbol in FAMILIES:
                lib,path=library(name,symbol);rows=[]
                for units in UNITS:
                    for routing in ('STEADY','KINWAVE','DYNWAVE'):
                        for kind in SHAPES:
                            for label,depth,initial,surcharge in (('open',.5,.4,0),('closed',.5,1.2,.5),('deep',5,2,0)):
                                with self.subTest(family=family,units=units,routing=routing,shape=kind,case=label):
                                    model=load(storage_source(kind,units=units,routing=routing))
                                    model.nodes.update('J',max_depth=depth,initial_depth=initial,surcharge_depth=surcharge)
                                    actual=self.observe(lib,base,model.to_document().text)
                                    fact=model.inspect_field(NODE,'max_depth').semantics.effective
                                    self.assertEqual(fact.status,'known')
                                    self.assertAlmostEqual(actual['maxima'][0],fact.value,places=10)
                                    self.assertAlmostEqual(actual['initial'][0],model.inspect_field(NODE,'initial_depth').semantics.effective.value,places=10)
                                    rows.append(dict(units=units,routing=routing,variant=kind,case=label,max_depth=actual['maxima'][0],initial_depth=actual['initial'][0]))
                        for kind in ('OVERFLOW','CUTOFF','TABULAR','WEIR'):
                            for depth in (None,5):
                                with self.subTest(family=family,units=units,routing=routing,law=kind,max_depth=depth):
                                    model=divider_model(kind,routing);model.reinterpret_units(units)
                                    model.nodes.update('J',max_depth=depth,initial_depth=.5)
                                    actual=self.observe(lib,base,model.to_document().text)
                                    self.assertAlmostEqual(actual['maxima'][0],model.inspect_field(NODE,'max_depth').semantics.effective.value,places=10)
                                    self.assertAlmostEqual(actual['initial'][0],model.inspect_field(NODE,'initial_depth').semantics.effective.value,places=10)
                                    rows.append(dict(units=units,routing=routing,variant='DIVIDER-'+kind,case='omitted' if depth is None else 'deep',max_depth=actual['maxima'][0],initial_depth=actual['initial'][0]))
                EVIDENCE.append(dict(kind='node-depth-native',family=family,cases=len(rows),rows=rows,
                                     library_sha256=hashlib.sha256(Path(path).read_bytes()).hexdigest()))

    def test_outfall_boundaries_preserve_complete_native_results(self):
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory)
            for family,name,symbol in FAMILIES:
                lib,path=library(name,symbol);rows=[]
                for units in UNITS:
                    for kind in ('FREE','NORMAL','FIXED','TIDAL','TIMESERIES'):
                        with self.subTest(family=family,units=units,boundary=kind):
                            model=outfall_model(kind);model.reinterpret_units(units)
                            model=load(model.to_document().text)
                            expected=self.observe(lib,base,model.to_document().text,full=True)
                            actual=self.observe(lib,base,model.to_document(normalize=True).text,full=True)
                            self.assertEqual(actual,expected)
                            self.assertEqual(model.inspect_field(OUTFALL,'boundary').semantics.effective.status,'known')
                            if kind in ('FIXED','TIDAL','TIMESERIES'):
                                for row in actual['history']:self.assertAlmostEqual(row[5],12.,places=10)
                            rows.append(dict(units=units,boundary=kind,steps=len(actual['history']),out_sha256=actual['out_sha256']))
                EVIDENCE.append(dict(kind='outfall-native-results',family=family,cases=len(rows),rows=rows,
                                     library_sha256=hashlib.sha256(Path(path).read_bytes()).hexdigest()))

    def test_node_queries_survive_archive_and_relocated_checkpoint(self):
        from easysewer.runtime import RunResult
        from test_native_v2_runner_checkpoint import NativeRunnerCheckpointTests,runner,resume_config
        helper=NativeRunnerCheckpointTests()
        with patch.dict(os.environ,EASYSEWER_CHECKPOINT_PACKAGED='1'):
            for family in ('standard','custom'):
                for kind in ('STORAGE','DIVIDER','OUTFALL',*(('DIVIDER-ACTIVE',) if family=='standard' else ())):
                    with self.subTest(family=family,kind=kind),tempfile.TemporaryDirectory() as directory:
                        root=Path(directory)
                        if kind=='STORAGE':model=load(storage_source('TABULAR'))
                        else:
                            model=(divider_model(routing='KINWAVE' if kind=='DIVIDER-ACTIVE' else 'DYNWAVE')
                                   if kind.startswith('DIVIDER') else outfall_model('TIMESERIES'))
                            model=load(model.to_document().text+'[MAP]\nUNITS METERS\n')
                        model.update_options(allow_ponding=True)
                        owner=OUTFALL if kind=='OUTFALL' else NODE
                        before=queries(model,owner)
                        original,saved=helper.original(root,family,model=model)
                        helper.success(original);self.assertTrue(saved)
                        self.assertEqual(queries(original.snapshot.model(),owner),before)
                        original.save(root/'expected');expected=RunResult.load(root/'expected')
                        self.assertEqual(queries(expected.snapshot.model(),owner),before)
                        shutil.rmtree(root/'first');(root/'original.hsf').unlink();(root/'saved').rename(root/'moved')
                        actual=runner(family).resume(root/'moved'/saved[0].directory.name,resume_config(root/'resumed'))
                        helper.equivalent(expected,actual);self.assertEqual(queries(actual.snapshot.model(),owner),before)
                        EVIDENCE.append(dict(kind='node-fields-checkpoint',family=family,variant=kind,fields=len(before),
                                             out_sha256=actual.output.sha256,original_workspace_removed=True))


if __name__=='__main__':unittest.main()
