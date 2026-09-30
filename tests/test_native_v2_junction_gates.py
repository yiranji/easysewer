"""Full native JUNCTIONS equivalence and active ponded-area observations."""
import ctypes,hashlib,os,struct,tempfile,unittest
from pathlib import Path
from easysewer import get_native_capabilities
from easysewer.io.output import OutputReader
from easysewer.runtime._solver_worker import _configure_error_mode
from test_native_v2_standard_io import direct_library
from easysewer.utils import probe_library_path
from test_junction_gates_v2 import CASES,UNITS,NODE,source,load,created,portable
from test_native_v2_title_report_gates import solve

EVIDENCE=[]
FAMILIES=(('standard','swmm5','swmm_getEasySewerStandardFixes'),('custom','flexible_ponding','swmm_getEasySewerNativeIOFixes'))
sha=lambda raw:hashlib.sha256(raw).hexdigest()


@unittest.skipUnless(all(get_native_capabilities()[name] for name in ('swmm_solver','flexible_ponding')),'Both native solvers required')
class NativeJunctionGateTests(unittest.TestCase):
    def test_each_variant_literal_creation_normalization_json_complete_outputs(self):
        _configure_error_mode()
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(os.environ.get('EASYSEWER_JUNCTION_GATE_OUTPUT',temporary))
            for family,name,symbol in FAMILIES:
                lib,library=direct_library(str(probe_library_path(name)),revision_symbol=symbol)
                for units in UNITS:
                    for variant,cases in CASES.items():
                        for index,(tail,values) in enumerate(cases):
                            for ponding in (False,True):
                                text=source(tail,units,ponding);model=load(text)
                                fresh=created(tail,values,units,ponding)
                                sources={'literal':text,'created':fresh.to_document().text,
                                    'normalized':model.to_document(normalize=True).text,'json':portable(model).to_document().text}
                                group=f'{family}-{units}-{list(CASES).index(variant)}-{index}-{int(ponding)}'
                                outputs=[];runs=[]
                                for kind,text in sources.items():
                                    folder=root/'equivalence'/group/kind;folder.mkdir(parents=True)
                                    result=solve(self,lib,folder,text);outputs.append((result['out'],result['report']))
                                    runs.append(dict(kind=kind,files={suffix:dict(path=str(folder/('model'+suffix)),sha256=sha((folder/('model'+suffix)).read_bytes())) for suffix in ('.inp','.rpt','.out')},normalized_report_sha256=sha(result['report'])))
                                self.assertTrue(all(value==outputs[0] for value in outputs[1:]),group)
                                EVIDENCE.append(dict(kind='equivalence',group=group,variant=variant,tail=tail,units=units,ponding=ponding,family=family,
                                    library_sha256=sha(library.read_bytes()),runs=runs,full_out_equal=True,normalized_report_equal=True))

    def test_ponded_area_inactive_and_active_flooding_storage_and_depth(self):
        _configure_error_mode()
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(os.environ.get('EASYSEWER_JUNCTION_GATE_OUTPUT',temporary))
            for family,name,symbol in FAMILIES:
                lib,library=direct_library(str(probe_library_path(name)),revision_symbol=symbol)
                for units in UNITS:
                    observations={};runs=[]
                    for ponding in (False,True):
                        for area in (0,20,40):
                            text=source(f' 1 0 0 {area}',units,ponding)
                            folder=root/'ponding'/family/units/f'{int(ponding)}-{area}';folder.mkdir(parents=True)
                            result=solve(self,lib,folder,text)
                            with OutputReader(folder/'model.out') as reader:
                                depth=reader.series(NODE,'swmm:depth').values
                                flooding=reader.series(None,'swmm:flooding').values
                                storage=reader.series(None,'swmm:storage').values
                            observations[(ponding,area)]=dict(out=result['out'],depth=max(depth),flooding=sum(flooding),storage=max(storage))
                            runs.append(dict(ponding=ponding,area=area,max_depth=max(depth),sum_flooding=sum(flooding),max_storage=max(storage),
                                files={suffix:dict(path=str(folder/('model'+suffix)),sha256=sha((folder/('model'+suffix)).read_bytes())) for suffix in ('.inp','.rpt','.out')},normalized_report_sha256=sha(result['report'])))
                    off=observations[(False,0)];zero=observations[(True,0)];small=observations[(True,20)];large=observations[(True,40)]
                    self.assertEqual(off['out'],observations[(False,20)]['out'])
                    self.assertEqual(off['out'],observations[(False,40)]['out'])
                    self.assertEqual(off['out'],zero['out'])
                    self.assertGreater(off['flooding'],small['flooding'])
                    self.assertGreater(off['flooding'],large['flooding'])
                    self.assertGreater(small['storage'],off['storage'])
                    self.assertGreater(large['storage'],off['storage'])
                    self.assertGreater(small['depth'],large['depth'])
                    self.assertGreater(large['depth'],off['depth'])
                    EVIDENCE.append(dict(kind='ponded-area-effect',family=family,units=units,library_sha256=sha(library.read_bytes()),runs=runs))
