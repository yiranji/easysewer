"""Direct solver observations and public continuation for network queries."""
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
from easysewer.model import Model, Ref
from easysewer.utils import probe_library_path
from test_native_v2_standard_io import direct_library
from test_network_fields_v2 import source, NODE, PIPE

EVIDENCE = []
PATHS = (
    *((NODE, name) for name in ('id','elevation','max_depth','initial_depth','surcharge_depth','ponded_area','position')),
    *((PIPE, name) for name in ('id','inlet','outlet','length','roughness','inlet_offset','outlet_offset','initial_flow','maximum_flow','section','losses','vertices')),
    (PIPE, ('inlet','key')), (PIPE, ('inlet','collection')),
    (NODE, ('position','x')), (NODE, ('position','y')),
    *((PIPE, ('losses',name)) for name in ('entry','exit','average','flap_gate','seepage')),
    (PIPE, ('vertices',0,'x')), (PIPE, ('vertices',0,'y')),
)


def queries(model):
    return tuple((model.inspect_field(owner,path),model.field_provenance(owner,path)) for owner,path in PATHS)


@unittest.skipUnless(get_native_capabilities()['swmm_solver'] and get_native_capabilities()['flexible_ponding'],
    'Standard/custom native solvers unavailable')
class NativeNetworkFieldTests(unittest.TestCase):
    def test_native_crown_adjustments_all_units_and_both_families(self):
        cases = (
            ('omitted',{}), ('zero',dict(tail=' 0')), ('below',dict(tail=' .25')), ('above',dict(tail=' 5')),
            ('initial',dict(tail=' 0 .5')), ('depth-negative',dict(offsets=('-1','-1'))),
            ('invert',dict(mode='ELEVATION',offsets=('*','*'))),
            ('below-invert',dict(mode='ELEVATION',offsets=('9','8'))),
            ('above-invert',dict(mode='ELEVATION',offsets=('10.5','9.5'))),
            ('filled',dict(section='FILLED_CIRCULAR 2 .5 0 0')),
            ('reverse',dict(offsets=('0','2'))),
            ('trapezoid',dict(section='TRAPEZOIDAL 2 1 1 1')),
        )
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for family,name,symbol in (('standard','swmm5','swmm_getEasySewerStandardFixes'),
                                       ('custom','flexible_ponding','swmm_getEasySewerNativeIOFixes')):
                lib,path=direct_library(probe_library_path(name),revision_symbol=symbol)
                lib.swmm_getValue.argtypes=[ctypes.c_int,ctypes.c_int];lib.swmm_getValue.restype=ctypes.c_double
                lib.swmm_getIndex.argtypes=[ctypes.c_int,ctypes.c_char_p];lib.swmm_getIndex.restype=ctypes.c_int
                rows=[]
                for units in ('CFS','GPM','MGD','CMS','LPS','MLD'):
                    for label,args in cases:
                        with self.subTest(family=family,units=units,case=label):
                            text=source(units=units,**args)
                            model=Model.from_document(InpDocument.from_text(text))
                            expected=model.inspect_field(NODE,'max_depth').semantics.effective
                            initial=model.inspect_field(NODE,'initial_depth').semantics.effective
                            self.assertEqual(expected.status,'known');self.assertEqual(initial.status,'known')
                            base=root/(family+'-'+units+'-'+label);base.mkdir()
                            inp,rpt,out=(base/('model'+suffix) for suffix in ('.inp','.rpt','.out'))
                            inp.write_text(text,encoding='utf-8')
                            started=False
                            try:
                                self.assertEqual(lib.swmm_open(os.fsencode(inp),os.fsencode(rpt),os.fsencode(out)),0)
                                index=lib.swmm_getIndex(2,b'J');self.assertGreaterEqual(index,0)
                                before=lib.swmm_getValue(302,index)
                                self.assertAlmostEqual(before,expected.value,places=11)
                                self.assertEqual(lib.swmm_start(0),0);started=True
                                after=lib.swmm_getValue(302,index);actual_initial=lib.swmm_getValue(303,index)
                                self.assertAlmostEqual(after,expected.value,places=11)
                                self.assertAlmostEqual(actual_initial,initial.value,places=11)
                                rows.append(dict(units=units,case=label,max_depth=after,initial_depth=actual_initial))
                            finally:
                                if started:self.assertEqual(lib.swmm_end(),0)
                                self.assertEqual(lib.swmm_close(),0)
                EVIDENCE.append(dict(family=family,kind='network-fields-native',cases=len(rows),rows=rows,
                    library_sha256=hashlib.sha256(Path(path).read_bytes()).hexdigest()))

    def test_queries_survive_archives_and_relocated_continuation(self):
        from easysewer.runtime import RunResult
        from test_native_v2_runner_checkpoint import NativeRunnerCheckpointTests, runner, resume_config
        helper=NativeRunnerCheckpointTests()
        with patch.dict(os.environ,EASYSEWER_CHECKPOINT_PACKAGED='1'):
            for family in ('standard','custom'):
                with self.subTest(family=family),tempfile.TemporaryDirectory() as directory:
                    root=Path(directory)
                    text=source(tail=' 4',offsets=('1','0'))+(
                        '[LOSSES]\nP 0 .2 .3\n[COORDINATES]\nJ 1 2\nJ 3 4\n[VERTICES]\nP 5 6\n'
                        '[MAP]\nUNITS METERS\n[INFLOWS]\nJ FLOW "" FLOW 1 1 0.1\n')
                    model=Model.from_document(InpDocument.from_text(text,source='network-source.inp'),strict=True)
                    model.update_options(allow_ponding=True)
                    model.links.update('P',length=110)
                    expected_queries=queries(model)
                    original,saved=helper.original(root,family,model=model)
                    helper.success(original);self.assertEqual(queries(original.snapshot.model()),expected_queries)
                    original.save(root/'expected');expected=RunResult.load(root/'expected')
                    self.assertEqual(queries(expected.snapshot.model()),expected_queries)
                    shutil.rmtree(root/'first');(root/'original.hsf').unlink();(root/'saved').rename(root/'moved')
                    actual=runner(family).resume(root/'moved'/saved[0].directory.name,resume_config(root/'resumed'))
                    helper.equivalent(expected,actual);self.assertEqual(queries(actual.snapshot.model()),expected_queries)
                    EVIDENCE.append(dict(family=family,kind='network-fields-checkpoint',out_sha256=actual.output.sha256,
                        fields=len(PATHS),original_workspace_removed=True,source_sha256=model.field_provenance(PIPE,'length').source_sha256))


if __name__=='__main__':
    unittest.main()
